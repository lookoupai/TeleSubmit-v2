"""
付费广告次数（credits）服务
"""
import logging
import secrets
import sqlite3
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple, cast

from config.settings import (
    PAID_AD_CURRENCY,
    PAID_AD_ENABLED,
    PAID_AD_PACKAGES,
    PAID_AD_PUBLIC_BASE_URL,
    PAY_EXPIRE_MINUTES,
    UPAY_BASE_URL,
    UPAY_DEFAULT_TYPE,
    UPAY_NOTIFY_PATH,
    UPAY_REDIRECT_PATH,
    UPAY_SECRET_KEY,
)
from database.db_manager import get_db
from utils.upay_pro_client import create_order as upay_create_order
from utils.upay_pro_client import normalize_amount, check_status as upay_check_status

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PaidAdPackage:
    sku_id: str
    credits: int
    amount: Decimal


def get_packages() -> List[PaidAdPackage]:
    packages: List[PaidAdPackage] = []
    for p in (PAID_AD_PACKAGES or []):
        packages.append(PaidAdPackage(sku_id=p["sku_id"], credits=int(p["credits"]), amount=p["amount"]))
    return packages


def _build_urls() -> Tuple[str, str]:
    if not PAID_AD_PUBLIC_BASE_URL:
        raise ValueError("PAID_AD.PUBLIC_BASE_URL 未配置，且 WEBHOOK_URL 为空，无法构造 notify_url")
    notify_url = f"{PAID_AD_PUBLIC_BASE_URL}{UPAY_NOTIFY_PATH}"
    redirect_url = f"{PAID_AD_PUBLIC_BASE_URL}{UPAY_REDIRECT_PATH}"
    return notify_url, redirect_url


