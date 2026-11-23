"""P0 regressions: real SQLite transactions, mocked external transports only."""

import asyncio
import importlib
import importlib.util
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import CallbackQuery, Message, Update
from telegram.error import BadRequest, TimedOut


@pytest.fixture
async def p0_db(tmp_path, monkeypatch):
    from database import db_manager

    monkeypatch.setattr(db_manager, "DB_PATH", str(tmp_path / "p0.db"))
    await db_manager.init_db()
    return db_manager.get_db


async def seed_order(db, *, paid=False):
    async with db() as conn:
        await conn.execute(
            "INSERT INTO ad_orders(out_trade_no,user_id,sku_id,credits,amount,currency,status,created_at,upay_trade_id) VALUES ('AD_TEST',11,'p1',5,'10','USDT',?,1,'T_TEST')",
            ("paid" if paid else "created",),
        )


def new_module(name):
    assert importlib.util.find_spec(name) is not None, f"Missing P0 capability: {name}"
    return importlib.import_module(name)


@pytest.mark.asyncio
async def test_existing_purchase_ledger_does_not_credit_again(p0_db):
    from utils import paid_ad_service as service

    await seed_order(p0_db)
    async with p0_db() as conn:
        await conn.execute("INSERT INTO user_ad_credits VALUES(11,5,1)")
        await conn.execute(
            "INSERT INTO ad_credit_ledger(out_trade_no,user_id,delta,reason,created_at) VALUES('AD_TEST',11,5,'purchase',1)"
        )
    assert await service._credit_purchase_if_needed(
        out_trade_no="AD_TEST", trade_id="T_TEST", now=2
    )
    assert await service.get_balance(11) == 5


@pytest.mark.asyncio
async def test_concurrent_payment_confirmations_credit_once(p0_db):
    from utils import paid_ad_service as service

    await seed_order(p0_db)
    results = await asyncio.gather(
        *(
            service._credit_purchase_if_needed(
                out_trade_no="AD_TEST", trade_id="T_TEST", now=2
            )
            for _ in range(4)
        )
    )
    assert all(results)
    assert await service.get_balance(11) == 5


@pytest.mark.asyncio
async def test_foreign_order_callback_never_contacts_gateway(p0_db, monkeypatch):
    from handlers import paid_ad_handlers as handler

    await seed_order(p0_db)
    gateway = AsyncMock(return_value=True)
    monkeypatch.setattr(handler, "confirm_paid_by_trade_id", gateway)
    monkeypatch.setattr(handler.runtime_settings, "paid_ad_enabled", lambda: True)
    query = SimpleNamespace(
        data="paid_ad_check_AD_TEST",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        message=None,
        inline_message_id="INLINE",
    )
    update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=99)
    )
    await handler.handle_paid_ad_callback(
        update, SimpleNamespace(bot=AsyncMock(), user_data={})
    )
    gateway.assert_not_awaited()


@pytest.mark.asyncio
async def test_migrations_are_repeatable(p0_db):
    from database.db_manager import init_db

    await init_db()
    async with p0_db() as conn:
        cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        names = {r[0] for r in await cursor.fetchall()}
        assert {
            "schema_migrations",
            "bot_operations",
            "publish_parts",
            "bot_message_views",
        } <= names
        await conn.execute(
            "INSERT INTO submissions(user_id,text_content) VALUES(11,'first')"
        )
        row = await (
            await conn.execute(
                "SELECT session_id,revision FROM submissions WHERE user_id=11"
            )
        ).fetchone()
        assert row["session_id"]
        await conn.execute(
            "UPDATE submissions SET text_content='second' WHERE user_id=11"
        )
        changed = await (
            await conn.execute(
                "SELECT session_id,revision FROM submissions WHERE user_id=11"
            )
        ).fetchone()
        assert changed["session_id"] == row["session_id"]
        assert changed["revision"] > row["revision"]


