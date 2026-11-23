"""Persistent claims; never hold a SQLite write transaction during network I/O."""

import json
import time
from database.db_manager import get_db


async def claim_operation(key, owner_user_id, payload):
    now = time.time()
    async with get_db() as conn:
        cur = await conn.execute(
            """INSERT INTO bot_operations(operation_key,owner_user_id,state,payload_json,created_at,updated_at)
            VALUES(?,?,'processing',?,?,?) ON CONFLICT(operation_key) DO NOTHING""",
            (key, owner_user_id, json.dumps(payload, ensure_ascii=False), now, now),
        )
        if cur.rowcount != 1:
            return None
        row = await (
            await conn.execute(
                "SELECT * FROM bot_operations WHERE id=?", (cur.lastrowid,)
            )
        ).fetchone()
        return dict(row)


async def get_operation(operation_id):
    async with get_db() as conn:
        row = await (
            await conn.execute(
                "SELECT * FROM bot_operations WHERE id=?", (operation_id,)
            )
        ).fetchone()
        return dict(row) if row else None


async def set_operation_state(operation_id, state, *, error_code=None):
    async with get_db() as conn:
        await conn.execute(
            "UPDATE bot_operations SET state=?,error_code=?,updated_at=? WHERE id=?",
            (state, error_code, time.time(), operation_id),
        )


async def claim_review(review_id, action, admin_id, payload=None):
    if action not in ("approve", "reject", "ban"):
        raise ValueError("Invalid review action")
    async with get_db() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        cur = await conn.execute(
            """UPDATE pending_reviews SET status=?,reviewed_by=?,reviewed_at=?,review_note=?
            WHERE id=? AND status='pending'""",
            (
                "approved" if action == "approve" else "rejected",
                admin_id,
                time.time(),
                "ban_pending" if action == "ban" else None,
                review_id,
            ),
        )
        if cur.rowcount != 1:
            return None
        row = await (
            await conn.execute("SELECT * FROM pending_reviews WHERE id=?", (review_id,))
        ).fetchone()
        if action == "approve":
            now = time.time()
            cur = await conn.execute(
                """INSERT INTO bot_operations(operation_key,owner_user_id,state,payload_json,created_at,updated_at)
                VALUES(?,?,'processing',?,?,?)""",
                (
                    f"review:{review_id}",
                    row["user_id"],
                    json.dumps(
                        payload or json.loads(row["submission_data"]),
                        ensure_ascii=False,
                    ),
                    now,
                    now,
                ),
            )
            operation_id = cur.lastrowid
        else:
            operation_id = None
        return {**dict(row), "operation_id": operation_id}


async def recover_interrupted_operations():
    """Startup only, after the prior runner has stopped. Never replay uncertain sends."""
    async with get_db() as conn:
        await conn.execute(
            "UPDATE publish_parts SET state='unknown',error_code='runner_interrupted' WHERE state='sending'"
        )
        await conn.execute(
            """UPDATE bot_operations SET state=CASE
            WHEN EXISTS(SELECT 1 FROM publish_parts p WHERE p.operation_id=bot_operations.id AND p.state='unknown') THEN 'unknown'
            WHEN EXISTS(SELECT 1 FROM publish_parts p WHERE p.operation_id=bot_operations.id AND p.state='succeeded') THEN 'partial'
            ELSE 'failed' END, error_code='runner_interrupted',updated_at=?
            WHERE state IN ('processing','sending')""",
            (time.time(),),
        )
    from utils.paid_ad_service import refund_one_credit

    async with get_db() as conn:
        rows = await (
            await conn.execute(
                "SELECT id,owner_user_id FROM bot_operations WHERE state='failed'"
            )
        ).fetchall()
    for row in rows:
        await refund_one_credit(row["owner_user_id"], operation_id=row["id"])


async def resolve_publication(operation_id, admin_id, *, message_ids=None):
    """Explicit operator attestation after inspecting the configured channel.

    None means confirmed no message was delivered; it cannot override a known receipt.
    """
    if message_ids is not None and (
        not message_ids or any(type(i) is not int or i <= 0 for i in message_ids)
    ):
        raise ValueError("Positive message IDs required")
    async with get_db() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        row = await (
            await conn.execute(
                "SELECT * FROM bot_operations WHERE id=?", (operation_id,)
            )
        ).fetchone()
        if not row or row["state"] not in ("unknown", "partial"):
            return False
        previous = json.loads(row["result_json"]).get("message_ids", [])
        if message_ids is None and previous:
            return False
        if message_ids is not None and not set(previous) <= set(message_ids):
            return False
        if message_ids is None:
            await conn.execute(
                "UPDATE publish_parts SET state='failed',error_code='operator_confirmed_not_sent',updated_at=? WHERE operation_id=? AND state IN ('sending','unknown')",
                (time.time(), operation_id),
            )
        result = {
            **json.loads(row["result_json"]),
            "message_ids": message_ids or [],
            "resolved_by": admin_id,
            "resolved_at": time.time(),
        }
        await conn.execute(
            "UPDATE bot_operations SET state=?,result_json=?,error_code='operator_resolved',updated_at=? WHERE id=?",
            (
                "succeeded" if message_ids else "failed",
                json.dumps(result),
                time.time(),
                operation_id,
            ),
        )
        return True


async def claim_retry(operation_id, admin_id):
    """A linked attempt keeps its own debit/refund identity and prior receipts."""
    async with get_db() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        source = await (
            await conn.execute(
                "SELECT * FROM bot_operations WHERE id=?", (operation_id,)
            )
        ).fetchone()
        if not source or source["state"] not in ("failed", "partial"):
            return None
        if source["state"] == "partial":
            unknown = await (
                await conn.execute(
                    "SELECT 1 FROM publish_parts WHERE operation_id=? AND state IN ('sending','unknown')",
                    (operation_id,),
                )
            ).fetchone()
            if unknown:
                return None
        payload = json.loads(source["payload_json"])
        payload["retry_of"] = operation_id
        payload["retry_by"] = admin_id
        if source["state"] == "partial":
            payload.setdefault("billing_operation_id", operation_id)
        now = time.time()
        cur = await conn.execute(
            """INSERT INTO bot_operations(operation_key,owner_user_id,state,payload_json,result_json,created_at,updated_at)
            VALUES(?,?,'processing',?,?,?,?) ON CONFLICT(operation_key) DO NOTHING""",
            (
                f"retry:{operation_id}",
                source["owner_user_id"],
                json.dumps(payload, ensure_ascii=False),
                source["result_json"],
                now,
                now,
            ),
        )
        if cur.rowcount != 1:
            return None
        child_id = cur.lastrowid
        await conn.execute(
            """INSERT INTO publish_parts(operation_id,part_key,state,message_ids_json,updated_at,renderer)
            SELECT ?,part_key,state,message_ids_json,?,renderer FROM publish_parts WHERE operation_id=? AND state='succeeded' ORDER BY id""",
            (child_id, now, operation_id),
        )
        result = json.loads(source["result_json"])
        result["retry_operation_id"] = child_id
        await conn.execute(
            "UPDATE bot_operations SET result_json=?,updated_at=? WHERE id=?",
            (json.dumps(result), now, operation_id),
        )
        return dict(
            await (
                await conn.execute(
                    "SELECT * FROM bot_operations WHERE id=?", (child_id,)
                )
            ).fetchone()
        )
