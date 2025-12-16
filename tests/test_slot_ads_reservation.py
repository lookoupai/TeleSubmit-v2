"""
Slot Ads 售卖准入与占位逻辑测试
"""

import os
import sqlite3
from contextlib import asynccontextmanager

import pytest


@pytest.mark.asyncio
async def test_slot_ads_reserved_order_blocks_resell(temp_dir):
    # 通过 monkeypatch 替换 slot_ad_service.get_db，避免依赖 aiosqlite（部分环境下 aiosqlite 可能卡死）
    from utils import slot_ad_service

    now = 1_700_000_000.0
    start_at = now + 3600.0
    end_at = start_at + (31 * 86400.0)

    db_path = os.path.join(temp_dir, "slot_ads_test.db")
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
        INSERT INTO slot_ad_creatives(user_id, button_text, button_url, ai_review_result, ai_review_passed, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (111, "测试按钮", "https://example.com", None, None, now),
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
            "SLT_TEST_1",
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
        async def cursor(self):
            return _AsyncCursor(conn.cursor())

    @asynccontextmanager
    async def _fake_get_db():
        try:
            yield _AsyncConn()
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    orig_get_db = slot_ad_service.get_db
    slot_ad_service.get_db = _fake_get_db  # type: ignore[assignment]
    try:
        # 未到 start_at：不应展示在频道键盘（get_active_orders），但应占位（get_reserved_orders）
        active = await slot_ad_service.get_active_orders(now=now)
        reserved = await slot_ad_service.get_reserved_orders(now=now)
        assert 1 not in active
        assert 1 in reserved

        # 非购买者：应被阻止再次购买，避免重复售卖
        gate_other = await slot_ad_service.ensure_can_purchase_or_renew(slot_id=1, user_id=222, now=now)
        assert gate_other.get("mode") == "blocked"
        assert gate_other.get("reason") == "occupied"

        # 购买者：未到续期保护窗，应提示暂不可续期
        gate_buyer = await slot_ad_service.ensure_can_purchase_or_renew(slot_id=1, user_id=111, now=now)
        assert gate_buyer.get("mode") == "blocked"
        assert gate_buyer.get("reason") == "renew_not_open"
    finally:
        slot_ad_service.get_db = orig_get_db  # type: ignore[assignment]
        conn.close()