@pytest.mark.asyncio
async def test_review_decision_has_one_winner(p0_db):
    service = new_module("utils.operation_service")
    async with p0_db() as conn:
        await conn.execute(
            "INSERT INTO pending_reviews(user_id,submission_data) VALUES(11,'{}')"
        )
    results = await asyncio.gather(
        *(
            service.claim_review(1, "approve", admin, {"text_content": "hello"})
            for admin in [21, 22]
        )
    )
    assert sum(r is not None for r in results) == 1
    async with p0_db() as conn:
        rows = await (await conn.execute("SELECT * FROM bot_operations")).fetchall()
        assert len(rows) == 1
        assert rows[0]["operation_key"] == "review:1"


@pytest.mark.asyncio
async def test_publication_timeout_is_recorded_without_retry(p0_db):
    service = new_module("utils.operation_service")
    publisher = new_module("utils.publish_service")
    op = await service.claim_operation(
        "submit:demo:1:channel", 11, {"text_content": "hello"}
    )
    send = AsyncMock(side_effect=TimedOut())
    with pytest.raises(publisher.PublishInterrupted):
        async with publisher.publication_scope(op["id"]):
            await publisher.send_part(send, chat_id="@test", text="hello")
    send.assert_awaited_once()
    result = await service.get_operation(op["id"])
    assert result["state"] == "unknown"
    assert await service.claim_operation("submit:demo:1:channel", 11, {}) is None


@pytest.mark.asyncio
async def test_publication_preserves_receipts_before_later_failure(p0_db):
    service = new_module("utils.operation_service")
    publisher = new_module("utils.publish_service")
    op = await service.claim_operation("submit:demo:2:channel", 11, {})
    send = AsyncMock(
        side_effect=[SimpleNamespace(message_id=100), BadRequest("invalid file")]
    )
    with pytest.raises(publisher.PublishInterrupted):
        async with publisher.publication_scope(op["id"]):
            await publisher.send_part(send, chat_id="@test", text="hello")
            await publisher.send_part(send, chat_id="@test", document="file")
    result = await service.get_operation(op["id"])
    assert result["state"] == "partial"
    assert json.loads(result["result_json"])["message_ids"] == [100]


@pytest.mark.asyncio
async def test_consume_and_refund_are_once_per_operation(p0_db):
    from utils import paid_ad_service as credits

    service = new_module("utils.operation_service")
    op = await service.claim_operation("submit:credit:1:channel", 11, {})
    async with p0_db() as conn:
        await conn.execute("INSERT INTO user_ad_credits VALUES(11,2,1)")
    assert await credits.reserve_one_credit(11, operation_id=op["id"])
    assert await credits.reserve_one_credit(11, operation_id=op["id"])
    assert await credits.get_balance(11) == 1
    await service.set_operation_state(op["id"], "failed")
    await credits.refund_one_credit(11, operation_id=op["id"])
    await credits.refund_one_credit(11, operation_id=op["id"])
    assert await credits.get_balance(11) == 2


def test_disabled_button_has_exactly_one_action(monkeypatch):
    from ui.keyboards import Keyboards
    from utils import runtime_settings

    monkeypatch.setattr(
        runtime_settings, "disabled_buttons_enabled", lambda: True, raising=False
    )
    button = Keyboards.rating_keyboard(1, 4.5, 5).inline_keyboard[1][0].to_dict()
    assert button.get("disabled") == {}
    assert "callback_data" not in button


def test_unknown_update_fields_are_preserved_by_current_ptb():
    update = Update.de_json(
        {
            "update_id": 1,
            "widget_query": {
                "id": "q",
                "from": {"id": 11, "is_bot": False, "first_name": "Test"},
                "data": "check:x",
            },
        },
        None,
    )
    assert update.api_kwargs["widget_query"]["id"] == "q"


@pytest.mark.asyncio
async def test_inline_callback_target_can_edit_rating_keyboard():
    module = new_module("utils.callback_context")
    query = SimpleNamespace(inline_message_id="INLINE", message=None)
    bot = AsyncMock()
    target = module.MessageRef.from_query(query)
    await target.edit_markup(bot, reply_markup=None)
    bot.edit_message_reply_markup.assert_awaited_once_with(
        inline_message_id="INLINE", reply_markup=None
    )


