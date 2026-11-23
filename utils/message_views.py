"""Best-effort UI refresh never changes a business transaction's outcome."""

import logging
from telegram.error import BadRequest, Forbidden
from database.db_manager import get_db

logger = logging.getLogger(__name__)


async def remember_view(
    kind, entity_id, owner_user_id, chat_id, message_id, *, renderer="plain"
):
    async with get_db() as conn:
        await conn.execute(
            """INSERT INTO bot_message_views(entity_kind,entity_id,owner_user_id,chat_id,message_id,renderer)
            VALUES(?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
            (kind, str(entity_id), owner_user_id, str(chat_id), message_id, renderer),
        )


async def refresh_review_views(context, review_id, text):
    from handlers.rich_content import edit_review_content
    from utils.callback_context import MessageRef
    from utils.telegram_api import TelegramAPIError

    async with get_db() as conn:
        rows = await (
            await conn.execute(
                "SELECT * FROM bot_message_views WHERE entity_kind='review' AND entity_id=? AND invalid=0",
                (str(review_id),),
            )
        ).fetchall()
    for row in rows:
        try:
            await edit_review_content(
                context,
                MessageRef(chat_id=row["chat_id"], message_id=row["message_id"]),
                renderer=row["renderer"],
                text=text,
            )
        except TelegramAPIError as exc:
            if exc.category in {"forbidden", "missing_message"}:
                async with get_db() as conn:
                    await conn.execute(
                        "UPDATE bot_message_views SET invalid=1 WHERE id=?",
                        (row["id"],),
                    )
            elif exc.category != "unchanged":
                logger.warning("富消息审核卡片刷新未完成: %s", exc.category)
        except Forbidden:
            async with get_db() as conn:
                await conn.execute(
                    "UPDATE bot_message_views SET invalid=1 WHERE id=?", (row["id"],)
                )
        except BadRequest as exc:
            if "message to edit not found" in str(exc).lower():
                async with get_db() as conn:
                    await conn.execute(
                        "UPDATE bot_message_views SET invalid=1 WHERE id=?",
                        (row["id"],),
                    )
        except Exception as exc:
            logger.warning("审核卡片刷新未完成: %s", type(exc).__name__)
