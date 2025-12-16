"""
按钮广告位（Slot Ads）服务

约束（KISS/YAGNI）：
- 固定 10 个 slot（由数据库初始化写入 1..10）
- 只实现“当前空位购买 / 到期前 7 天游客不可买但可看可购时间 / 广告主可续期”
- 轻度风控：按钮文案/URL 基础校验 + 可选 AI 风险审核（儿童/恐怖等）
"""

from __future__ import annotations

import html
import json
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from config.settings import (
    ADMIN_IDS,
    BOT_USERNAME,
    PAID_AD_PUBLIC_BASE_URL,
    PAY_EXPIRE_MINUTES,
    SLOT_AD_BUTTON_TEXT_MAX_LEN,
    SLOT_AD_CURRENCY,
    SLOT_AD_ENABLED,
    SLOT_AD_PLANS,
    SLOT_AD_RENEW_PROTECT_DAYS,
    SLOT_AD_URL_MAX_LEN,
    UPAY_BASE_URL,
    UPAY_DEFAULT_TYPE,
    UPAY_NOTIFY_PATH,
    UPAY_REDIRECT_PATH,
    UPAY_SECRET_KEY,
)
from database.db_manager import get_db
from utils.upay_pro_client import check_status as upay_check_status
from utils.upay_pro_client import create_order as upay_create_order
from utils.upay_pro_client import normalize_amount

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SlotAdPlan:
    sku_id: str
    days: int
    amount: Decimal


def get_plans() -> List[SlotAdPlan]:
    plans: List[SlotAdPlan] = []
    for p in (SLOT_AD_PLANS or []):
        plans.append(SlotAdPlan(sku_id=str(p["sku_id"]), days=int(p["days"]), amount=p["amount"]))
    return plans


def is_admin(user_id: int) -> bool:
    return int(user_id) in set(ADMIN_IDS or [])


def _build_urls() -> Tuple[str, str]:
    if not PAID_AD_PUBLIC_BASE_URL:
        raise ValueError("PUBLIC_BASE_URL 未配置（可复用 PAID_AD.PUBLIC_BASE_URL 或 WEBHOOK_URL）")
    notify_url = f"{PAID_AD_PUBLIC_BASE_URL}{UPAY_NOTIFY_PATH}"
    redirect_url = f"{PAID_AD_PUBLIC_BASE_URL}{UPAY_REDIRECT_PATH}"
    return notify_url, redirect_url


def _parse_upay_create_order_response(resp: Any) -> Dict[str, Any]:
    """
    解析 UPAY_PRO create_order 响应（兼容少量字段差异）。
    """

    def _get_data(obj: Any) -> Dict[str, Any]:
        if not isinstance(obj, dict):
            return {}
        data = obj.get("data")
        if isinstance(data, dict):
            return data
        return obj

    def _coerce_epoch_seconds(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            t = float(value)
        except (ValueError, TypeError):
            return None
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


def validate_button_text(text: str) -> str:
    t = (text or "").strip()
    if not t:
        raise ValueError("按钮文案不能为空")
    if "\n" in t or "\r" in t:
        raise ValueError("按钮文案不允许换行")
    if len(t) > int(SLOT_AD_BUTTON_TEXT_MAX_LEN):
        raise ValueError(f"按钮文案过长，最多 {SLOT_AD_BUTTON_TEXT_MAX_LEN} 字符")
    return t


def validate_button_url(url: str) -> str:
    u = (url or "").strip()
    if not u:
        raise ValueError("链接不能为空")
    if len(u) > int(SLOT_AD_URL_MAX_LEN):
        raise ValueError(f"链接过长，最多 {SLOT_AD_URL_MAX_LEN} 字符")
    parsed = urlparse(u)
    if parsed.scheme.lower() != "https":
        raise ValueError("仅允许 https:// 链接")
    if not parsed.netloc:
        raise ValueError("链接格式无效（缺少域名）")
    return u


async def get_slot_defaults() -> Dict[int, Dict[str, Optional[str]]]:
    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute("SELECT slot_id, default_text, default_url, sell_enabled FROM ad_slots ORDER BY slot_id")
        rows = await cursor.fetchall()
        out: Dict[int, Dict[str, Optional[str]]] = {}
        for r in rows:
            out[int(r["slot_id"])] = {
                "default_text": r["default_text"],
                "default_url": r["default_url"],
                "sell_enabled": bool(int(r["sell_enabled"])),
            }
        return out


async def set_slot_default(slot_id: int, default_text: Optional[str], default_url: Optional[str]) -> None:
    now = time.time()
    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            "UPDATE ad_slots SET default_text = ?, default_url = ?, updated_at = ? WHERE slot_id = ?",
            (default_text, default_url, now, int(slot_id)),
        )