@pytest.mark.asyncio
async def test_album_timeout_stops_remaining_groups(p0_db):
    from handlers.publish import handle_media_publish

    bot = SimpleNamespace(send_media_group=AsyncMock(side_effect=TimedOut()))
    with pytest.raises(Exception):
        await handle_media_publish(
            SimpleNamespace(bot=bot), [f"photo:file{i}" for i in range(12)], None, False
        )
    bot.send_media_group.assert_awaited_once()


@pytest.mark.asyncio
async def test_two_review_callbacks_publish_only_once(p0_db, monkeypatch):
    from handlers import review_handlers as handler

    async with p0_db() as conn:
        await conn.execute(
            "INSERT INTO pending_reviews(user_id,submission_data) VALUES(11,'{}')"
        )
    monkeypatch.setattr(handler, "ADMIN_IDS", [21, 22])
    publish = AsyncMock(return_value=(True, ""))
    monkeypatch.setattr(handler, "_publish_approved_submission", publish)

    def update(admin):
        user = SimpleNamespace(id=admin, username="admin")
        query = SimpleNamespace(
            from_user=user,
            data="review_approve_1",
            message=None,
            inline_message_id="INLINE",
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        return SimpleNamespace(callback_query=query, effective_user=user)

    await asyncio.gather(
        *(
            handler.handle_review_callback(update(a), SimpleNamespace(bot=AsyncMock()))
            for a in [21, 22]
        )
    )
    publish.assert_awaited_once()


@pytest.mark.asyncio
async def test_old_confirmation_cannot_publish_current_draft(p0_db, monkeypatch):
    from handlers import callback_handlers as handler

    async with p0_db() as conn:
        await conn.execute(
            "INSERT INTO submissions(user_id,text_content) VALUES(11,'new draft')"
        )
    publish = AsyncMock()
    monkeypatch.setattr(handler, "publish_submission", publish)
    query = SimpleNamespace(
        data="submit_confirm_old_1", answer=AsyncMock(), edit_message_text=AsyncMock()
    )
    update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=11)
    )
    await handler.handle_submit_confirm(
        update, SimpleNamespace(bot=AsyncMock(), user_data={})
    )
    publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_publication_is_not_refunded(p0_db):
    from utils import paid_ad_service as credits

    service = new_module("utils.operation_service")
    op = await service.claim_operation("submit:unknown:1:channel", 11, {})
    async with p0_db() as conn:
        await conn.execute("INSERT INTO user_ad_credits VALUES(11,2,1)")
    await credits.reserve_one_credit(11, operation_id=op["id"])
    await service.set_operation_state(op["id"], "unknown")
    assert not await credits.refund_one_credit(11, operation_id=op["id"])
    assert await credits.get_balance(11) == 1


@pytest.mark.asyncio
async def test_completed_publication_survives_private_notification_failure(
    p0_db, monkeypatch
):
    from handlers import publish as handler
    from utils import runtime_settings

    async with p0_db() as conn:
        await conn.execute(
            "INSERT INTO submissions(user_id,text_content,image_id,document_id,username) VALUES(11,'hello','[]','[]','demo')"
        )
    for name in [
        "duplicate_check_enabled",
        "ai_review_enabled",
        "rating_enabled",
        "bot_notify_owner",
    ]:
        monkeypatch.setattr(runtime_settings, name, lambda: False)
    monkeypatch.setattr(handler, "CHANNEL_ID", "@test")
    sent = []

    async def send(**kwargs):
        if kwargs["chat_id"] == "@test":
            sent.append(kwargs)
            return SimpleNamespace(message_id=101)
        raise BadRequest("private chat blocked")

    user = SimpleNamespace(id=11, username="demo")
    update = SimpleNamespace(effective_user=user, callback_query=None)
    context = SimpleNamespace(
        bot=SimpleNamespace(send_message=send),
        user_data={
            "submit_settings_snapshot": {"rating_enabled": False, "notify_owner": False}
        },
    )
    # A failed notification is a UI failure, not a new publication attempt.
    try:
        await handler.publish_submission(update, context)
    except BadRequest:
        pass
    assert len(sent) == 1
    async with p0_db() as conn:
        op = await (await conn.execute("SELECT * FROM bot_operations")).fetchone()
        assert op["state"] == "succeeded"
        assert json.loads(op["result_json"])["message_ids"] == [101]


