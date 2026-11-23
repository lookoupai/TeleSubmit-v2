"""Publish and read rich posts through the same receipts as regular posts."""

import json
import logging
from types import SimpleNamespace

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from config.settings import BOT_USERNAME, CHANNEL_ID
from database.db_manager import get_db
from ui.rich_messages import PostContent
from utils.publish_service import send_part, set_publication_renderer
from utils.telegram_api import TelegramAPIError

logger = logging.getLogger(__name__)
FALLBACK_ERRORS = {"unsupported", "invalid_payload"}


async def send_resource(context, content, *, chat_id=CHANNEL_ID):
    chunks = list(split_plain_text(content.source().text))
    api = context.bot_data["telegram_api"]

    async def send_first(chat_id):
        await set_publication_renderer("rich")
        try:
            result = await api.call(
                "sendRichMessage",
                {
                    "chat_id": chat_id,
                    "rich_message": content.render(),
                },
            )
        except TelegramAPIError as exc:
            if exc.category not in FALLBACK_ERRORS:
                raise
            logger.info("频道富消息使用普通视图: %s", exc.category)
            await set_publication_renderer("plain")
            if content.document:
                return await context.bot.send_document(
                    chat_id=chat_id, document=content.document[0]
                )
            return await context.bot.send_message(chat_id=chat_id, text=chunks[0])
        return SimpleNamespace(message_id=result["message_id"], renderer="rich")

    main = await send_part(send_first, chat_id=chat_id)
    if getattr(main, "renderer", None) != "rich":
        for chunk in chunks if content.document else chunks[1:]:
            await send_part(
                context.bot.send_message,
                chat_id=chat_id,
                text=chunk,
                reply_to_message_id=main.message_id,
            )
    return main


def source_for_publication(
    context, snapshot, data, media, docs, spoiler, *, show_submitter, prefix=""
):
    if (
        not snapshot.get("rich_posts")
        or not getattr(context, "bot_data", {}).get("telegram_api")
        or media
        or spoiler
        or len(docs) > 1
    ):
        return None
    content = PostContent.from_submission(
        data, docs, show_submitter=show_submitter, prefix=prefix
    )
    return content if content.document or (not docs and content.body) else None


async def add_post_view_button(context, message_id, markup=None, *, chat_id=CHANNEL_ID):
    try:
        username = BOT_USERNAME or context.bot.username
        rows = list(markup.inline_keyboard) if markup else []
        rows.append(
            [
                InlineKeyboardButton(
                    "纯文本视图",
                    url=f"https://t.me/{username}?start=posttext_{message_id}",
                )
            ]
        )
        await context.bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=InlineKeyboardMarkup(rows),
        )
    except Exception as exc:
        logger.warning("纯文本入口未附加: %s", type(exc).__name__)


def split_plain_text(text):
    while text:
        limit = len(
            text[:4000].encode("utf-16-le")[:8000].decode("utf-16-le", errors="ignore")
        )
        cutoff = text.rfind("\n", 0, limit) if limit < len(text) else limit
        cutoff = cutoff if cutoff >= 100 else limit
        yield text[:cutoff]
        text = text[cutoff:].lstrip("\n")


async def try_post_text(update, context):
    if not context.args or not context.args[0].startswith("posttext_"):
        return False
    message = update.effective_message
    if update.effective_chat.type != "private":
        return True
    try:
        message_id = int(context.args[0].split("_", 1)[1])
        if not 0 < message_id < 2**63:
            raise ValueError
    except ValueError:
        await message.reply_text("帖子编号无效。")
        return True
    async with get_db() as conn:
        row = await (
            await conn.execute(
                "SELECT source_content_json FROM published_posts WHERE message_id=? AND renderer='rich'",
                (message_id,),
            )
        ).fetchone()
    if not row or not row[0]:
        await message.reply_text("找不到这条帖子。")
        return True
    # Check current channel membership before redistributing a private channel's content.
    if not CHANNEL_ID.startswith("@"):
        try:
            member = await context.bot.get_chat_member(
                CHANNEL_ID, update.effective_user.id
            )
            allowed = member.status in {
                "member",
                "administrator",
                "creator",
            } or getattr(member, "is_member", False)
        except Exception:
            allowed = False
        if not allowed:
            await message.reply_text("请先加入原频道再查看。")
            return True
    source = json.loads(row[0])
    for chunk in split_plain_text(source["text"]):
        await message.reply_text(chunk, disable_web_page_preview=True)
    for file_id, _ in source.get("documents", []):
        await message.reply_document(file_id)
    return True


async def edit_review_content(
    context, target, *, renderer, text=None, rich=None, markup=None
):
    api = getattr(context, "bot_data", {}).get("telegram_api")
    if renderer == "rich" and api:
        data = target.parameters()
        if rich:
            data["rich_message"] = rich
        else:
            from ui.rich_messages import paragraph

            data["rich_message"] = {"blocks": [paragraph(text)]}
        data["reply_markup"] = markup.to_dict() if markup else {"inline_keyboard": []}
        return await api.call("editMessageText", data)
    return await context.bot.edit_message_text(
        **target.parameters(), text=text, reply_markup=markup
    )
