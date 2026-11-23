import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update

from database import db_manager
from handlers import channel_listener, rating_handlers, rich_content, widget_handlers
from ui.keyboards import Keyboards
from ui.rich_messages import PostContent, review_blocks
from utils import runtime_settings, widget_service
from utils.content_normalizer import normalize_message_content, normalized_quote
from utils.operation_service import claim_operation, get_operation
from utils.publish_service import PublishInterrupted, publication_scope
from utils.telegram_api import TelegramAPIError


@pytest_asyncio.fixture
async def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_manager, "DB_PATH", str(tmp_path / "ui.db"))
    monkeypatch.setattr(runtime_settings, "_snapshot", {})
    await db_manager.init_db()
    return db_manager.get_db


@pytest.mark.asyncio
async def test_two_voters_receive_private_feedback_and_one_public_refresh(
    isolated_db, monkeypatch
):
    monkeypatch.setattr(runtime_settings, "rating_enabled", lambda: True)
    monkeypatch.setattr(runtime_settings, "rating_allow_update", lambda: True)
    monkeypatch.setattr(
        rating_handlers, "feature_enabled", lambda feature, user_id: True
    )
    async with isolated_db() as conn:
        await conn.execute(
            """INSERT INTO rating_subjects(id,subject_type,subject_key,created_at,updated_at)
               VALUES(1,'domain','example.com',0,0)"""
        )
    markup = Keyboards.rating_keyboard(1, 0, 0)
    extra = InlineKeyboardButton("查看", url="https://example.com")
    channel_message = SimpleNamespace(
        chat=SimpleNamespace(id=-1001, type="channel"),
        message_id=3,
        reply_markup=InlineKeyboardMarkup([*markup.inline_keyboard, [extra]]),
        link="https://t.me/example/3",
    )
    context = SimpleNamespace(bot=AsyncMock(), bot_data={"telegram_api": AsyncMock()})
    queries = [
        SimpleNamespace(
            id=f"q{user}",
            data=f"rating_1_{score}",
            from_user=SimpleNamespace(id=user),
            message=channel_message,
            inline_message_id=None,
            answer=AsyncMock(),
        )
        for user, score in [(10, 3), (11, 5)]
    ]
    await asyncio.gather(
        *(
            rating_handlers.handle_rating_callback(
                SimpleNamespace(callback_query=query), context
            )
            for query in queries
        )
    )
    await rating_handlers.refresh_rating_keyboards(context)
    api = context.bot_data["telegram_api"]
    delivered = {
        args.args[1]["ephemeral_message_parameters"]["callback_query_id"]
        for args in api.call.await_args_list
    }
    assert delivered == {"q10", "q11"}
    assert all("chat_id" not in args.args[1] for args in api.call.await_args_list)
    for query in queries:
        query.answer.assert_awaited_once()
    context.bot.edit_message_reply_markup.assert_awaited_once()
    keyboard = context.bot.edit_message_reply_markup.await_args.kwargs["reply_markup"]
    assert keyboard.inline_keyboard[-1][0] == extra
    assert "4.0" in keyboard.inline_keyboard[1][0].text


def make_content(body="完整正文 <b>按原文显示</b>", docs=()):
    return PostContent.from_submission(
        {"title": "资源", "text_content": body, "tags": "#中文", "username": "writer"},
        docs,
        show_submitter=False,
    )