@pytest.mark.asyncio
async def test_recovery_does_not_replay_interrupted_sends(p0_db):
    service = new_module("utils.operation_service")
    op = await service.claim_operation("submit:recover:1:channel", 11, {})
    async with p0_db() as conn:
        await conn.execute(
            "INSERT INTO publish_parts(operation_id,part_key,state,updated_at) VALUES(?,'1','sending',1)",
            (op["id"],),
        )
        await conn.execute(
            "UPDATE bot_operations SET state='sending' WHERE id=?", (op["id"],)
        )
    await service.recover_interrupted_operations()
    assert (await service.get_operation(op["id"]))["state"] == "unknown"


@pytest.mark.asyncio
async def test_operator_resolution_requires_uncertain_state(p0_db):
    service = new_module("utils.operation_service")
    resolve = getattr(service, "resolve_publication", None)
    assert callable(resolve), "Missing operator reconciliation path"
    op = await service.claim_operation("submit:resolve:1:channel", 11, {})
    assert not await resolve(op["id"], 21, message_ids=[101])
    await service.set_operation_state(op["id"], "unknown")
    assert await resolve(op["id"], 21, message_ids=[101])
    assert (await service.get_operation(op["id"]))["state"] == "succeeded"


@pytest.mark.asyncio
async def test_foreign_slot_order_callback_never_contacts_gateway(p0_db, monkeypatch):
    from handlers import slot_ad_handlers as handler

    async with p0_db() as conn:
        await conn.execute(
            "INSERT INTO slot_ad_orders(out_trade_no,slot_id,buyer_user_id,creative_id,plan_days,status,created_at) VALUES('SLT_TEST',1,11,1,7,'created',1)"
        )
    gateway = AsyncMock(return_value=True)
    monkeypatch.setattr(handler, "confirm_paid_by_trade_id", gateway)
    query = SimpleNamespace(
        data="slot_ad_check_SLT_TEST", answer=AsyncMock(), message=None
    )
    await handler.handle_slot_callback(
        SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=99)),
        SimpleNamespace(user_data={}),
    )
    gateway.assert_not_awaited()


@pytest.mark.asyncio
async def test_raw_adapter_never_retries_lost_send_receipts():
    module = new_module("utils.telegram_api")

    class Session:
        def post(self, *args, **kwargs):
            raise asyncio.TimeoutError("request URL with secret must not escape")

    client = module.TelegramAPI("TEST_TOKEN", session=Session())
    with pytest.raises(module.TelegramAPIError) as caught:
        await client.call(
            "sendMessage",
            {
                "text": "hello",
                "ephemeral_message_parameters": {"callback_query_id": "q"},
            },
        )
    assert caught.value.category == "unknown_delivery"
    assert "TEST_TOKEN" not in str(caught.value)


@pytest.mark.asyncio
async def test_failed_operation_can_be_retried_with_one_linked_claim(p0_db):
    service = new_module("utils.operation_service")
    retry = getattr(service, "claim_retry", None)
    assert callable(retry), "Missing explicit retry path"
    op = await service.claim_operation(
        "submit:retry:1:channel", 11, {"submission": {"text_content": "hello"}}
    )
    assert await retry(op["id"], 21) is None
    await service.set_operation_state(op["id"], "failed")
    children = await asyncio.gather(retry(op["id"], 21), retry(op["id"], 22))
    assert sum(c is not None for c in children) == 1