async def set_slot_sell_enabled(slot_id: int, enabled: bool) -> None:
    now = time.time()
    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            "UPDATE ad_slots SET sell_enabled = ?, updated_at = ? WHERE slot_id = ?",
            (1 if enabled else 0, now, int(slot_id)),
        )


async def _get_active_order_for_slot(slot_id: int, now: float) -> Optional[Dict[str, Any]]:
    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            """
            SELECT o.*, c.button_text AS button_text, c.button_url AS button_url
            FROM slot_ad_orders o
            JOIN slot_ad_creatives c ON c.id = o.creative_id
            WHERE o.slot_id = ?
              AND o.status = 'active'
              AND o.start_at IS NOT NULL AND o.end_at IS NOT NULL
              AND o.start_at <= ? AND o.end_at > ?
            ORDER BY o.id DESC
            LIMIT 1
            """,
            (int(slot_id), now, now),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def _get_reserved_order_for_slot(slot_id: int, now: float) -> Optional[Dict[str, Any]]:
    """
    获取 slot 已支付/已占用订单（用于“占位/售卖准入”，不要求 start_at <= now）。

    说明：
    - Slot Ads 的 start_at 可能是“下一次定时消息发送时间”，在 start_at 到来前也应视为已售出，避免重复售卖。
    - 键盘展示仍使用 _get_active_order_for_slot（仅展示生效窗口内的按钮）。
    """
    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            """
            SELECT o.*, c.button_text AS button_text, c.button_url AS button_url
            FROM slot_ad_orders o
            JOIN slot_ad_creatives c ON c.id = o.creative_id
            WHERE o.slot_id = ?
              AND o.status = 'active'
              AND o.start_at IS NOT NULL AND o.end_at IS NOT NULL
              AND o.end_at > ?
            ORDER BY o.end_at DESC, o.id DESC
            LIMIT 1
            """,
            (int(slot_id), now),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_active_orders(now: Optional[float] = None) -> Dict[int, Dict[str, Any]]:
    t = float(now if now is not None else time.time())
    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            """
            SELECT o.*, c.button_text AS button_text, c.button_url AS button_url
            FROM slot_ad_orders o
            JOIN slot_ad_creatives c ON c.id = o.creative_id
            WHERE o.status = 'active'
              AND o.start_at IS NOT NULL AND o.end_at IS NOT NULL
              AND o.start_at <= ? AND o.end_at > ?
            """,
            (t, t),
        )
        rows = await cursor.fetchall()
        out: Dict[int, Dict[str, Any]] = {}
        for r in rows:
            out[int(r["slot_id"])] = dict(r)
        return out


async def get_reserved_orders(now: Optional[float] = None) -> Dict[int, Dict[str, Any]]:
    """
    获取“已售出/已支付”的 slot 订单（用于后台展示与售卖准入，不要求 start_at <= now）。
    """
    t = float(now if now is not None else time.time())
    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            """
            SELECT o.*, c.button_text AS button_text, c.button_url AS button_url
            FROM slot_ad_orders o
            JOIN slot_ad_creatives c ON c.id = o.creative_id
            WHERE o.status = 'active'
              AND o.start_at IS NOT NULL AND o.end_at IS NOT NULL
              AND o.end_at > ?
            ORDER BY o.end_at DESC, o.id DESC
            """,
            (t,),
        )
        rows = await cursor.fetchall()
        out: Dict[int, Dict[str, Any]] = {}
        for r in rows:
            slot_id = int(r["slot_id"])
            # 同一 slot 只保留“占用到最晚”的那一单，避免历史重复单导致误判为可售
            if slot_id not in out:
                out[slot_id] = dict(r)
        return out


