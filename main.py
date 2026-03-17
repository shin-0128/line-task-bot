import hashlib
import hmac
import base64
import json
import os
import uuid
from datetime import datetime
import anthropic
import httpx
from fastapi import FastAPI, Request, HTTPException
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from upstash_redis import Redis

load_dotenv()

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")

app = FastAPI()

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
redis = Redis(url=os.environ["UPSTASH_REDIS_REST_URL"], token=os.environ["UPSTASH_REDIS_REST_TOKEN"])

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
    redis.rpush(f"logs:{today}", json.dumps(event))


def save_tasks(tasks: list[dict]) -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    for task in tasks:
        redis.rpush(f"tasks:{today}", json.dumps(task))


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
    start = raw.find("[")
    end = raw.rfind("]") + 1
    if start != -1 and end > start:
        return raw[start:end]
    return raw


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
    print(f"[DEBUG] raw JSON: {repr(raw)}")
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
