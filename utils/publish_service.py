"""Record each send before dispatch; a lost receipt is never an invitation to resend."""

import asyncio
import contextvars
import json
import time
from types import SimpleNamespace
from contextlib import asynccontextmanager
from telegram.error import BadRequest, Forbidden, RetryAfter
from database.db_manager import get_db
from utils.telegram_api import TelegramAPIError

_publication = contextvars.ContextVar("publication", default=None)


class PublishInterrupted(Exception):
    def __init__(self, state, operation_id=None):
        self.state, self.operation_id = state, operation_id
        super().__init__(
            "发送结果待核对"
            if state == "unknown"
            else "部分内容已发布" if state == "partial" else "发布未完成"
        )


@asynccontextmanager
async def publication_scope(operation_id):
    from utils.operation_service import get_operation

    op = await get_operation(operation_id)
    payload = json.loads(op["payload_json"])
    token = _publication.set(
        {"id": operation_id, "part": 0, "destination": payload.get("_destination")}
    )
    try:
        yield
    finally:
        _publication.reset(token)


async def send_part(sender, **kwargs):
    publication = _publication.get()
    operation_id = publication["id"] if publication else None
    part_key = None
    if publication:
        if publication.get("destination") is not None:
            kwargs["chat_id"] = publication["destination"]
        publication["part"] += 1
        part_key = str(publication["part"])
        async with get_db() as conn:
            row = await (
                await conn.execute(
                    "SELECT state FROM bot_operations WHERE id=?", (operation_id,)
                )
            ).fetchone()
            if not row or row["state"] not in ("processing", "sending"):
                raise PublishInterrupted(
                    row["state"] if row else "unknown", operation_id
                )
            prior = await (
                await conn.execute(
                    "SELECT * FROM publish_parts WHERE operation_id=? AND part_key=?",
                    (operation_id, part_key),
                )
            ).fetchone()
            if prior:
                if prior["state"] != "succeeded":
                    raise PublishInterrupted("unknown", operation_id)
                messages = [
                    SimpleNamespace(message_id=mid, renderer=prior["renderer"])
                    for mid in json.loads(prior["message_ids_json"])
                ]
                return messages if len(messages) > 1 else messages[0]
            await conn.execute(
                "INSERT INTO publish_parts(operation_id,part_key,state,updated_at) VALUES(?,?,'sending',?)",
                (operation_id, part_key, time.time()),
            )
            await conn.execute(
                "UPDATE bot_operations SET state='sending',updated_at=? WHERE id=?",
                (time.time(), operation_id),
            )
    try:
        result = await sender(**kwargs)
        messages = result if isinstance(result, (list, tuple)) else [result]
        ids = [m.message_id for m in messages]
        if not ids or not all(isinstance(i, int) and i > 0 for i in ids):
            raise ValueError("Missing Telegram receipt")
        if publication:
            async with get_db() as conn:
                await conn.execute(
                    "UPDATE publish_parts SET state='succeeded',message_ids_json=?,renderer=?,updated_at=? WHERE operation_id=? AND part_key=?",
                    (
                        json.dumps(ids),
                        getattr(result, "renderer", None),
                        time.time(),
                        operation_id,
                        part_key,
                    ),
                )
                rows = await (
                    await conn.execute(
                        "SELECT message_ids_json FROM publish_parts WHERE operation_id=? AND state='succeeded' ORDER BY id",
                        (operation_id,),
                    )
                ).fetchall()
                all_ids = [mid for row in rows for mid in json.loads(row[0])]
                await conn.execute(
                    """UPDATE bot_operations SET result_json =
                           json_set(result_json, '$.message_ids', json(?)), updated_at=? WHERE id=?""",
                    (json.dumps(all_ids), time.time(), operation_id),
                )
                if getattr(result, "renderer", None) == "rich":
                    await conn.execute(
                        """UPDATE bot_operations SET result_json =
                           json_set(result_json, '$.renderer', 'rich') WHERE id=?""",
                        (operation_id,),
                    )
        return result
    except BaseException as exc:
        definite = isinstance(exc, (BadRequest, Forbidden, RetryAfter)) or (
            isinstance(exc, TelegramAPIError)
            and exc.category
            in {"invalid_payload", "unsupported", "forbidden", "rate_limited"}
        )
        state = "failed" if definite else "unknown"
        if publication:
            async with get_db() as conn:
                previous = await (
                    await conn.execute(
                        "SELECT 1 FROM publish_parts WHERE operation_id=? AND state='succeeded' LIMIT 1",
                        (operation_id,),
                    )
                ).fetchone()
                if definite and previous:
                    state = "partial"
                await conn.execute(
                    "UPDATE publish_parts SET state=?,error_code=?,updated_at=? WHERE operation_id=? AND part_key=?",
                    (
                        "failed" if definite else "unknown",
                        type(exc).__name__,
                        time.time(),
                        operation_id,
                        part_key,
                    ),
                )
                await conn.execute(
                    "UPDATE bot_operations SET state=?,error_code=?,updated_at=? WHERE id=?",
                    (state, type(exc).__name__, time.time(), operation_id),
                )
        if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            raise
        raise PublishInterrupted(state, operation_id) from exc


def current_operation_id():
    current = _publication.get()
    return current["id"] if current else None


async def set_publication_renderer(renderer):
    operation_id = current_operation_id()
    if operation_id:
        async with get_db() as conn:
            await conn.execute(
                """UPDATE bot_operations SET result_json =
                   json_set(result_json, '$.renderer', ?) WHERE id=?""",
                (renderer, operation_id),
            )