async def get_pending_orders(now: Optional[float] = None) -> Dict[int, Dict[str, Any]]:
    """
    获取“已创建待支付/待确认”的 slot 订单（用于后台排障展示，不参与占位逻辑）。
    """
    t = float(now if now is not None else time.time())
    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            """
            SELECT o.*, c.button_text AS button_text, c.button_url AS button_url
            FROM slot_ad_orders o
            JOIN slot_ad_creatives c ON c.id = o.creative_id
            WHERE o.status = 'created'
              AND (o.expires_at IS NULL OR o.expires_at > ?)
            ORDER BY o.created_at DESC, o.id DESC
            """,
            (t,),
        )
        rows = await cursor.fetchall()
        out: Dict[int, Dict[str, Any]] = {}
        for r in rows:
            slot_id = int(r["slot_id"])
            if slot_id not in out:
                out[slot_id] = dict(r)
        return out


def _buy_deeplink(slot_id: int) -> Optional[str]:
    if not BOT_USERNAME:
        return None
    return f"https://t.me/{BOT_USERNAME}?start=buy_slot_{int(slot_id)}"


def build_channel_keyboard(
    *,
    slot_defaults: Dict[int, Dict[str, Optional[str]]],
    active_orders: Dict[int, Dict[str, Any]],
) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for slot_id in sorted(slot_defaults.keys()):
        active = active_orders.get(int(slot_id))
        if active:
            rows.append([InlineKeyboardButton(str(active["button_text"]), url=str(active["button_url"]))])
            continue

        slot = slot_defaults[int(slot_id)]
        sell_enabled = bool(slot.get("sell_enabled"))
        default_text = slot.get("default_text")
        default_url = slot.get("default_url")
        buy_url = _buy_deeplink(slot_id) if sell_enabled else None

        line: List[InlineKeyboardButton] = []
        if default_text and default_url:
            line.append(InlineKeyboardButton(str(default_text), url=str(default_url)))
        if sell_enabled:
            buy_label = "购买（独享此行）"
            if buy_url:
                line.append(InlineKeyboardButton(buy_label, url=buy_url))
            else:
                # BOT_USERNAME 未配置时降级为 callback（让用户至少能看到入口）
                line.append(InlineKeyboardButton(buy_label, callback_data=f"slot_buy_{int(slot_id)}"))
        if line:
            rows.append(line)

    return InlineKeyboardMarkup(rows)


