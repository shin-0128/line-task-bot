import hashlib
import hmac
import base64
import json
import os
import uuid
from datetime import datetime
from pathlib import Path

import anthropic
import httpx
from fastapi import FastAPI, Request, HTTPException
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build

load_dotenv()

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

TASKS_DIR = Path("tasks")
TASKS_DIR.mkdir(exist_ok=True)

app = FastAPI()

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

TASK_DETECTION_PROMPT = """\
以下のメッセージからタスク（やるべき仕事・依頼・締め切り付きの作業）を検出してください。

タスクっぽい表現の例：
- 「〜やっておいて」「〜しておいて」
- 「〜お願い」「〜お願いします」「〜頼む」
- 「〜までに」（締め切りを示す）
- 「〜しておくこと」「〜すること」
- 「〜やってください」「〜対応して」

タスクが検出された場合は次のJSON配列を返してください（タスクがなければ空配列 [] を返す）：
[
  {
    "content": "タスクの内容（簡潔に）",
    "assigned_to": "担当者名またはユーザーID（明示されていなければ null）",
    "deadline": "期限（YYYY-MM-DD形式。明示されていなければ null）",
    "raw_task_text": "タスクとして検出した元の文章"
  }
]

JSONのみを返してください。説明文は不要です。
"""


def verify_signature(body: bytes, signature: str) -> bool:
    hash = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256,
    ).digest()
    expected = base64.b64encode(hash).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def save_log(event: dict) -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = LOG_DIR / f"{today}.json"

    records = []
    if log_file.exists():
        with open(log_file, "r", encoding="utf-8") as f:
            records = json.load(f)

    records.append(event)

    with open(log_file, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


def save_tasks(tasks: list[dict]) -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    tasks_file = TASKS_DIR / f"{today}.json"

    records = []
    if tasks_file.exists():
        with open(tasks_file, "r", encoding="utf-8") as f:
            records = json.load(f)

    records.extend(tasks)

    with open(tasks_file, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


def get_sheets_service():
    info = json.loads(GOOGLE_CREDENTIALS_JSON)
    credentials = service_account.Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return build("sheets", "v4", credentials=credentials)


def append_tasks_to_sheet(tasks: list[dict]) -> None:
    service = get_sheets_service()
    rows = [
        [
            t.get("timestamp", ""),
            t.get("group_id", ""),
            t.get("user_id", ""),
            t.get("content", ""),
            t.get("assigned_to") or "",
            t.get("deadline") or "",
        ]
        for t in tasks
    ]
    service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range="A:F",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()


def extract_json_text(response) -> str:
    raw = response.content[0].text.strip()
    # マークダウンコードブロック（```json ... ``` や ``` ... ```）を除去
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        if raw.endswith("```"):
            raw = raw.rsplit("```", 1)[0]
    return raw.strip()


def detect_tasks_from_text(text: str, context: dict) -> list[dict]:
    response = claude.messages.create(
        model="claude-opus-4-6",
        max_tokens=1024,
        messages=[
            {
                "role": "user",
                "content": f"{TASK_DETECTION_PROMPT}\n\nメッセージ:\n{text}",
            }
        ],
    )

    raw = extract_json_text(response)
    detected = json.loads(raw)

    return [
        {
            "id": str(uuid.uuid4()),
            "timestamp": datetime.now().isoformat(),
            "source": "text",
            **context,
            **task,
        }
        for task in detected
    ]


def detect_tasks_from_image(image_data: bytes, media_type: str, context: dict) -> list[dict]:
    image_b64 = base64.standard_b64encode(image_data).decode("utf-8")

    response = claude.messages.create(
        model="claude-opus-4-6",
        max_tokens=1024,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": image_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": f"{TASK_DETECTION_PROMPT}\n\n上の画像に含まれるテキストや内容からタスクを検出してください。",
                    },
                ],
            }
        ],
    )

    raw = extract_json_text(response)
    detected = json.loads(raw)

    return [
        {
            "id": str(uuid.uuid4()),
            "timestamp": datetime.now().isoformat(),
            "source": "image",
            **context,
            **task,
        }
        for task in detected
    ]


async def download_line_content(message_id: str) -> tuple[bytes, str]:
    url = f"https://api-data.line.me/v2/bot/message/{message_id}/content"
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        media_type = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
        return resp.content, media_type


@app.post("/webhook")
async def webhook(request: Request):
    signature = request.headers.get("X-Line-Signature", "")
    body = await request.body()

    if not verify_signature(body, signature):
        raise HTTPException(status_code=400, detail="Invalid signature")

    payload = json.loads(body)

    for event in payload.get("events", []):
        source = event.get("source", {})
        if source.get("type") != "group":
            continue

        if event.get("type") != "message":
            continue

        message = event.get("message", {})
        msg_type = message.get("type")
        group_id = source.get("groupId")
        user_id = source.get("userId")
        message_id = message.get("id")

        context = {
            "group_id": group_id,
            "user_id": user_id,
            "message_id": message_id,
        }

        if msg_type == "text":
            text = message.get("text", "")

            log_entry = {
                **context,
                "text": text,
                "raw_event": event,
            }
            save_log(log_entry)
            print(f"[LOG] group={group_id} user={user_id} text={text}")

            try:
                tasks = detect_tasks_from_text(text, context)
                if tasks:
                    save_tasks(tasks)
                    append_tasks_to_sheet(tasks)
                    for t in tasks:
                        print(f"[TASK] {t['content']} / 担当: {t['assigned_to']} / 期限: {t['deadline']}")
            except Exception as e:
                print(f"[ERROR] タスク検出失敗 (text): {e}")

        elif msg_type == "image":
            log_entry = {
                **context,
                "message_type": "image",
                "raw_event": event,
            }
            save_log(log_entry)
            print(f"[LOG] group={group_id} user={user_id} type=image")

            try:
                image_data, media_type = await download_line_content(message_id)
                tasks = detect_tasks_from_image(image_data, media_type, context)
                if tasks:
                    save_tasks(tasks)
                    append_tasks_to_sheet(tasks)
                    for t in tasks:
                        print(f"[TASK] {t['content']} / 担当: {t['assigned_to']} / 期限: {t['deadline']}")
            except Exception as e:
                print(f"[ERROR] タスク検出失敗 (image): {e}")

    return {"status": "ok"}


@app.get("/health")
async def health():
    return {"status": "ok"}