def parse_upay_create_order_response(resp: Any) -> Dict[str, Any]:
    """
    解析 UPAY_PRO create_order 响应（兼容少量字段差异）。

    UPAY_PRO 源码（046447fa）响应结构：
    - status_code, message, data
    - data: trade_id, order_id, amount, actual_amount, token, expiration_time(毫秒), payment_url
    """

    def _get_data(obj: Any) -> Dict[str, Any]:
        if not isinstance(obj, dict):
            return {}
        data = obj.get("data")
        if isinstance(data, dict):
            return cast(Dict[str, Any], data)
        return cast(Dict[str, Any], obj)

    def _coerce_epoch_seconds(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            t = float(value)
        except (ValueError, TypeError):
            return None
        # UPAY_PRO 的 expiration_time 为 UnixMilli（毫秒）
        if t > 1_000_000_000_000:
            return t / 1000.0
        return t

    data = _get_data(resp)
    trade_id = data.get("trade_id") or data.get("TradeId")
    payment_url = data.get("payment_url") or data.get("paymentUrl") or data.get("url")
    expiration_time = data.get("expiration_time") or data.get("expirationTime")

    actual_amount = data.get("actual_amount") or data.get("actualAmount")
    token = data.get("token") or data.get("Token") or data.get("address")
    pay_type = data.get("type") or data.get("Type")

    pay_amount: Optional[Decimal] = None
    if actual_amount is not None:
        try:
            pay_amount = normalize_amount(actual_amount, decimals=2)
        except Exception:
            pay_amount = None

    return {
        "trade_id": str(trade_id) if trade_id else None,
        "payment_url": str(payment_url) if payment_url else None,
        "expires_at": _coerce_epoch_seconds(expiration_time),
        "pay_amount": pay_amount,
        "pay_address": str(token) if token else None,
        "pay_type": str(pay_type) if pay_type else None,
        "raw_data": data,
    }


async def get_balance(user_id: int) -> int:
    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute("SELECT balance FROM user_ad_credits WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        return int(row["balance"]) if row else 0


async def create_order_for_package(
    *,
    user_id: int,
    sku_id: str,
    pay_type: Optional[str] = None,
) -> Dict[str, Any]:
    if not PAID_AD_ENABLED:
        raise ValueError("付费广告功能未启用")
    if not UPAY_BASE_URL:
        raise ValueError("UPAY_BASE_URL 未配置")
    if not UPAY_SECRET_KEY:
        raise ValueError("UPAY_SECRET_KEY 未配置")

    pkg = next((p for p in get_packages() if p.sku_id == sku_id), None)
    if not pkg:
        raise ValueError("无效套餐")

    notify_url, redirect_url = _build_urls()

    out_trade_no = f"AD{int(time.time())}{secrets.token_hex(4).upper()}"
    created_at = time.time()
    expires_at = created_at + (int(PAY_EXPIRE_MINUTES) * 60)
    type_ = pay_type or UPAY_DEFAULT_TYPE

    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            """
            INSERT INTO ad_orders
            (out_trade_no, user_id, sku_id, credits, amount, currency, status, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                out_trade_no,
                user_id,
                pkg.sku_id,
                int(pkg.credits),
                str(pkg.amount),
                PAID_AD_CURRENCY,
                "created",
                created_at,
                expires_at,
            ),
        )

    upay_resp = await upay_create_order(
        base_url=UPAY_BASE_URL,
        secret_key=UPAY_SECRET_KEY,
        order_id=out_trade_no,
        amount=pkg.amount,
        type_=type_,
        notify_url=notify_url,
        redirect_url=redirect_url,
    )

    parsed = parse_upay_create_order_response(upay_resp)
    trade_id = parsed.get("trade_id")
    payment_url = parsed.get("payment_url")
    expires_at_from_gateway = parsed.get("expires_at")

    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            """
            UPDATE ad_orders
            SET upay_trade_id = ?, payment_url = ?, expires_at = COALESCE(?, expires_at)
            WHERE out_trade_no = ?
            """,
            (
                str(trade_id) if trade_id else None,
                str(payment_url) if payment_url else None,
                float(expires_at_from_gateway) if expires_at_from_gateway else None,
                out_trade_no,
            ),
        )

    return {
        "out_trade_no": out_trade_no,
        "trade_id": trade_id,
        "payment_url": payment_url,
        "expires_at": expires_at_from_gateway or expires_at,
        "pay_amount": parsed.get("pay_amount"),
        "pay_address": parsed.get("pay_address"),
        "pay_type": parsed.get("pay_type") or type_,
        "package": pkg,
        "raw": upay_resp,
    }


async def _credit_purchase_if_needed(
    *,
    out_trade_no: str,
    trade_id: Optional[str],
    now: float,
) -> bool:
    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute("SELECT * FROM ad_orders WHERE out_trade_no = ?", (out_trade_no,))
        order = await cursor.fetchone()
        if not order:
            return False
        if order["status"] == "paid":
            return True

        user_id = int(order["user_id"])
        credits = int(order["credits"])

        try:
            await cursor.execute(
                """
                INSERT INTO ad_credit_ledger(out_trade_no, user_id, delta, reason, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (out_trade_no, user_id, credits, "purchase", now),
            )
        except sqlite3.IntegrityError:
            # 已入账（幂等）
            pass

        # 入账余额
        await cursor.execute(
            """
            INSERT INTO user_ad_credits(user_id, balance, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                balance = user_ad_credits.balance + excluded.balance,
                updated_at = excluded.updated_at
            """,
            (user_id, credits, now),
        )

        await cursor.execute(
            """
            UPDATE ad_orders
            SET status = 'paid', paid_at = ?, upay_trade_id = COALESCE(?, upay_trade_id)
            WHERE out_trade_no = ?
            """,
            (now, trade_id, out_trade_no),
        )
        return True


async def handle_upay_notify(payload: Dict[str, Any]) -> Tuple[bool, str]:
    """
    处理 UPAY_PRO 回调：
    - 仅当 status==2 才入账
    - 幂等（ledger.out_trade_no UNIQUE）
    """
    try:
        status = int(payload.get("status", 0))
    except (ValueError, TypeError):
        return (False, "invalid status")

    if status != 2:
        return (True, "ignored")

    out_trade_no = str(payload.get("order_id") or "").strip()
    trade_id = str(payload.get("trade_id") or "").strip() or None
    if not out_trade_no:
        return (False, "missing order_id")

    amount = payload.get("amount")
    if amount is None:
        return (False, "missing amount")

    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute("SELECT amount FROM ad_orders WHERE out_trade_no = ?", (out_trade_no,))
        row = await cursor.fetchone()
        if not row:
            return (False, "order not found")
        try:
            expected = normalize_amount(row["amount"])
            got = normalize_amount(amount)
        except Exception:
            return (False, "amount parse error")
        if expected != got:
            return (False, "amount mismatch")

    ok = await _credit_purchase_if_needed(out_trade_no=out_trade_no, trade_id=trade_id, now=time.time())
    return (ok, "ok" if ok else "order not found")


async def confirm_paid_by_trade_id(out_trade_no: str) -> bool:
    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute("SELECT upay_trade_id, status FROM ad_orders WHERE out_trade_no = ?", (out_trade_no,))
        row = await cursor.fetchone()
        if not row:
            return False
        if row["status"] == "paid":
            return True
        trade_id = row["upay_trade_id"]
        if not trade_id:
            return False

    resp = await upay_check_status(base_url=UPAY_BASE_URL, trade_id=str(trade_id))
    data = resp.get("data") if isinstance(resp, dict) else None
    if not isinstance(data, dict):
        data = resp
    try:
        status = int(data.get("status", 0))
    except (ValueError, TypeError):
        return False
    if status != 2:
        return False
    return await _credit_purchase_if_needed(out_trade_no=out_trade_no, trade_id=str(trade_id), now=time.time())


async def reserve_one_credit(user_id: int) -> bool:
    now = time.time()
    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            """
            UPDATE user_ad_credits
            SET balance = balance - 1, updated_at = ?
            WHERE user_id = ? AND balance >= 1
            """,
            (now, user_id),
        )
        if cursor.rowcount != 1:
            return False
        await cursor.execute(
            "INSERT INTO ad_credit_ledger(out_trade_no, user_id, delta, reason, created_at) VALUES (NULL, ?, ?, ?, ?)",
            (user_id, -1, "consume", now),
        )
        return True


async def refund_one_credit(user_id: int) -> None:
    now = time.time()
    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            """
            INSERT INTO user_ad_credits(user_id, balance, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                balance = user_ad_credits.balance + excluded.balance,
                updated_at = excluded.updated_at
            """,
            (user_id, 1, now),
        )
        await cursor.execute(
            "INSERT INTO ad_credit_ledger(out_trade_no, user_id, delta, reason, created_at) VALUES (NULL, ?, ?, ?, ?)",
            (user_id, 1, "refund", now),
        )