async def create_creative(*, user_id: int, button_text: str, button_url: str, ai_review: Optional[Dict[str, Any]] = None) -> int:
    now = time.time()
    ai_review_json = json.dumps(ai_review, ensure_ascii=False) if ai_review else None
    ai_passed = None
    if ai_review and "passed" in ai_review:
        ai_passed = 1 if bool(ai_review.get("passed")) else 0

    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            """
            INSERT INTO slot_ad_creatives(user_id, button_text, button_url, ai_review_result, ai_review_passed, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (int(user_id), button_text, button_url, ai_review_json, ai_passed, now),
        )
        return int(cursor.lastrowid)


async def get_current_active_window(slot_id: int, now: Optional[float] = None) -> Optional[Tuple[float, float, int]]:
    """
    获取 slot 已售出广告的 (start_at, end_at, buyer_user_id)。

    注意：不要求 start_at <= now（start_at 可能是下一次定时消息发送时间，但在此之前也应禁止再次售卖）。
    """
    t = float(now if now is not None else time.time())
    row = await _get_reserved_order_for_slot(slot_id, t)
    if not row:
        return None
    return (float(row["start_at"]), float(row["end_at"]), int(row["buyer_user_id"]))


async def ensure_can_purchase_or_renew(*, slot_id: int, user_id: int, now: Optional[float] = None) -> Dict[str, Any]:
    """
    购买/续期准入判定：
    - slot 无 active：允许购买
    - slot 有 active：
      - 非广告主：拒绝，提示 end_at（预计可购买时间）；若进入保护窗同样拒绝（口径一致）
      - 广告主：
        - 若在保护窗（end_at - RENEW_PROTECT_DAYS <= now < end_at）：允许续期
        - 否则：拒绝（避免“提前多期预售”复杂度）
    """
    t = float(now if now is not None else time.time())
    window = await get_current_active_window(slot_id, now=t)
    if not window:
        return {"mode": "buy"}

    _, end_at, buyer_user_id = window
    end_at = float(end_at)
    buyer_user_id = int(buyer_user_id)
    protect_start = end_at - (int(SLOT_AD_RENEW_PROTECT_DAYS) * 86400)

    if int(user_id) != buyer_user_id:
        return {
            "mode": "blocked",
            "available_at": end_at,
            "reason": "occupied",
        }

    if t >= protect_start and t < end_at:
        return {"mode": "renew", "renew_start_at": end_at, "available_at": end_at}

    return {
        "mode": "blocked",
        "available_at": end_at,
        "reason": "renew_not_open",
    }


async def create_slot_ad_payment_order(
    *,
    slot_id: int,
    buyer_user_id: int,
    creative_id: int,
    plan_days: int,
    planned_start_at: float,
    currency: str = SLOT_AD_CURRENCY,
    pay_type: Optional[str] = None,
) -> Dict[str, Any]:
    if not SLOT_AD_ENABLED:
        raise ValueError("按钮广告位功能未开启")
    if not UPAY_BASE_URL:
        raise ValueError("UPAY_BASE_URL 未配置")
    if not UPAY_SECRET_KEY:
        raise ValueError("UPAY_SECRET_KEY 未配置")

    plan = next((p for p in get_plans() if int(p.days) == int(plan_days)), None)
    if not plan:
        raise ValueError("无效租期套餐")

    notify_url, redirect_url = _build_urls()

    out_trade_no = f"SLT{int(time.time())}{secrets.token_hex(4).upper()}"
    created_at = time.time()
    expires_at = created_at + (int(PAY_EXPIRE_MINUTES) * 60)
    type_ = pay_type or UPAY_DEFAULT_TYPE

    planned_end_at = float(planned_start_at) + (int(plan.days) * 86400)

    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            """
            INSERT INTO slot_ad_orders
            (out_trade_no, slot_id, buyer_user_id, creative_id, plan_days, amount, currency, status,
             created_at, expires_at, start_at, end_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'created', ?, ?, ?, ?)
            """,
            (
                out_trade_no,
                int(slot_id),
                int(buyer_user_id),
                int(creative_id),
                int(plan.days),
                str(plan.amount),
                str(currency),
                created_at,
                expires_at,
                float(planned_start_at),
                float(planned_end_at),
            ),
        )

    upay_resp = await upay_create_order(
        base_url=UPAY_BASE_URL,
        secret_key=UPAY_SECRET_KEY,
        order_id=out_trade_no,
        amount=plan.amount,
        type_=type_,
        notify_url=notify_url,
        redirect_url=redirect_url,
    )

    parsed = _parse_upay_create_order_response(upay_resp)
    trade_id = parsed.get("trade_id")
    payment_url = parsed.get("payment_url")
    expires_at_from_gateway = parsed.get("expires_at")

    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            """
            UPDATE slot_ad_orders
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
        "plan": plan,
        "planned_start_at": planned_start_at,
        "planned_end_at": planned_end_at,
        "raw": upay_resp,
    }


async def mark_order_paid_and_activate_if_needed(*, out_trade_no: str, trade_id: Optional[str], now: Optional[float] = None) -> bool:
    """
    幂等：同一 out_trade_no 只会从 created -> active 一次。
    """
    t = float(now if now is not None else time.time())
    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute("SELECT * FROM slot_ad_orders WHERE out_trade_no = ?", (str(out_trade_no),))
        order = await cursor.fetchone()
        if not order:
            return False
        if order["status"] in ("paid", "active"):
            return True
        if order["status"] != "created":
            return False

        await cursor.execute(
            """
            UPDATE slot_ad_orders
            SET status = 'active', paid_at = ?, upay_trade_id = COALESCE(?, upay_trade_id)
            WHERE out_trade_no = ? AND status = 'created'
            """,
            (t, trade_id, str(out_trade_no)),
        )
        return cursor.rowcount == 1