def test_normalization_keeps_folded_text_links_files_and_quote_separate():
    message = {
        "text": "显示同一内容的普通视图",
        "quote": {"text": "旧引用", "rich_texts": [{"type": "bold", "text": "富引用"}]},
        "rich_message": {
            "blocks": [
                {"type": "heading", "text": "资源"},
                {"type": "new_future_block", "text": "不参与检索"},
                {
                    "type": "expandable_blockquote",
                    "blocks": [
                        {
                            "type": "paragraph",
                            "text": ["中文标签", {"type": "bold", "text": "#深层"}],
                        },
                        {"type": "pre", "text": "可搜索的代码"},
                    ],
                },
                {"type": "table", "cells": [[{"text": "表格内容"}]]},
                {
                    "type": "paragraph",
                    "text": {
                        "type": "url",
                        "text": "链接",
                        "url": "https://example.com/path",
                    },
                },
                {
                    "type": "document",
                    "document": {"file_id": "test-file", "file_name": "完整附件.zip"},
                },
            ]
        },
    }
    result = normalize_message_content(message)
    assert result.text == "资源\n中文标签#深层\n可搜索的代码\n表格内容\n链接"
    assert result.links == ["https://example.com/path"]
    assert result.documents == [("test-file", "完整附件.zip")]
    assert normalized_quote(message) == "富引用"
    assert (
        normalize_message_content(
            {"rich_message": {"blocks": []}, "text": "兼容正文"}
        ).text
        == "兼容正文"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "category, expected", [("unsupported", "sending"), ("unknown_delivery", "unknown")]
)
async def test_rich_sender_falls_back_only_on_definite_rejection(
    isolated_db, category, expected
):
    operation = await claim_operation("test-rich", 11, {})
    context = SimpleNamespace(
        bot=SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=17))
        ),
        bot_data={
            "telegram_api": SimpleNamespace(
                call=AsyncMock(side_effect=TelegramAPIError(category))
            )
        },
    )
    async with publication_scope(operation["id"]):
        if category == "unsupported":
            sent = await rich_content.send_resource(context, make_content("🌻" * 4300))
            assert sent.message_id == 17
            assert context.bot.send_message.await_count == 3
            assert all(
                len(call.kwargs["text"].encode("utf-16-le")) <= 8000
                for call in context.bot.send_message.await_args_list
            )
        else:
            with pytest.raises(PublishInterrupted, match="待核对"):
                await rich_content.send_resource(context, make_content())
            context.bot.send_message.assert_not_awaited()
    op = await get_operation(operation["id"])
    assert op["state"] == expected
    assert json.loads(op["result_json"])["renderer"] == (
        "rich" if expected == "unknown" else "plain"
    )


@pytest.mark.asyncio
async def test_rich_channel_content_survives_index_rebuild(
    isolated_db, monkeypatch, tmp_path
):
    from utils import index_manager, search_engine

    monkeypatch.setattr(index_manager, "DB_PATH", db_manager.DB_PATH)
    monkeypatch.setattr(search_engine, "_search_engine", None)
    search_engine.init_search_engine(index_dir=str(tmp_path / "index"))
    text = "占位内容 " * 500 + " uniquefoldedtoken"
    message = SimpleNamespace(
        message_id=78,
        text=None,
        caption=None,
        api_kwargs={
            "rich_message": {
                "blocks": [
                    {"type": "heading", "text": "资源列表"},
                    {
                        "type": "expandable_blockquote",
                        "blocks": [{"type": "paragraph", "text": text}],
                    },
                ]
            }
        },
    )
    info = channel_listener.validate_and_normalize_message_info(
        await channel_listener.extract_message_info(message)
    )
    assert await channel_listener.save_channel_message(info)
    async with isolated_db() as conn:
        await conn.execute(
            """INSERT INTO published_posts(message_id,title,caption,is_deleted,publish_time)
               VALUES(79,'Deleted resource','deleteduniquetoken',1,0)"""
        )
    engine = search_engine.get_search_engine()
    manager = index_manager.IndexManager()
    assert (await manager.get_index_stats())["in_sync"]
    with engine.ix.searcher() as searcher:
        from whoosh.query import Term

        assert len(searcher.search(Term("description", "uniquefoldedtoken"))) == 1
    rebuilt = await manager.rebuild_index()
    assert rebuilt["success"] and rebuilt["added"] == 1
    # Reopen the configured index, as the next process does after a restart.
    from whoosh import index
    engine.ix = index.open_dir(str(engine.index_dir), indexname=engine.index_name)
    # A second incremental rebuild must not duplicate an already indexed post.
    assert (await manager.rebuild_index(clear_first=False))["success"]
    with engine.ix.searcher() as searcher:
        assert len(searcher.search(Term("description", "uniquefoldedtoken"))) == 1
        assert len(searcher.search(Term("description", "deleteduniquetoken"))) == 0
    assert (await manager.get_index_stats())["in_sync"]


def test_review_blocks_never_interpret_user_html():
    blocks = review_blocks(
        {"title": "<b>标题</b>", "text_content": "<script>正文</script>"},
        42,
        "writer",
        {"category": "待定", "confidence": 0.7, "reason": "需复核"},
    )["blocks"]
    assert blocks[0]["text"] == "<b>标题</b>"
    assert blocks[3]["type"] == "expandable_blockquote"
    assert "<script>正文</script>" in blocks[3]["blocks"][1]["text"]