@pytest.mark.asyncio
async def test_partial_retry_reuses_successful_steps(p0_db):
    service = new_module("utils.operation_service")
    retry = getattr(service, "claim_retry", None)
    assert callable(retry), "Missing explicit retry path"
    publisher = new_module("utils.publish_service")
    op = await service.claim_operation("submit:retry:2:channel", 11, {})
    async with publisher.publication_scope(op["id"]):
        await publisher.send_part(
            AsyncMock(return_value=SimpleNamespace(message_id=101)),
            chat_id="@test",
            text="hello",
        )
        with pytest.raises(publisher.PublishInterrupted):
            await publisher.send_part(
                AsyncMock(side_effect=BadRequest("bad file")),
                chat_id="@test",
                document="file",
            )
    child = await retry(op["id"], 21)
    sender = AsyncMock(return_value=SimpleNamespace(message_id=102))
    async with publisher.publication_scope(child["id"]):
        first = await publisher.send_part(sender, chat_id="@test", text="hello")
        await publisher.send_part(sender, chat_id="@test", document="file")
    assert first.message_id == 101
    sender.assert_awaited_once_with(chat_id="@test", document="file")


@pytest.mark.asyncio
async def test_post_confirmation_is_bound_to_current_revision(p0_db):
    from handlers.publish import show_submission_confirmation

    async with p0_db() as conn:
        await conn.execute(
            "INSERT INTO submissions(user_id,text_content) VALUES(11,'hello')"
        )
        row = await (
            await conn.execute("SELECT * FROM submissions WHERE user_id=11")
        ).fetchone()
    bot = AsyncMock()
    state = await show_submission_confirmation(
        SimpleNamespace(effective_user=SimpleNamespace(id=11)), SimpleNamespace(bot=bot)
    )
    assert state == 13
    markup = bot.send_message.await_args.kwargs["reply_markup"]
    assert (
        markup.inline_keyboard[0][0].callback_data
        == f"submit_confirm_{row['session_id']}_{row['revision']}"
    )


@pytest.mark.asyncio
async def test_confirmed_ban_awaits_blacklist_result(p0_db, monkeypatch):
    from handlers import review_handlers as handler
    from utils import blacklist

    async with p0_db() as conn:
        await conn.execute(
            "INSERT INTO pending_reviews(user_id,submission_data) VALUES(11,'{}')"
        )
    monkeypatch.setattr(handler, "ADMIN_IDS", [21])
    add = AsyncMock(return_value=False)
    monkeypatch.setattr(blacklist, "add_to_blacklist", add)
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=21),
        data="review_banok_1",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    await handler.handle_review_callback(
        SimpleNamespace(callback_query=query), SimpleNamespace(bot=AsyncMock())
    )
    add.assert_awaited_once()
    assert "拉黑未完成" in query.edit_message_text.await_args.args[0]


@pytest.mark.asyncio
async def test_migration_preserves_legacy_drafts_and_ledger(tmp_path, monkeypatch):
    import sqlite3
    from database import db_manager

    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE submissions(user_id INTEGER PRIMARY KEY,timestamp REAL,mode TEXT,image_id TEXT,document_id TEXT,tags TEXT,link TEXT,title TEXT,note TEXT,spoiler TEXT,username TEXT,text_content TEXT)"
        )
        conn.execute(
            "INSERT INTO submissions(user_id,text_content) VALUES(11,'legacy text')"
        )
        conn.execute(
            "CREATE TABLE ad_credit_ledger(id INTEGER PRIMARY KEY AUTOINCREMENT,out_trade_no TEXT UNIQUE,user_id INTEGER NOT NULL,delta INTEGER NOT NULL,reason TEXT NOT NULL,created_at REAL NOT NULL)"
        )
        conn.execute(
            "INSERT INTO ad_credit_ledger(user_id,delta,reason,created_at) VALUES(11,-1,'consume',1)"
        )
    monkeypatch.setattr(db_manager, "DB_PATH", str(path))
    await db_manager.init_db()
    await db_manager.init_db()
    async with db_manager.get_db() as conn:
        draft = await (await conn.execute("SELECT * FROM submissions")).fetchone()
        assert draft["text_content"] == "legacy text"
        assert draft["session_id"]
        ledger = await (await conn.execute("SELECT * FROM ad_credit_ledger")).fetchone()
        assert ledger["delta"] == -1
        assert ledger["idempotency_key"] is None


