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
    PAID_AD_PUBLIC_BASE_URL,
    UPAY_BASE_URL,
    UPAY_NOTIFY_PATH,
    UPAY_REDIRECT_PATH,
    UPAY_SECRET_KEY,
)
from database.db_manager import get_db
from utils import runtime_settings
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
    for p in runtime_settings.paid_ad_packages():
        packages.append(PaidAdPackage(sku_id=p.sku_id, credits=int(p.credits), amount=p.amount))
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
    if not runtime_settings.paid_ad_enabled():
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
    expires_at = created_at + (int(runtime_settings.pay_expire_minutes()) * 60)
    type_ = pay_type or runtime_settings.upay_default_type()

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
                runtime_settings.paid_ad_currency(),
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
            SET upay_trade_id = COALESCE(upay_trade_id, ?), payment_url = ?,
                expires_at = COALESCE(?, expires_at), pay_amount = ?, pay_address = ?, pay_type = ?
            WHERE out_trade_no = ?
            """,
            (
                str(trade_id) if trade_id else None,
                str(payment_url) if payment_url else None,
                float(expires_at_from_gateway) if expires_at_from_gateway else None,
                str(parsed["pay_amount"]) if parsed.get("pay_amount") is not None else None,
                parsed.get("pay_address"),
                parsed.get("pay_type") or type_,
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
    *, out_trade_no: str, trade_id: Optional[str], now: float
) -> bool:
    async with get_db() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        order = await (
            await conn.execute(
                "SELECT * FROM ad_orders WHERE out_trade_no=?", (out_trade_no,)
            )
        ).fetchone()
        if not order:
            return False
        if order["status"] == "paid":
            return True
        if (
            order["upay_trade_id"]
            and trade_id
            and str(order["upay_trade_id"]) != str(trade_id)
        ):
            return False
        user_id, credits = int(order["user_id"]), int(order["credits"])
        existing = await (
            await conn.execute(
                "SELECT * FROM ad_credit_ledger WHERE out_trade_no=? OR idempotency_key=?",
                (out_trade_no, f"purchase:{out_trade_no}"),
            )
        ).fetchone()
        if existing:
            if (
                existing["user_id"] != user_id
                or existing["delta"] != credits
                or existing["reason"] != "purchase"
            ):
                raise ValueError("Purchase ledger mismatch")
        else:
            await conn.execute(
                "INSERT INTO ad_credit_ledger(out_trade_no,user_id,delta,reason,created_at,idempotency_key) VALUES(?,?,?,'purchase',?,?)",
                (out_trade_no, user_id, credits, now, f"purchase:{out_trade_no}"),
            )
            await conn.execute(
                """INSERT INTO user_ad_credits(user_id,balance,updated_at) VALUES(?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET balance=balance+excluded.balance,updated_at=excluded.updated_at""",
                (user_id, credits, now),
            )
        await conn.execute(
            "UPDATE ad_orders SET status='paid',paid_at=?,upay_trade_id=COALESCE(?,upay_trade_id) WHERE out_trade_no=?",
            (now, trade_id, out_trade_no),
        )
        return True


async def order_belongs_to_user(out_trade_no: str, user_id: int) -> bool:
    async with get_db() as conn:
        row = await (
            await conn.execute(
                "SELECT 1 FROM ad_orders WHERE out_trade_no=? AND user_id=?",
                (out_trade_no, user_id),
            )
        ).fetchone()
        return row is not None


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


async def reserve_one_credit(user_id: int, *, operation_id: int) -> bool:
    now = time.time()
    async with get_db() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        op = await (
            await conn.execute(
                "SELECT owner_user_id,state FROM bot_operations WHERE id=?",
                (operation_id,),
            )
        ).fetchone()
        if not op or op["owner_user_id"] != user_id:
            return False
        consumed = await (
            await conn.execute(
                "SELECT 1 FROM ad_credit_ledger WHERE idempotency_key=?",
                (f"consume:{operation_id}",),
            )
        ).fetchone()
        refunded = await (
            await conn.execute(
                "SELECT 1 FROM ad_credit_ledger WHERE idempotency_key=?",
                (f"refund:{operation_id}",),
            )
        ).fetchone()
        if refunded:
            return False
        if consumed:
            return True
        if op["state"] != "processing":
            return False
        cursor = await conn.execute(
            "UPDATE user_ad_credits SET balance=balance-1,updated_at=? WHERE user_id=? AND balance>=1",
            (now, user_id),
        )
        if cursor.rowcount != 1:
            return False
        await conn.execute(
            "INSERT INTO ad_credit_ledger(user_id,delta,reason,created_at,idempotency_key) VALUES(?,-1,'consume',?,?)",
            (user_id, now, f"consume:{operation_id}"),
        )
        return True


async def refund_one_credit(user_id: int, *, operation_id: int) -> bool:
    async with get_db() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        op = await (
            await conn.execute(
                "SELECT owner_user_id,state FROM bot_operations WHERE id=?",
                (operation_id,),
            )
        ).fetchone()
        if not op or op["owner_user_id"] != user_id or op["state"] != "failed":
            return False
        receipts = await (
            await conn.execute(
                "SELECT 1 FROM publish_parts WHERE operation_id=? AND state IN ('sending','unknown','succeeded') LIMIT 1",
                (operation_id,),
            )
        ).fetchone()
        if receipts:
            return False
        consumed = await (
            await conn.execute(
                "SELECT 1 FROM ad_credit_ledger WHERE idempotency_key=?",
                (f"consume:{operation_id}",),
            )
        ).fetchone()
        if not consumed:
            return False
        cur = await conn.execute(
            "INSERT INTO ad_credit_ledger(user_id,delta,reason,created_at,idempotency_key) VALUES(?,1,'refund',?,?) ON CONFLICT(idempotency_key) DO NOTHING",
            (user_id, time.time(), f"refund:{operation_id}"),
        )
        if cur.rowcount == 1:
            await conn.execute(
                "UPDATE user_ad_credits SET balance=balance+1,updated_at=? WHERE user_id=?",
                (time.time(), user_id),
            )
        return True