async def handle_upay_notify_for_slot_ads(payload: Dict[str, Any]) -> Tuple[bool, str]:
    """
    处理 UPAY_PRO 回调（Slot Ads 订单）：
    - 仅 status==2 才激活
    - 校验 amount 与订单一致（按下单金额）
    """
    ok, msg, _, _ = await process_upay_notify_for_slot_ads(payload)
    return (ok, msg)


async def process_upay_notify_for_slot_ads(payload: Dict[str, Any]) -> Tuple[bool, str, bool, Optional[str]]:
    """
    处理 UPAY_PRO 回调（Slot Ads 订单），并返回是否发生“首次激活”（用于避免回调重试导致重复通知）。

    Returns:
        (ok, msg, activated, out_trade_no)
    """
    try:
        status = int(payload.get("status", 0))
    except (ValueError, TypeError):
        return (False, "invalid status", False, None)

    if status != 2:
        out_trade_no = str(payload.get("order_id") or "").strip() or None
        return (True, "ignored", False, out_trade_no)

    out_trade_no = str(payload.get("order_id") or "").strip()
    trade_id = str(payload.get("trade_id") or "").strip() or None
    if not out_trade_no:
        return (False, "missing order_id", False, None)

    amount = payload.get("amount")
    if amount is None:
        return (False, "missing amount", False, out_trade_no)

    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute("SELECT amount, status FROM slot_ad_orders WHERE out_trade_no = ?", (out_trade_no,))
        row = await cursor.fetchone()
        if not row:
            return (False, "order not found", False, out_trade_no)
        if row["status"] in ("paid", "active"):
            return (True, "ok", False, out_trade_no)
        try:
            expected = normalize_amount(row["amount"])
            got = normalize_amount(amount)
        except Exception:
            return (False, "amount parse error", False, out_trade_no)
        if expected != got:
            return (False, "amount mismatch", False, out_trade_no)

    activated = await mark_order_paid_and_activate_if_needed(out_trade_no=out_trade_no, trade_id=trade_id, now=time.time())
    if not activated:
        return (False, "order not found", False, out_trade_no)
    return (True, "ok", True, out_trade_no)


async def get_slot_order_for_user_notice(out_trade_no: str) -> Optional[Dict[str, Any]]:
    """
    获取用于“支付成功通知”的订单信息（含素材）。
    """
    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            """
            SELECT o.out_trade_no, o.slot_id, o.buyer_user_id, o.status, o.start_at, o.end_at, o.paid_at,
                   c.button_text AS button_text, c.button_url AS button_url
            FROM slot_ad_orders o
            JOIN slot_ad_creatives c ON c.id = o.creative_id
            WHERE o.out_trade_no = ?
            LIMIT 1
            """,
            (str(out_trade_no),),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def confirm_paid_by_trade_id(out_trade_no: str) -> bool:
    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute("SELECT upay_trade_id, status FROM slot_ad_orders WHERE out_trade_no = ?", (out_trade_no,))
        row = await cursor.fetchone()
        if not row:
            return False
        if row["status"] in ("paid", "active"):
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
    return await mark_order_paid_and_activate_if_needed(out_trade_no=out_trade_no, trade_id=str(trade_id), now=time.time())


async def terminate_active_order(*, slot_id: int, reason: str, now: Optional[float] = None) -> bool:
    t = float(now if now is not None else time.time())
    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            """
            UPDATE slot_ad_orders
            SET status = 'terminated', terminated_at = ?, terminate_reason = ?
            WHERE slot_id = ? AND status = 'active'
            """,
            (t, (reason or "").strip()[:200], int(slot_id)),
        )
        return cursor.rowcount > 0


def format_epoch(ts: float) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)


def format_slot_blocked_message(*, slot_id: int, available_at: float) -> str:
    return f"该广告位（{int(slot_id)}）当前不可购买。\n预计可购买时间：{html.escape(format_epoch(available_at))}（服务器时间）"


