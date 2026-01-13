"""
Slot Ads 订单素材编辑（用户自助/后台管理员）测试
"""

import os
import sqlite3
from contextlib import asynccontextmanager

import pytest


class _AsyncCursor:
    def __init__(self, cursor: sqlite3.Cursor):
        self._cursor = cursor

    @property
    def lastrowid(self):
        return self._cursor.lastrowid

    @property
    def rowcount(self):
        return self._cursor.rowcount

    async def execute(self, sql: str, parameters=()):
        self._cursor.execute(sql, parameters)
        return self

    async def fetchone(self):
        return self._cursor.fetchone()

    async def fetchall(self):
        return self._cursor.fetchall()


class _AsyncConn:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    async def cursor(self):
        return _AsyncCursor(self._conn.cursor())

    async def commit(self):
        self._conn.commit()

    async def rollback(self):
        self._conn.rollback()


@pytest.mark.asyncio
async def test_slot_ads_edit_limit_and_admin_force(temp_dir):
    from utils import slot_ad_service, runtime_settings

    now = 1_700_000_000.0
    start_at = now + 3600.0
    end_at = start_at + (31 * 86400.0)

    db_path = os.path.join(temp_dir, "slot_ads_edit_test.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    conn.execute(
        """
        CREATE TABLE slot_ad_creatives (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            button_text TEXT NOT NULL,
            button_url TEXT NOT NULL,
            ai_review_result TEXT,
            ai_review_passed INTEGER,
            created_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE slot_ad_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            out_trade_no TEXT UNIQUE,
            slot_id INTEGER NOT NULL,
            buyer_user_id INTEGER NOT NULL,
            creative_id INTEGER NOT NULL,
            plan_days INTEGER NOT NULL,
            amount TEXT,
            currency TEXT,
            status TEXT NOT NULL,
            upay_trade_id TEXT,
            payment_url TEXT,
            expires_at REAL,
            start_at REAL,
            end_at REAL,
            created_at REAL NOT NULL,
            paid_at REAL,
            terminated_at REAL,
            terminate_reason TEXT,
            reminder_opt_in INTEGER NOT NULL DEFAULT 0,
            remind_at REAL,
            remind_sent INTEGER NOT NULL DEFAULT 0,
            remind_sent_at REAL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE slot_ad_order_edits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            out_trade_no TEXT NOT NULL,
            day_key TEXT NOT NULL,
            editor_type TEXT NOT NULL,
            editor_user_id INTEGER,
            old_creative_id INTEGER,
            new_creative_id INTEGER NOT NULL,
            note TEXT,
            created_at REAL NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX idx_slot_ad_order_edits_order_day ON slot_ad_order_edits(out_trade_no, day_key)")

    conn.execute(
        """
        INSERT INTO slot_ad_creatives(user_id, button_text, button_url, ai_review_result, ai_review_passed, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (111, "原按钮", "https://example.com/a", None, None, now),
    )
    creative_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.execute(
        """
        INSERT INTO slot_ad_orders
        (out_trade_no, slot_id, buyer_user_id, creative_id, plan_days, amount, currency, status,
         created_at, expires_at, start_at, end_at, paid_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "SLT_EDIT_1",
            1,
            111,
            creative_id,
            31,
            "10",
            "CNY",
            "active",
            now,
            now + 1800.0,
            start_at,
            end_at,
            now,
        ),
    )
    conn.commit()

    @asynccontextmanager
    async def _fake_get_db():
        try:
            yield _AsyncConn(conn)
        except Exception:
            conn.rollback()
            raise

    orig_get_db = slot_ad_service.get_db
    slot_ad_service.get_db = _fake_get_db  # type: ignore[assignment]

    orig_snapshot = dict(getattr(runtime_settings, "_snapshot", {}) or {})
    try:
        runtime_settings._snapshot = {  # type: ignore[attr-defined]
            **orig_snapshot,
            runtime_settings.KEY_SLOT_AD_EDIT_LIMIT_PER_ORDER_PER_DAY: "1",
        }

        # 第一次：用户修改成功
        r1 = await slot_ad_service.update_slot_ad_order_creative_by_user(
            out_trade_no="SLT_EDIT_1",
            user_id=111,
            button_text="新按钮1",
            button_url="https://example.com/b",
            now=now,
        )
        assert r1["out_trade_no"] == "SLT_EDIT_1"
        new_creative_id_1 = int(r1["new_creative_id"])
        assert new_creative_id_1 != int(creative_id)

        row = conn.execute("SELECT creative_id FROM slot_ad_orders WHERE out_trade_no = ?", ("SLT_EDIT_1",)).fetchone()
        assert int(row["creative_id"]) == new_creative_id_1

        # 第二次：同一天应被限制
        with pytest.raises(ValueError):
            await slot_ad_service.update_slot_ad_order_creative_by_user(
                out_trade_no="SLT_EDIT_1",
                user_id=111,
                button_text="新按钮2",
                button_url="https://example.com/c",
                now=now,
            )

        # 管理员：不 force 也应被限制（同一订单同一天已编辑过）
        with pytest.raises(ValueError):
            await slot_ad_service.update_slot_ad_order_creative_by_admin(
                out_trade_no="SLT_EDIT_1",
                button_text="管理员按钮",
                button_url="https://example.com/admin",
                force=False,
                now=now,
            )

        # 管理员：force 可以绕过限制
        r2 = await slot_ad_service.update_slot_ad_order_creative_by_admin(
            out_trade_no="SLT_EDIT_1",
            button_text="管理员按钮",
            button_url="https://example.com/admin",
            force=True,
            now=now,
        )
        assert int(r2["new_creative_id"]) != new_creative_id_1

        # 次日：用户可再次修改
        r3 = await slot_ad_service.update_slot_ad_order_creative_by_user(
            out_trade_no="SLT_EDIT_1",
            user_id=111,
            button_text="新按钮次日",
            button_url="https://example.com/next",
            now=now + 86400.0,
        )
        assert str(r3["out_trade_no"]) == "SLT_EDIT_1"
    finally:
        runtime_settings._snapshot = orig_snapshot  # type: ignore[attr-defined]
        slot_ad_service.get_db = orig_get_db  # type: ignore[assignment]
        conn.close()

