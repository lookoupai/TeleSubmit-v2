"""
按钮广告位（Slot Ads）服务

约束（KISS/YAGNI）：
- slot 行数可配置（DB 初始化补齐 1..MAX_ROWS，展示为前 N 行）
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
    UPAY_BASE_URL,
    UPAY_NOTIFY_PATH,
    UPAY_REDIRECT_PATH,
    UPAY_SECRET_KEY,
)
from database.db_manager import get_db
from utils import runtime_settings
from utils.upay_pro_client import check_status as upay_check_status
from utils.upay_pro_client import create_order as upay_create_order
from utils.upay_pro_client import normalize_amount

logger = logging.getLogger(__name__)
ALLOWED_BUTTON_STYLES = ("primary", "success", "danger")


@dataclass(frozen=True)
class SlotAdPlan:
    sku_id: str
    days: int
    amount: Decimal


def get_plans() -> List[SlotAdPlan]:
    plans: List[SlotAdPlan] = []
    for p in runtime_settings.slot_ad_plans():
        plans.append(SlotAdPlan(sku_id=str(p.sku_id), days=int(p.days), amount=p.amount))
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
    max_len = int(runtime_settings.slot_ad_button_text_max_len())
    if len(t) > max_len:
        raise ValueError(f"按钮文案过长，最多 {max_len} 字符")
    return t


def validate_button_url(url: str) -> str:
    u = (url or "").strip()
    if not u:
        raise ValueError("链接不能为空")
    max_len = int(runtime_settings.slot_ad_url_max_len())
    if len(u) > max_len:
        raise ValueError(f"链接过长，最多 {max_len} 字符")
    parsed = urlparse(u)
    if parsed.scheme.lower() != "https":
        raise ValueError("仅允许 https:// 链接")
    if not parsed.netloc:
        raise ValueError("链接格式无效（缺少域名）")
    return u


def validate_button_style(style: Optional[str]) -> Optional[str]:
    s = (style or "").strip().lower()
    if not s or s in ("none", "off", "无"):
        return None
    if s not in ALLOWED_BUTTON_STYLES:
        raise ValueError("按钮样式仅支持 primary/success/danger")
    return s


def validate_icon_custom_emoji_id(icon_custom_emoji_id: Optional[str]) -> Optional[str]:
    v = (icon_custom_emoji_id or "").strip()
    if not v or v.lower() in ("none", "off", "无"):
        return None
    if len(v) > 64:
        raise ValueError("会员表情 ID 过长（最多 64 字符）")
    if not v.isdigit():
        raise ValueError("会员表情 ID 必须是数字字符串")
    return v


def _normalize_advanced_fields_for_runtime(
    *,
    style: Optional[str],
    icon_custom_emoji_id: Optional[str],
) -> Tuple[Optional[str], Optional[str]]:
    cleaned_style = validate_button_style(style)
    cleaned_icon = validate_icon_custom_emoji_id(icon_custom_emoji_id)

    if not runtime_settings.slot_ad_allow_style():
        cleaned_style = None
    if not runtime_settings.slot_ad_allow_custom_emoji():
        cleaned_icon = None
    if runtime_settings.slot_ad_custom_emoji_mode() == "off":
        cleaned_icon = None

    return cleaned_style, cleaned_icon


def parse_default_buttons_lines(raw: str) -> List[Dict[str, Any]]:
    """
    解析默认按钮列表（用于后台输入）：
    - 每行一条：<text> | <https://url> [| style] [| icon_custom_emoji_id]
    - 空行忽略
    """
    out: List[Dict[str, Any]] = []
    lines = [ln.strip() for ln in (raw or "").splitlines()]
    for ln in lines:
        if not ln:
            continue
        if "|" not in ln:
            raise ValueError("默认按钮列表格式错误：每行需使用 “文案 | https://链接 [| style] [| emoji_id]”")
        parts = [x.strip() for x in ln.split("|")]
        if len(parts) < 2 or len(parts) > 4:
            raise ValueError("默认按钮列表格式错误：每行仅支持 2~4 段（文案 | 链接 | 样式 | 会员表情ID）")
        text = parts[0]
        url = parts[1]
        style = validate_button_style(parts[2]) if len(parts) >= 3 else None
        icon_custom_emoji_id = validate_icon_custom_emoji_id(parts[3]) if len(parts) >= 4 else None
        item: Dict[str, Any] = {
            "text": validate_button_text(text),
            "url": validate_button_url(url),
        }
        if style:
            item["style"] = style
        if icon_custom_emoji_id:
            item["icon_custom_emoji_id"] = icon_custom_emoji_id
        out.append(item)
    return out


def _safe_parse_default_buttons_json(raw: Optional[str]) -> List[Dict[str, Any]]:
    if not raw:
        return []
    try:
        data = json.loads(str(raw))
    except Exception:
        logger.warning("ad_slots.default_buttons_json 解析失败，将忽略该字段", exc_info=True)
        return []
    if not isinstance(data, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        text = (item.get("text") or item.get("label") or "").strip()
        url = (item.get("url") or "").strip()
        if not text or not url:
            continue
        style = None
        icon_custom_emoji_id = None
        try:
            style = validate_button_style(item.get("style"))
        except Exception:
            style = None
        try:
            icon_custom_emoji_id = validate_icon_custom_emoji_id(item.get("icon_custom_emoji_id"))
        except Exception:
            icon_custom_emoji_id = None
        # DB 中的脏数据不应导致渲染失败：这里不做严格校验，只做最小约束
        entry: Dict[str, Any] = {"text": str(text), "url": str(url)}
        if style:
            entry["style"] = style
        if icon_custom_emoji_id:
            entry["icon_custom_emoji_id"] = icon_custom_emoji_id
        out.append(entry)
    return out


async def get_slot_defaults() -> Dict[int, Dict[str, Any]]:
    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            "SELECT slot_id, default_text, default_url, default_buttons_json, sell_enabled FROM ad_slots ORDER BY slot_id"
        )
        rows = await cursor.fetchall()
        out: Dict[int, Dict[str, Any]] = {}
        for r in rows:
            default_buttons = _safe_parse_default_buttons_json(r["default_buttons_json"])
            if not default_buttons and r["default_text"] and r["default_url"]:
                default_buttons = [{"text": str(r["default_text"]), "url": str(r["default_url"])}]
            out[int(r["slot_id"])] = {
                "default_text": r["default_text"],
                "default_url": r["default_url"],
                "default_buttons": default_buttons,
                "sell_enabled": bool(int(r["sell_enabled"])),
            }
        return out


async def set_slot_default(slot_id: int, default_text: Optional[str], default_url: Optional[str]) -> None:
    default_buttons_json = None
    if default_text and default_url:
        default_buttons_json = json.dumps([{"text": str(default_text), "url": str(default_url)}], ensure_ascii=False)
    now = time.time()
    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            "UPDATE ad_slots SET default_text = ?, default_url = ?, default_buttons_json = ?, updated_at = ? WHERE slot_id = ?",
            (default_text, default_url, default_buttons_json, now, int(slot_id)),
        )


async def set_slot_default_buttons(slot_id: int, default_buttons: List[Dict[str, Any]]) -> None:
    buttons = default_buttons or []
    first = buttons[0] if buttons else None
    default_text = (first.get("text") if isinstance(first, dict) else None) if first else None
    default_url = (first.get("url") if isinstance(first, dict) else None) if first else None
    default_buttons_json = json.dumps(buttons, ensure_ascii=False) if buttons else None

    now = time.time()
    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            "UPDATE ad_slots SET default_text = ?, default_url = ?, default_buttons_json = ?, updated_at = ? WHERE slot_id = ?",
            (default_text, default_url, default_buttons_json, now, int(slot_id)),
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
            SELECT o.*, c.button_text AS button_text, c.button_url AS button_url,
                   c.button_style AS button_style, c.icon_custom_emoji_id AS icon_custom_emoji_id
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
            SELECT o.*, c.button_text AS button_text, c.button_url AS button_url,
                   c.button_style AS button_style, c.icon_custom_emoji_id AS icon_custom_emoji_id
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
            SELECT o.*, c.button_text AS button_text, c.button_url AS button_url,
                   c.button_style AS button_style, c.icon_custom_emoji_id AS icon_custom_emoji_id
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
            SELECT o.*, c.button_text AS button_text, c.button_url AS button_url,
                   c.button_style AS button_style, c.icon_custom_emoji_id AS icon_custom_emoji_id
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
            SELECT o.*, c.button_text AS button_text, c.button_url AS button_url,
                   c.button_style AS button_style, c.icon_custom_emoji_id AS icon_custom_emoji_id
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


def _build_slot_url_button(
    *,
    text: str,
    url: str,
    style: Optional[str] = None,
    icon_custom_emoji_id: Optional[str] = None,
) -> InlineKeyboardButton:
    style, icon_custom_emoji_id = _normalize_advanced_fields_for_runtime(
        style=style,
        icon_custom_emoji_id=icon_custom_emoji_id,
    )
    kwargs: Dict[str, Any] = {"url": str(url)}
    api_kwargs: Dict[str, Any] = {}
    if style:
        api_kwargs["style"] = style
    if icon_custom_emoji_id:
        api_kwargs["icon_custom_emoji_id"] = icon_custom_emoji_id
    if api_kwargs:
        kwargs["api_kwargs"] = api_kwargs
    return InlineKeyboardButton(str(text), **kwargs)


def markup_has_custom_emoji(markup: Optional[InlineKeyboardMarkup]) -> bool:
    if not markup or not getattr(markup, "inline_keyboard", None):
        return False
    for row in (markup.inline_keyboard or []):
        for b in (row or []):
            try:
                if (b.to_dict() or {}).get("icon_custom_emoji_id"):
                    return True
            except Exception:
                continue
    return False


def strip_custom_emoji_from_markup(markup: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for row in (markup.inline_keyboard or []):
        new_row: List[InlineKeyboardButton] = []
        for b in (row or []):
            try:
                data = b.to_dict() or {}
            except Exception:
                new_row.append(b)
                continue

            text = str(data.get("text") or "")
            has_custom = bool(data.get("icon_custom_emoji_id"))
            style = data.get("style")

            if not has_custom:
                new_row.append(b)
                continue

            if data.get("url"):
                new_row.append(
                    _build_slot_url_button(
                        text=text,
                        url=str(data.get("url")),
                        style=style,
                        icon_custom_emoji_id=None,
                    )
                )
                continue

            if data.get("callback_data") is not None:
                kwargs: Dict[str, Any] = {"callback_data": data.get("callback_data")}
                if style:
                    kwargs["api_kwargs"] = {"style": str(style)}
                new_row.append(InlineKeyboardButton(text, **kwargs))
                continue

            new_row.append(b)
        if new_row:
            rows.append(new_row)
    return InlineKeyboardMarkup(rows)


def build_channel_keyboard(
    *,
    slot_defaults: Dict[int, Dict[str, Any]],
    active_orders: Dict[int, Dict[str, Any]],
) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    active_rows_count = int(runtime_settings.slot_ad_active_rows_count())
    for slot_id in sorted(slot_defaults.keys()):
        if active_rows_count >= 0 and int(slot_id) > active_rows_count:
            break
        active = active_orders.get(int(slot_id))
        if active:
            try:
                rows.append([
                    _build_slot_url_button(
                        text=str(active["button_text"]),
                        url=str(active["button_url"]),
                        style=(active.get("button_style") or None),
                        icon_custom_emoji_id=(active.get("icon_custom_emoji_id") or None),
                    )
                ])
            except Exception:
                rows.append([InlineKeyboardButton(str(active["button_text"]), url=str(active["button_url"]))])
            continue

        slot = slot_defaults[int(slot_id)]
        sell_enabled = bool(slot.get("sell_enabled"))
        default_buttons = slot.get("default_buttons") or []
        default_text = slot.get("default_text")
        default_url = slot.get("default_url")
        buy_url = _buy_deeplink(slot_id) if sell_enabled else None

        line: List[InlineKeyboardButton] = []
        if default_buttons:
            # 预留空间给“购买”按钮，避免超出 Telegram 单行上限
            max_defaults = 7 if sell_enabled else 8
            for btn in default_buttons[:max_defaults]:
                try:
                    text = str(btn.get("text") or "").strip()
                    url = str(btn.get("url") or "").strip()
                    if text and url:
                        line.append(
                            _build_slot_url_button(
                                text=text,
                                url=url,
                                style=(btn.get("style") or None),
                                icon_custom_emoji_id=(btn.get("icon_custom_emoji_id") or None),
                            )
                        )
                except Exception:
                    continue
        elif default_text and default_url:
            line.append(_build_slot_url_button(text=str(default_text), url=str(default_url)))
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


async def create_creative(
    *,
    user_id: int,
    button_text: str,
    button_url: str,
    button_style: Optional[str] = None,
    icon_custom_emoji_id: Optional[str] = None,
    ai_review: Optional[Dict[str, Any]] = None,
) -> int:
    now = time.time()
    style, icon_custom_emoji_id = _normalize_advanced_fields_for_runtime(
        style=button_style,
        icon_custom_emoji_id=icon_custom_emoji_id,
    )
    ai_review_json = json.dumps(ai_review, ensure_ascii=False) if ai_review else None
    ai_passed = None
    if ai_review and "passed" in ai_review:
        ai_passed = 1 if bool(ai_review.get("passed")) else 0

    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            """
            INSERT INTO slot_ad_creatives(
                user_id, button_text, button_url, button_style, icon_custom_emoji_id,
                ai_review_result, ai_review_passed, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (int(user_id), button_text, button_url, style, icon_custom_emoji_id, ai_review_json, ai_passed, now),
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
    protect_start = end_at - (int(runtime_settings.slot_ad_renew_protect_days()) * 86400)

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
    currency: str = runtime_settings.slot_ad_currency(),
    pay_type: Optional[str] = None,
) -> Dict[str, Any]:
    if not runtime_settings.slot_ad_enabled():
        raise ValueError("按钮广告位功能未开启")
    active_rows_count = int(runtime_settings.slot_ad_active_rows_count())
    if int(slot_id) <= 0 or int(slot_id) > active_rows_count:
        raise ValueError(f"该广告位（{int(slot_id)}）当前未启用（启用范围：1..{active_rows_count}）")
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
    expires_at = created_at + (int(runtime_settings.pay_expire_minutes()) * 60)
    type_ = pay_type or runtime_settings.upay_default_type()

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
                   c.button_text AS button_text, c.button_url AS button_url,
                   c.button_style AS button_style, c.icon_custom_emoji_id AS icon_custom_emoji_id
            FROM slot_ad_orders o
            JOIN slot_ad_creatives c ON c.id = o.creative_id
            WHERE o.out_trade_no = ?
            LIMIT 1
            """,
            (str(out_trade_no),),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

def _day_key(ts: float) -> str:
    return time.strftime("%Y%m%d", time.localtime(float(ts)))


async def get_slot_order_for_edit(out_trade_no: str) -> Optional[Dict[str, Any]]:
    """
    获取可用于“编辑素材”的订单信息（含当前素材与归属信息）。
    """
    ot = str(out_trade_no or "").strip()
    if not ot:
        return None
    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            """
            SELECT o.out_trade_no, o.slot_id, o.buyer_user_id, o.status, o.start_at, o.end_at, o.creative_id,
                   c.button_text AS button_text, c.button_url AS button_url,
                   c.button_style AS button_style, c.icon_custom_emoji_id AS icon_custom_emoji_id
            FROM slot_ad_orders o
            JOIN slot_ad_creatives c ON c.id = o.creative_id
            WHERE o.out_trade_no = ?
            LIMIT 1
            """,
            (ot,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def _count_order_edits_today(
    *,
    out_trade_no: str,
    day_key: str,
    editor_type: str,
    editor_user_id: Optional[int],
) -> int:
    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            """
            SELECT COUNT(1) AS c
            FROM slot_ad_order_edits
            WHERE out_trade_no = ?
              AND day_key = ?
              AND editor_type = ?
              AND (editor_user_id IS ? OR editor_user_id = ?)
            """,
            (str(out_trade_no), str(day_key), str(editor_type), editor_user_id, editor_user_id),
        )
        row = await cursor.fetchone()
        try:
            return int(row["c"] or 0) if row else 0
        except Exception:
            return 0


async def user_can_edit_order_today(*, out_trade_no: str, user_id: int, now: Optional[float] = None) -> Dict[str, Any]:
    """
    用于 UI 提前提示：返回剩余次数与是否可编辑（不做素材校验/更新）。
    """
    t = float(now if now is not None else time.time())
    order = await get_slot_order_for_edit(out_trade_no)
    if not order:
        return {"ok": False, "reason": "not_found"}
    if int(order.get("buyer_user_id") or 0) != int(user_id):
        return {"ok": False, "reason": "no_permission"}
    if str(order.get("status") or "") != "active":
        return {"ok": False, "reason": "not_active"}
    end_at = order.get("end_at")
    if end_at is None or float(end_at) <= t:
        return {"ok": False, "reason": "expired"}

    limit = int(runtime_settings.slot_ad_edit_limit_per_order_per_day())
    if limit <= 0:
        return {"ok": True, "remaining": None, "limit": 0}
    day = _day_key(t)
    used = await _count_order_edits_today(out_trade_no=str(out_trade_no), day_key=day, editor_type="user_bot", editor_user_id=int(user_id))
    remaining = max(0, limit - int(used))
    return {"ok": remaining > 0, "remaining": remaining, "limit": limit}


async def _create_creative_in_tx(
    *,
    cursor,
    user_id: int,
    button_text: str,
    button_url: str,
    button_style: Optional[str],
    icon_custom_emoji_id: Optional[str],
    ai_review: Optional[Dict[str, Any]],
    now: float,
) -> int:
    style, icon_custom_emoji_id = _normalize_advanced_fields_for_runtime(
        style=button_style,
        icon_custom_emoji_id=icon_custom_emoji_id,
    )
    ai_review_json = json.dumps(ai_review, ensure_ascii=False) if ai_review else None
    ai_passed = None
    if ai_review and "passed" in ai_review:
        ai_passed = 1 if bool(ai_review.get("passed")) else 0
    await cursor.execute(
        """
        INSERT INTO slot_ad_creatives(
            user_id, button_text, button_url, button_style, icon_custom_emoji_id,
            ai_review_result, ai_review_passed, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(user_id),
            str(button_text),
            str(button_url),
            style,
            icon_custom_emoji_id,
            ai_review_json,
            ai_passed,
            float(now),
        ),
    )
    return int(cursor.lastrowid)