@pytest.mark.asyncio
async def test_non_admin_cannot_change_native_button_settings(p0_db, monkeypatch):
    from handlers import telegram_ui_admin as handler

    write = AsyncMock()
    monkeypatch.setattr(handler.runtime_settings, "set_many", write)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=999),
        effective_message=SimpleNamespace(reply_text=AsyncMock()),
    )
    await handler.ui_settings(update, SimpleNamespace(args=["disabled_buttons", "on"]))
    write.assert_not_awaited()


@pytest.mark.asyncio
async def test_repairing_published_record_is_idempotent(p0_db, monkeypatch):
    from handlers import publish as handler

    monkeypatch.setattr(
        handler, "get_search_engine", lambda: SimpleNamespace(add_post=lambda doc: None)
    )
    row = {
        "tags": "#demo",
        "title": "demo",
        "note": "description",
        "link": "",
        "username": "demo",
    }
    await handler.save_published_post(11, 101, row, [], [], [101], "hello")
    await handler.save_published_post(11, 101, row, [], [], [101], "hello")
    async with p0_db() as conn:
        count = await (
            await conn.execute(
                "SELECT count(*) FROM published_posts WHERE message_id=101"
            )
        ).fetchone()
    assert count[0] == 1
    repair = getattr(handler, "repair_publication_record", None)
    assert callable(repair), "Missing record/index repair without resending"


@pytest.mark.asyncio
async def test_plain_submit_clears_previous_paid_mode(p0_db, monkeypatch):
    from handlers import mode_selection as handler

    monkeypatch.setattr(handler, "cleanup_old_data", AsyncMock())
    monkeypatch.setattr(handler, "is_blacklisted", lambda user: False)
    monkeypatch.setattr(handler, "ensure_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr(handler.runtime_settings, "bot_mode", lambda: "TEXT")
    monkeypatch.setattr(handler.runtime_settings, "paid_ad_enabled", lambda: True)
    monkeypatch.setattr(handler, "show_text_welcome", AsyncMock())
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=11, username="demo"),
        message=SimpleNamespace(text="/submit", reply_text=AsyncMock()),
    )
    context = SimpleNamespace(user_data={"paid_ad": True})
    await handler.submit(update, context)
    assert not context.user_data.get("paid_ad")


@pytest.mark.asyncio
async def test_photo_payment_balance_edits_caption(p0_db, monkeypatch):
    from handlers import paid_ad_handlers as handler

    monkeypatch.setattr(handler.runtime_settings, "paid_ad_enabled", lambda: True)
    query = SimpleNamespace(
        data="paid_ad_balance",
        message=SimpleNamespace(photo=[1], document=None, video=None),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        edit_message_caption=AsyncMock(),
    )
    await handler.handle_paid_ad_callback(
        SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=11)),
        SimpleNamespace(user_data={}),
    )
    query.edit_message_caption.assert_awaited_once()
    query.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirmed_no_delivery_releases_reserved_credit(p0_db):
    from utils import operation_service as service, publish_service as publisher
    from utils import paid_ad_service as credits

    op = await service.claim_operation("submit:attest:1:channel", 11, {})
    async with p0_db() as conn:
        await conn.execute("INSERT INTO user_ad_credits VALUES(11,2,1)")
    await credits.reserve_one_credit(11, operation_id=op["id"])
    with pytest.raises(publisher.PublishInterrupted):
        async with publisher.publication_scope(op["id"]):
            await publisher.send_part(
                AsyncMock(side_effect=TimedOut()), chat_id="@test", text="hello"
            )
    assert await service.resolve_publication(op["id"], 21, message_ids=None)
    assert await credits.refund_one_credit(11, operation_id=op["id"])
    assert await credits.get_balance(11) == 2
