"""Route Widget queries carried in PTB's preserved Update API fields."""

import asyncio
import logging
import re

from telegram import Update
from telegram.ext import BaseHandler

from utils.telegram_api import TelegramAPIError
from utils.widget_service import encode_data, queue_order_check

logger = logging.getLogger(__name__)
CHECK = re.compile(r"check:((?:AD|SLT)[0-9A-F]{10,32})")


class OrderWidgetHandler(BaseHandler):
    def __init__(self):
        super().__init__(handle_widget_query, block=False)

    def check_update(self, update):
        return isinstance(update, Update) and "widget_query" in update.api_kwargs


async def widget_response(query):
    kind = query.get("type")
    if (
        not isinstance(kind, dict)
        or kind.get("type") != "data"
        or kind.get("wallet_card")
        or query.get("widget_id")
        or query.get("inline_message_id")
    ):
        return "此操作不可用"
    chat, sender = query.get("chat"), query.get("from")
    if (
        not isinstance(chat, dict)
        or not isinstance(sender, dict)
        or chat.get("type") != "private"
    ):
        return "仅限订单所属私聊"
    ids = (sender.get("id"), chat.get("id"), query.get("message_id"))
    data = query.get("data")
    match = CHECK.fullmatch(data) if isinstance(data, str) and len(data) <= 64 else None
    if any(type(value) is not int for value in ids) or not match:
        return "订单操作无效"
    return await queue_order_check(*ids, match[1])


async def handle_widget_query(update, context):
    query = update.api_kwargs["widget_query"]
    api = context.bot_data.get("telegram_api")
    if not api or not isinstance(query, dict) or not isinstance(query.get("id"), str):
        return
    try:
        message = await asyncio.wait_for(widget_response(query), timeout=2)
    except Exception as exc:
        logger.warning("订单 Widget 查询受理失败: %s", type(exc).__name__)
        message = "暂时无法受理，请稍后重试"
    try:
        await asyncio.wait_for(
            api.call(
                "answerWidgetQuery",
                {
                    "widget_query_id": query["id"],
                    "data": encode_data({"message": message}),
                },
            ),
            timeout=7,
        )
    except (TelegramAPIError, asyncio.TimeoutError) as exc:
        logger.info("订单 Widget 应答未送达: %s", getattr(exc, "category", "timeout"))


async def handle_widget_button(update, context):
    query = update.callback_query
    chat = update.effective_chat
    if not chat or chat.type != "private" or chat.id != query.from_user.id:
        await query.answer("仅限订单所属私聊", show_alert=True)
        return
    if query.data == "widget_balance":
        from utils.paid_ad_service import get_balance

        balance = await get_balance(query.from_user.id)
        await query.answer(f"当前广告发布余额：{balance} 次", show_alert=True)
        return
    try:
        message = await asyncio.wait_for(
            queue_order_check(
                query.from_user.id,
                chat.id,
                query.message.message_id,
                query.data.removeprefix("widget_check_"),
            ),
            timeout=2,
        )
    except Exception:
        message = "暂时无法受理，请稍后重试"
    await query.answer(message, show_alert=True)