async def seed_widget_order(db, kind):
    order_id = "AD1234567890ABCDEF" if kind == "ad_widget" else "SLT1234567890ABCDEF"
    async with db() as conn:
        values = (
            order_id,
            time.time(),
            time.time() + 300,
            "11.27",
            "test-address",
            "TRC20",
        )
        if kind == "ad_widget":
            await conn.execute(
                """INSERT INTO ad_orders(out_trade_no,user_id,sku_id,credits,amount,currency,status,
                    created_at,expires_at,pay_amount,pay_address,pay_type)
                   VALUES(?,42,'p1',10,'11.00','USDT','created',?,?,?,?,?)""",
                values,
            )
        else:
            await conn.execute(
                """INSERT INTO slot_ad_orders(out_trade_no,slot_id,buyer_user_id,creative_id,plan_days,amount,
                    currency,status,created_at,expires_at,pay_amount,pay_address,pay_type)
                   VALUES(?,1,42,1,7,'11.00','USDT','created',?,?,?,?,?)""",
                values,
            )
    return order_id


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["ad_widget", "slot_widget"])
async def test_widget_checks_persist_and_paid_state_wins(
    isolated_db, monkeypatch, kind
):
    order_id = await seed_widget_order(isolated_db, kind)
    monkeypatch.setattr(widget_service, "feature_enabled", lambda *args: True)
    api = SimpleNamespace(call=AsyncMock(return_value={"message_id": 53}))
    context = SimpleNamespace(bot=AsyncMock(), bot_data={"telegram_api": api})
    chat = SimpleNamespace(id=42, type="private")
    assert await widget_service.send_order_widget(context, kind, order_id, chat=chat)
    payload = api.call.await_args.args[1]
    assert json.loads(payload["chat_data"])["amount"] == "11.27"
    assert "test-address" not in payload["chat_data"]
    assert (
        await widget_service.queue_order_check(43, 42, 53, order_id)
        == "订单不存在或无权访问"
    )
    assert (
        await widget_service.queue_order_check(42, 42, 54, order_id)
        == "订单不存在或无权访问"
    )

    async def payment_notification(_):
        table = "ad_orders" if kind == "ad_widget" else "slot_ad_orders"
        async with isolated_db() as conn:
            await conn.execute(
                f"UPDATE {table} SET status='paid', paid_at=? WHERE out_trade_no=?",
                (time.time(), order_id),
            )
        return False

    lookup = AsyncMock(side_effect=payment_notification)
    service = (
        widget_service.paid_ad_service
        if kind == "ad_widget"
        else widget_service.slot_ad_service
    )
    monkeypatch.setattr(service, "confirm_paid_by_trade_id", lookup)
    assert (
        await widget_service.queue_order_check(42, 42, 53, order_id) == "正在核对支付"
    )
    assert (
        await widget_service.queue_order_check(42, 42, 53, order_id) == "正在核对支付"
    )
    await widget_service.widget_refresh_tick(context)
    # Run the next cycle with fresh application state, as on container restart.
    restarted = SimpleNamespace(bot=AsyncMock(), bot_data={"telegram_api": api})
    await widget_service.widget_refresh_tick(restarted)
    lookup.assert_awaited_once_with(order_id)
    state = json.loads(api.call.await_args.args[1]["chat_data"])
    assert state["state"] == "paid"
    assert state["r"] > 0
    assert "已到账" in restarted.bot.edit_message_caption.await_args.kwargs["caption"]


@pytest.mark.asyncio
async def test_ptb_widget_update_is_answered_and_queued(isolated_db, monkeypatch):
    order_id = await seed_widget_order(isolated_db, "ad_widget")
    monkeypatch.setattr(widget_service, "feature_enabled", lambda *args: True)
    api = SimpleNamespace(call=AsyncMock(return_value={"message_id": 53}))
    context = SimpleNamespace(bot_data={"telegram_api": api})
    await widget_service.send_order_widget(
        context, "ad_widget", order_id, chat=SimpleNamespace(id=42, type="private")
    )
    update = Update.de_json(
        {
            "update_id": 1,
            "widget_query": {
                "id": "widget-1",
                "from": {"id": 42},
                "chat": {"id": 42, "type": "private"},
                "message_id": 53,
                "type": {"type": "data"},
                "data": "check:" + order_id,
            },
        },
        Bot("123:example"),
    )
    assert widget_handlers.OrderWidgetHandler().check_update(update)
    await widget_handlers.handle_widget_query(update, context)
    assert api.call.await_args.args == (
        "answerWidgetQuery",
        {"widget_query_id": "widget-1", "data": '{"message":"正在核对支付"}'},
    )


def test_widget_budget_counts_utf8_and_compressed_html():
    assert "TelegramWidget" in widget_service.widget_html()
    with pytest.raises(ValueError):
        widget_service.encode_data({"text": "字" * 350})
