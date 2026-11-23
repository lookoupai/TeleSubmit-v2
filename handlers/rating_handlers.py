"""Save votes before sending either private feedback or the public aggregate."""

import asyncio
import logging
from collections import OrderedDict

from telegram import InlineKeyboardMarkup, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import CallbackContext

from config.settings import BOT_USERNAME
from database.db_manager import get_db
from ui.keyboards import Keyboards
from utils import runtime_settings
from utils.callback_context import MessageRef
from utils.telegram_api import TelegramAPIError
from utils.telegram_capabilities import feature_enabled

logger = logging.getLogger(__name__)
PENDING_REFRESHES = "rating_refreshes"


async def save_vote(subject_id, user_id, score, allow_update):
    async with get_db() as conn:
        await conn.execute("PRAGMA busy_timeout=1000")
        await conn.execute("BEGIN IMMEDIATE")
        subject = await (
            await conn.execute(
                "SELECT score_sum, vote_count FROM rating_subjects WHERE id = ?",
                (subject_id,),
            )
        ).fetchone()
        if not subject:
            return "这条内容无法评分", None
        vote = await (
            await conn.execute(
                "SELECT score FROM rating_votes WHERE subject_id = ? AND user_id = ?",
                (subject_id, user_id),
            )
        ).fetchone()
        old_score = vote["score"] if vote else None
        if vote and not allow_update:
            return "你已经给这条内容评分过了", None
        if old_score == score:
            return "你的评分已是当前星级", None

        await conn.execute(
            """INSERT INTO rating_votes (subject_id, user_id, score, created_at, updated_at)
               VALUES (?, ?, ?, unixepoch(), unixepoch())
               ON CONFLICT(subject_id, user_id) DO UPDATE
               SET score = excluded.score, updated_at = excluded.updated_at""",
            (subject_id, user_id, score),
        )
        total = subject["score_sum"] + score - (old_score or 0)
        count = subject["vote_count"] + int(vote is None)
        avg = total / count
        await conn.execute(
            """UPDATE rating_subjects SET score_sum = ?, vote_count = ?, avg_score = ?,
                   updated_at = unixepoch() WHERE id = ?""",
            (total, count, avg, subject_id),
        )
        return ("感谢你的评分！" if vote is None else "已更新你的评分"), (avg, count)


async def refresh_rating_keyboards(context):
    pending = context.bot_data.get(PENDING_REFRESHES)
    if not pending:
        return
    for _ in range(min(len(pending), 20)):
        target, (subject_id, markup) = pending.popitem(last=False)
        try:
            async with get_db() as conn:
                subject = await (
                    await conn.execute(
                        "SELECT avg_score, vote_count FROM rating_subjects WHERE id = ?",
                        (subject_id,),
                    )
                ).fetchone()
        except Exception:
            pending.setdefault(target, (subject_id, markup))
            logger.exception("读取评分汇总失败")
            break
        if not subject:
            continue
        keyboard = Keyboards.rating_keyboard(
            subject_id, subject["avg_score"], subject["vote_count"]
        )
        if markup:
            extra = [
                row
                for row in markup.inline_keyboard
                if not any(
                    str(button.callback_data or "").startswith("rating_")
                    or (
                        button.text.startswith("⭐")
                        and (
                            button.callback_data == "ui_noop"
                            or "disabled" in button.api_kwargs
                        )
                    )
                    for button in row
                )
            ]
            keyboard = InlineKeyboardMarkup([*keyboard.inline_keyboard, *extra])
        try:
            await target.edit_markup(context.bot, reply_markup=keyboard)
        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                logger.warning("评分键盘更新被拒绝")
        except TelegramError as exc:
            logger.warning("评分键盘更新失败: %s", type(exc).__name__)


async def handle_rating_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    started = asyncio.get_running_loop().time()
    if not runtime_settings.rating_enabled():
        await query.answer("评分功能暂未启用")
        return
    try:
        _, subject, value = query.data.split("_")
        subject_id, score = int(subject), int(value)
        if not 1 <= score <= 5 or not 1 <= subject_id < 2**63:
            raise ValueError
    except (ValueError, AttributeError):
        await query.answer("评分数据无效", show_alert=True)
        return
    try:
        text, result = await save_vote(
            subject_id,
            query.from_user.id,
            score,
            runtime_settings.rating_allow_update(),
        )
    except Exception:
        logger.exception("评分保存失败")
        await query.answer("评分处理失败，请稍后重试", show_alert=True)
        return
    if result is None:
        await query.answer(text, show_alert=True)
        return

    message = query.message
    try:
        target = MessageRef.from_query(query)
        pending = context.bot_data.setdefault(PENDING_REFRESHES, OrderedDict())
        pending[target] = (subject_id, getattr(message, "reply_markup", None))
        if len(pending) > 1000:
            pending.popitem(last=False)
    except ValueError:
        pass

    avg, count = result
    try:
        await asyncio.wait_for(query.answer(f"{text}  {score} 星"), timeout=2)
    except (TelegramError, asyncio.TimeoutError):
        logger.debug("评分回调应答已超时或不可用")
    api = context.bot_data.get("telegram_api")
    if (
        not api
        or not message
        or message.chat.type != "channel"
        or not feature_enabled("ephemeral_feedback", query.from_user.id)
    ):
        return
    remaining = 13 - (asyncio.get_running_loop().time() - started)
    if remaining <= 0:
        return
    try:
        data = {
            "text": f"已评 {score} 星 · 平均 {avg:.1f} · {count} 人参与",
            "ephemeral_message_parameters": {"callback_query_id": query.id},
        }
        buttons = []
        if getattr(message, "link", None):
            buttons.append({"text": "查看原帖", "url": message.link})
        username = BOT_USERNAME or getattr(context.bot, "username", None)
        if isinstance(username, str) and username:
            buttons.append(
                {"text": "评分帮助", "url": f"https://t.me/{username}?start=ratinghelp"}
            )
        if buttons:
            data["reply_markup"] = {"inline_keyboard": [buttons]}
        await asyncio.wait_for(api.call("sendMessage", data), timeout=remaining)
    except TelegramAPIError as exc:
        logger.info("评分临时反馈未送达: %s", exc.category)
    except asyncio.TimeoutError:
        logger.info("评分临时反馈超时")
