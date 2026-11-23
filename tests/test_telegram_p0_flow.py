"""Framework-to-database flow using PTB itself and a deterministic fake wire."""

import json
from types import SimpleNamespace

import pytest
from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ExtBot,
)
from telegram.request import BaseRequest
from tests.test_telegram_p0 import p0_db


class TelegramWire(BaseRequest):
    def __init__(self):
        self.calls = []
        self.message_id = 100

    @property
    def read_timeout(self):
        return 5

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        endpoint = url.rsplit("/", 1)[-1]
        data = request_data.parameters if request_data else {}
        self.calls.append((endpoint, data))
        if endpoint == "getMe":
            result = {
                "id": 123,
                "is_bot": True,
                "first_name": "Test",
                "username": "test_bot",
            }
        elif endpoint in ("answerCallbackQuery", "editMessageReplyMarkup"):
            result = True
        else:
            self.message_id += 1
            chat_id = data.get("chat_id", 11)
            result = {
                "message_id": self.message_id,
                "date": 1700000000,
                "chat": {
                    "id": -100 if chat_id == "@test" else int(chat_id),
                    "type": "channel" if chat_id == "@test" else "private",
                },
                "text": data.get("text", ""),
            }
        return 200, json.dumps({"ok": True, "result": result}).encode()


@pytest.mark.asyncio
async def test_ptb_confirmation_completes_conversation_once(p0_db, monkeypatch):
    from handlers import publish as publisher
    from handlers.callback_handlers import handle_callback_query
    from utils import runtime_settings

    for name in [
        "duplicate_check_enabled",
        "ai_review_enabled",
        "rating_enabled",
        "bot_notify_owner",
    ]:
        monkeypatch.setattr(runtime_settings, name, lambda: False)
    monkeypatch.setattr(publisher, "CHANNEL_ID", "@test")
    monkeypatch.setattr(
        publisher,
        "get_search_engine",
        lambda: SimpleNamespace(add_post=lambda doc: None),
    )
    async with p0_db() as conn:
        await conn.execute(
            "INSERT INTO submissions(user_id,text_content,image_id,document_id,username) VALUES(11,'hello world','[]','[]','demo')"
        )
        row = await (
            await conn.execute("SELECT * FROM submissions WHERE user_id=11")
        ).fetchone()
    wire = TelegramWire()
    bot = ExtBot(token="123:TEST", request=wire, get_updates_request=TelegramWire())
    app = Application.builder().bot(bot).build()

    async def preview(update, context):
        context.user_data["submit_settings_snapshot"] = {
            "rating_enabled": False,
            "notify_owner": False,
        }
        return await publisher.show_submission_confirmation(update, context)

    conversation = ConversationHandler(
        entry_points=[CommandHandler("preview", preview)],
        states={13: [CallbackQueryHandler(handle_callback_query, pattern="^submit_")]},
        fallbacks=[],
        allow_reentry=True,
    )
    app.add_handler(conversation, group=2)
    app.add_handler(CallbackQueryHandler(handle_callback_query), group=3)
    errors = []

    async def error(update, context):
        errors.append(context.error)

    app.add_error_handler(error)
    await app.initialize()
    try:
        user = {"id": 11, "is_bot": False, "first_name": "Demo", "username": "demo"}
        message = {
            "message_id": 1,
            "date": 1700000000,
            "chat": {"id": 11, "type": "private"},
            "from": user,
            "text": "/preview",
            "entities": [{"type": "bot_command", "offset": 0, "length": 8}],
        }
        await app.process_update(
            Update.de_json({"update_id": 1, "message": message}, bot)
        )
        data = f"submit_confirm_{row['session_id']}_{row['revision']}"
        callback = {
            "id": "q1",
            "from": user,
            "chat_instance": "c",
            "data": data,
            "message": {
                "message_id": 101,
                "date": 1700000000,
                "chat": {"id": 11, "type": "private"},
                "text": "preview",
            },
        }
        await app.process_update(
            Update.de_json({"update_id": 2, "callback_query": callback}, bot)
        )
        assert errors == []
        channel_sends = [
            c
            for c in wire.calls
            if c[0] == "sendMessage" and c[1].get("chat_id") == "@test"
        ]
        assert len(channel_sends) == 1
        assert conversation._conversations == {}
        # A genuinely new callback from the stale preview must not publish again.
        callback["id"] = "q2"
        await app.process_update(
            Update.de_json({"update_id": 3, "callback_query": callback}, bot)
        )
        assert (
            len(
                [
                    c
                    for c in wire.calls
                    if c[0] == "sendMessage" and c[1].get("chat_id") == "@test"
                ]
            )
            == 1
        )
        async with p0_db() as conn:
            op = await (await conn.execute("SELECT * FROM bot_operations")).fetchone()
            assert op["state"] == "succeeded"
            assert await (
                await conn.execute("SELECT 1 FROM published_posts")
            ).fetchone()
    finally:
        await app.shutdown()