async def _update_order_creative_with_audit_in_tx(
    *,
    cursor,
    out_trade_no: str,
    new_creative_id: int,
    old_creative_id: int,
    editor_type: str,
    editor_user_id: Optional[int],
    note: str,
    now: float,
) -> None:
    await cursor.execute(
        "UPDATE slot_ad_orders SET creative_id = ? WHERE out_trade_no = ?",
        (int(new_creative_id), str(out_trade_no)),
    )
    await cursor.execute(
        """
        INSERT INTO slot_ad_order_edits(out_trade_no, day_key, editor_type, editor_user_id, old_creative_id, new_creative_id, note, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(out_trade_no),
            _day_key(float(now)),
            str(editor_type),
            int(editor_user_id) if editor_user_id is not None else None,
            int(old_creative_id) if old_creative_id is not None else None,
            int(new_creative_id),
            (str(note or "").strip()[:200] or None),
            float(now),
        ),
    )


async def update_slot_ad_order_creative_by_user(
    *,
    out_trade_no: str,
    user_id: int,
    button_text: str,
    button_url: str,
    button_style: Optional[str] = None,
    icon_custom_emoji_id: Optional[str] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """
    用户在广告有效期内自助更新素材（每日限额）。
    - 允许 start_at > now（已支付待生效）
    - 仅允许修改自己的订单
    """
    t = float(now if now is not None else time.time())
    bt = validate_button_text(button_text)
    bu = validate_button_url(button_url)
    style, icon_custom_emoji_id = _normalize_advanced_fields_for_runtime(
        style=button_style,
        icon_custom_emoji_id=icon_custom_emoji_id,
    )

    from utils.ad_risk_reviewer import review_ad_risk

    review = await review_ad_risk(button_text=bt, button_url=bu)
    if not review.passed:
        raise ValueError(f"风控拒绝：{review.category}，原因：{review.reason}")

    limit = int(runtime_settings.slot_ad_edit_limit_per_order_per_day())
    day = _day_key(t)

    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute("SELECT * FROM slot_ad_orders WHERE out_trade_no = ? LIMIT 1", (str(out_trade_no),))
        order = await cursor.fetchone()
        if not order:
            raise ValueError("订单不存在")
        if int(order["buyer_user_id"]) != int(user_id):
            raise ValueError("无权限")
        if str(order["status"] or "") != "active":
            raise ValueError("订单状态不允许修改")
        end_at = order["end_at"]
        if end_at is None or float(end_at) <= t:
            raise ValueError("订单已到期")

        if limit > 0:
            await cursor.execute(
                """
                SELECT COUNT(1) AS c
                FROM slot_ad_order_edits
                WHERE out_trade_no = ? AND day_key = ? AND editor_type = 'user_bot' AND editor_user_id = ?
                """,
                (str(out_trade_no), str(day), int(user_id)),
            )
            row = await cursor.fetchone()
            used = int(row["c"] or 0) if row else 0
            if used >= limit:
                raise ValueError(f"今日已达到修改次数上限（{limit} 次/单/天）")

        old_creative_id = int(order["creative_id"])
        new_creative_id = await _create_creative_in_tx(
            cursor=cursor,
            user_id=int(user_id),
            button_text=bt,
            button_url=bu,
            button_style=style,
            icon_custom_emoji_id=icon_custom_emoji_id,
            ai_review=review.to_dict(),
            now=t,
        )
        await _update_order_creative_with_audit_in_tx(
            cursor=cursor,
            out_trade_no=str(out_trade_no),
            new_creative_id=int(new_creative_id),
            old_creative_id=int(old_creative_id),
            editor_type="user_bot",
            editor_user_id=int(user_id),
            note="user_edit",
            now=t,
        )
        await conn.commit()
        return {
            "out_trade_no": str(out_trade_no),
            "slot_id": int(order["slot_id"]),
            "buyer_user_id": int(order["buyer_user_id"]),
            "old_creative_id": int(old_creative_id),
            "new_creative_id": int(new_creative_id),
        }


async def update_slot_ad_order_creative_by_admin(
    *,
    out_trade_no: str,
    button_text: str,
    button_url: str,
    button_style: Optional[str] = None,
    icon_custom_emoji_id: Optional[str] = None,
    force: bool = False,
    note: str = "admin_web_edit",
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """
    管理员在 Web 后台手动更新素材。
    - 默认同样受每日限额约束（force=True 可忽略限额）
    """
    t = float(now if now is not None else time.time())
    bt = validate_button_text(button_text)
    bu = validate_button_url(button_url)
    style, icon_custom_emoji_id = _normalize_advanced_fields_for_runtime(
        style=button_style,
        icon_custom_emoji_id=icon_custom_emoji_id,
    )

    from utils.ad_risk_reviewer import review_ad_risk

    review = await review_ad_risk(button_text=bt, button_url=bu)
    if not review.passed:
        raise ValueError(f"风控拒绝：{review.category}，原因：{review.reason}")

    limit = int(runtime_settings.slot_ad_edit_limit_per_order_per_day())
    day = _day_key(t)

    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute("SELECT * FROM slot_ad_orders WHERE out_trade_no = ? LIMIT 1", (str(out_trade_no),))
        order = await cursor.fetchone()
        if not order:
            raise ValueError("订单不存在")
        if str(order["status"] or "") != "active":
            raise ValueError("订单状态不允许修改")
        end_at = order["end_at"]
        if end_at is None or float(end_at) <= t:
            raise ValueError("订单已到期")

        if (not force) and limit > 0:
            await cursor.execute(
                "SELECT COUNT(1) AS c FROM slot_ad_order_edits WHERE out_trade_no = ? AND day_key = ?",
                (str(out_trade_no), str(day)),
            )
            row = await cursor.fetchone()
            used = int(row["c"] or 0) if row else 0
            if used >= limit:
                raise ValueError(f"今日已达到修改次数上限（{limit} 次/单/天）。如需强制修改，请勾选“强制”。")

        old_creative_id = int(order["creative_id"])
        buyer_user_id = int(order["buyer_user_id"])
        new_creative_id = await _create_creative_in_tx(
            cursor=cursor,
            user_id=buyer_user_id,
            button_text=bt,
            button_url=bu,
            button_style=style,
            icon_custom_emoji_id=icon_custom_emoji_id,
            ai_review=review.to_dict(),
            now=t,
        )
        await _update_order_creative_with_audit_in_tx(
            cursor=cursor,
            out_trade_no=str(out_trade_no),
            new_creative_id=int(new_creative_id),
            old_creative_id=int(old_creative_id),
            editor_type="admin_web",
            editor_user_id=None,
            note=note,
            now=t,
        )
        await conn.commit()
        return {
            "out_trade_no": str(out_trade_no),
            "slot_id": int(order["slot_id"]),
            "buyer_user_id": buyer_user_id,
            "old_creative_id": int(old_creative_id),
            "new_creative_id": int(new_creative_id),
        }


async def refresh_last_scheduled_message_keyboard(*, bot, now: Optional[float] = None) -> bool:
    """
    立即刷新“最近一次定时消息”的按钮键盘（仅改 reply_markup，不改正文）。
    返回 True 表示已尝试刷新（且具备 last_message_id）；False 表示无可刷新目标。
    """
    from utils.scheduled_publish_service import get_config as get_sched_config

    sched = await get_sched_config()
    if not sched.last_message_chat_id or not sched.last_message_id:
        return False
    t = float(now if now is not None else time.time())
    slot_defaults = await get_slot_defaults()
    active = await get_active_orders(now=t)
    keyboard = build_channel_keyboard(slot_defaults=slot_defaults, active_orders=active)
    try:
        await bot.edit_message_reply_markup(
            chat_id=int(sched.last_message_chat_id),
            message_id=int(sched.last_message_id),
            reply_markup=keyboard,
        )
    except Exception as e:
        if runtime_settings.slot_ad_custom_emoji_mode() == "auto" and markup_has_custom_emoji(keyboard):
            logger.warning(f"刷新键盘时 custom emoji 可能不可用，自动降级重试: {e}")
            await bot.edit_message_reply_markup(
                chat_id=int(sched.last_message_chat_id),
                message_id=int(sched.last_message_id),
                reply_markup=strip_custom_emoji_from_markup(keyboard),
            )
        else:
            raise
    return True


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