async def enable_expiry_reminder(*, out_trade_no: str, user_id: int, advance_days: int = 1) -> bool:
    """
    用户自愿开启到期提醒（默认关闭）。只允许开启自己的订单。
    """
    now = time.time()
    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            "SELECT buyer_user_id, end_at FROM slot_ad_orders WHERE out_trade_no = ?",
            (str(out_trade_no),),
        )
        row = await cursor.fetchone()
        if not row:
            return False
        if int(row["buyer_user_id"]) != int(user_id):
            return False
        end_at = row["end_at"]
        if end_at is None:
            return False
        remind_at = float(end_at) - (int(advance_days) * 86400)
        await cursor.execute(
            """
            UPDATE slot_ad_orders
            SET reminder_opt_in = 1, remind_at = ?, remind_sent = 0, remind_sent_at = NULL
            WHERE out_trade_no = ?
            """,
            (remind_at, str(out_trade_no)),
        )
        return cursor.rowcount == 1


async def disable_expiry_reminder(*, out_trade_no: str, user_id: int) -> bool:
    """
    用户关闭到期提醒。只允许操作自己的订单。
    """
    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            "SELECT buyer_user_id FROM slot_ad_orders WHERE out_trade_no = ?",
            (str(out_trade_no),),
        )
        row = await cursor.fetchone()
        if not row:
            return False
        if int(row["buyer_user_id"]) != int(user_id):
            return False
        await cursor.execute(
            """
            UPDATE slot_ad_orders
            SET reminder_opt_in = 0, remind_at = NULL, remind_sent = 0, remind_sent_at = NULL
            WHERE out_trade_no = ?
            """,
            (str(out_trade_no),),
        )
        return cursor.rowcount == 1


async def fetch_due_reminders(*, now: Optional[float] = None) -> List[Dict[str, Any]]:
    t = float(now if now is not None else time.time())
    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            """
            SELECT o.out_trade_no, o.slot_id, o.buyer_user_id, o.end_at
            FROM slot_ad_orders o
            WHERE o.reminder_opt_in = 1
              AND o.remind_sent = 0
              AND o.remind_at IS NOT NULL
              AND o.remind_at <= ?
              AND o.status = 'active'
            ORDER BY o.remind_at ASC
            LIMIT 100
            """,
            (t,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def mark_reminder_sent(*, out_trade_no: str, sent_at: Optional[float] = None) -> None:
    t = float(sent_at if sent_at is not None else time.time())
    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            """
            UPDATE slot_ad_orders
            SET remind_sent = 1, remind_sent_at = ?
            WHERE out_trade_no = ?
            """,
            (t, str(out_trade_no)),
        )


async def send_due_reminders(context) -> None:
    """
    JobQueue 定时调用。注意：机器人只能私聊主动联系过的用户，失败也需要落库防重复重试刷屏。
    """
    due = await fetch_due_reminders()
    if not due:
        return

    for item in due:
        out_trade_no = str(item["out_trade_no"])
        slot_id = int(item["slot_id"])
        buyer_user_id = int(item["buyer_user_id"])
        end_at = float(item["end_at"]) if item.get("end_at") is not None else None

        try:
            end_text = format_epoch(end_at) if end_at else "未知"
            renew_link = _buy_deeplink(slot_id)
            text = (
                "🔔 广告位即将到期提醒\n\n"
                f"广告位：{slot_id}\n"
                f"到期时间：{end_text}（服务器时间）\n\n"
                "如需续期，请尽快操作（到期前 7 天为续期保护窗）。"
            )
            if renew_link:
                await context.bot.send_message(
                    chat_id=buyer_user_id,
                    text=text,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("一键续期/购买", url=renew_link)]]),
                    disable_web_page_preview=True,
                )
            else:
                await context.bot.send_message(chat_id=buyer_user_id, text=text)
        except Exception as e:
            logger.warning(f"发送到期提醒失败: user_id={buyer_user_id}, out_trade_no={out_trade_no}, err={e}")
        finally:
            await mark_reminder_sent(out_trade_no=out_trade_no)
