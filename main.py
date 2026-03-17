# -*- coding: utf-8 -*-
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
redis = Redis(
    url=os.environ["UPSTASH_REDIS_REST_URL"],
    token=os.environ["UPSTASH_REDIS_REST_TOKEN"],
)

TASK_DETECTION_PROMPT = """
以下のメッセージからタスク（やるべき仕事・依頼・締め切り付きの作業）を検出してください。

タスクっぽい表現の例：
- 「〜やっておいて」「〜しておいて」
- 「〜お願い」「〜お願いします」「〜頼む」
- 「〜までに」（締め切りを示す）
- 「〜しておくこと」「〜すること」
- 「〜やってください」「〜対応して」

deadlineについて：「明日」「来週」「今週中」などの相対的な表現は今日の日付2026-03-17を基準に計算してYYYY-MM-DD形式に変換すること。期限が全く言及されていない場合のみnullにする。

タスクが検出された場合はsave_tasksツールを呼び出してください。
タスクがなければ空のリストでsave_tasksを呼び出してください。
"""

TASK_TOOL = {
    "name": "save_tasks",
    "description": "検出したタスクを保存する",
    "input_schema": {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "description": "検出されたタスクのリスト。タスクがなければ空配列。",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "タスクの内容（簡潔に）"},
                        "assigned_to": {"type": "string", "description": "担当者名。明示されていなければ空文字列。"},
                        "deadline": {"type": "string", "description": "期限（YYYY-MM-DD形式）。明日=2026-03-18、来週=2026-03-24、今週中=2026-03-21など今日2026-03-17基準で計算。期限の言及が全くない場合のみ空文字列。"},
                        "raw_task_text": {"type": "string", "description": "タスクとして検出した元の文章"}
                    },
                    "required": ["content", "raw_task_text", "assigned_to", "deadline"]
                }
            }
        },
        "required": ["tasks"]
    }
}


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
    redis.rpush(f"logs:{today}", json.dumps(event, ensure_ascii=False))


def save_tasks(tasks: list[dict]) -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    for task in tasks:
        redis.rpush(f"tasks:{today}", json.dumps(task, ensure_ascii=False))


def get_sheets_service():
    info = json.loads(GOOGLE_CREDENTIALS_JSON)
    credentials = service_account.Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return build("sheets", "v4", credentials=credentials)


def append_tasks_to_sheet(tasks: list[dict], display_name: str = "", group_name: str = "") -> None:
    service = get_sheets_service()
    rows = [
        [
            t.get("timestamp", ""),
            group_name,
            display_name,
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


def detect_tasks_from_text(text: str, context: dict) -> list[dict]:
    response = claude.messages.create(
        model="claude-opus-4-6",
        max_tokens=1024,
        tools=[TASK_TOOL],
        tool_choice={"type": "any"},
        messages=[
            {
                "role": "user",
                "content": f"{TASK_DETECTION_PROMPT}\n\nメッセージ:\n{text}",
            }
        ],
    )

    for block in response.content:
        if block.type == "tool_use":
            detected = block.input.get("tasks", [])
            print(f"[DEBUG] tool_use detected {len(detected)} tasks")
            for task in detected:
                print(f"[DEBUG] task data: {task}")
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

    print("[DEBUG] no tool_use block found")
    return []


def detect_tasks_from_image(image_data: bytes, media_type: str, context: dict) -> list[dict]:
    image_b64 = base64.standard_b64encode(image_data).decode("utf-8")

    response = claude.messages.create(
        model="claude-opus-4-6",
        max_tokens=1024,
        tools=[TASK_TOOL],
        tool_choice={"type": "any"},
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

    for block in response.content:
        if block.type == "tool_use":
            detected = block.input.get("tasks", [])
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

    return []


async def get_line_display_name(user_id: str) -> str:
    url = f"https://api.line.me/v2/bot/profile/{user_id}"
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code == 200:
            return resp.json().get("displayName", user_id)
    return user_id


async def get_line_group_name(group_id: str) -> str:
    url = f"https://api.line.me/v2/bot/group/{group_id}/summary"
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code == 200:
            return resp.json().get("groupName", group_id)
    return group_id


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
            save_log({**context, "text": text, "raw_event": event})
            print(f"[LOG] group={group_id} user={user_id} text={text}")

            try:
                tasks = detect_tasks_from_text(text, context)
                if tasks:
                    save_tasks(tasks)
                    try:
                        display_name = await get_line_display_name(user_id)
                        group_name = await get_line_group_name(group_id)
                        append_tasks_to_sheet(tasks, display_name, group_name)
                    except Exception as sheet_error:
                        print(f"[ERROR] Sheets書き込み失敗: {sheet_error}")
                    for t in tasks:
                        print(f"[TASK] {t['content']} / 担当: {t['assigned_to']} / 期限: {t['deadline']}")
                else:
                    print("[LOG] タスクなし")
            except Exception as e:
                print(f"[ERROR] タスク検出失敗 (text): {e}")

        elif msg_type == "image":
            save_log({**context, "message_type": "image", "raw_event": event})
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
