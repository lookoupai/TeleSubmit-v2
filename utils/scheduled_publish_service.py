"""
定时发布服务（Scheduled Publish）

设计目标：
- 调度参数与消息正文从数据库读取，支持热更新
- 由 JobQueue 周期性 tick，发现 next_run_at 到期即发布
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

from telegram.constants import ParseMode

from config.settings import CHANNEL_ID
from database.db_manager import get_db
from utils.slot_ad_service import build_channel_keyboard, get_active_orders, get_slot_defaults

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScheduledPublishConfig:
    enabled: bool
    schedule_type: str
    schedule_payload: Dict[str, Any]
    message_text: str
    auto_pin: bool
    delete_prev: bool
    next_run_at: Optional[float]
    last_run_at: Optional[float]
    last_message_chat_id: Optional[int]
    last_message_id: Optional[int]


async def get_config() -> ScheduledPublishConfig:
    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute("SELECT * FROM scheduled_publish_config WHERE id = 1")
        row = await cursor.fetchone()
        if not row:
            return ScheduledPublishConfig(
                enabled=False,
                schedule_type="daily_at",
                schedule_payload={},
                message_text="",
                auto_pin=False,
                delete_prev=False,
                next_run_at=None,
                last_run_at=None,
                last_message_chat_id=None,
                last_message_id=None,
            )
        row_keys = set(getattr(row, "keys", lambda: [])())
        payload = {}
        try:
            payload = json.loads(row["schedule_payload"] or "{}")
            if not isinstance(payload, dict):
                payload = {}
        except Exception:
            payload = {}
        return ScheduledPublishConfig(
            enabled=bool(int(row["enabled"])),
            schedule_type=str(row["schedule_type"] or "daily_at"),
            schedule_payload=payload,
            message_text=str(row["message_text"] or ""),
            auto_pin=bool(int(row["auto_pin"])) if "auto_pin" in row_keys else False,
            delete_prev=bool(int(row["delete_prev"])) if "delete_prev" in row_keys else False,
            next_run_at=float(row["next_run_at"]) if row["next_run_at"] is not None else None,
            last_run_at=float(row["last_run_at"]) if row["last_run_at"] is not None else None,
            last_message_chat_id=int(row["last_message_chat_id"]) if row["last_message_chat_id"] is not None else None,
            last_message_id=int(row["last_message_id"]) if row["last_message_id"] is not None else None,
        )


def _parse_hhmm(value: str) -> Tuple[int, int]:
    s = (value or "").strip()
    if ":" not in s:
        raise ValueError("时间格式应为 HH:MM")
    hh_str, mm_str = s.split(":", 1)
    hh = int(hh_str)
    mm = int(mm_str)
    if hh < 0 or hh > 23 or mm < 0 or mm > 59:
        raise ValueError("时间范围无效")
    return hh, mm


def compute_next_run_at(*, now: float, schedule_type: str, payload: Dict[str, Any], last_run_at: Optional[float] = None) -> float:
    st = (schedule_type or "daily_at").strip().lower()
    dt_now = datetime.fromtimestamp(float(now))

    if st == "daily_at":
        hhmm = str(payload.get("time") or "09:00")
        hh, mm = _parse_hhmm(hhmm)
        candidate = dt_now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= dt_now:
            candidate = candidate + timedelta(days=1)
        return candidate.timestamp()

    if st == "every_n_hours":
        hours = int(payload.get("hours") or 24)
        if hours <= 0:
            raise ValueError("间隔小时数必须 > 0")
        base = datetime.fromtimestamp(float(last_run_at)) if last_run_at else dt_now
        candidate = base + timedelta(hours=hours)
        if candidate <= dt_now:
            candidate = dt_now + timedelta(hours=hours)
        return candidate.timestamp()

    raise ValueError(f"不支持的 schedule_type: {schedule_type}")


async def update_config_fields(**fields: Any) -> None:
    """
    更新 scheduled_publish_config.id=1 的部分字段。
    """
    if not fields:
        return
    now = time.time()
    fields = dict(fields)
    fields["updated_at"] = now

    columns = ", ".join([f"{k} = ?" for k in fields.keys()])
    values = list(fields.values())
    values.append(1)

    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute(f"UPDATE scheduled_publish_config SET {columns} WHERE id = ?", tuple(values))


def render_message_template(message_text: str, now: Optional[float] = None) -> str:
    t = float(now if now is not None else time.time())
    dt = datetime.fromtimestamp(t)
    return (
        (message_text or "")
        .replace("{date}", dt.strftime("%Y-%m-%d"))
        .replace("{datetime}", dt.strftime("%Y-%m-%d %H:%M:%S"))
    )


async def get_next_run_at_for_ads(now: Optional[float] = None) -> Optional[float]:
    """
    Slot Ads 生效起点：默认取 scheduled_publish_config.next_run_at（若启用且在未来）。
    """
    cfg = await get_config()
    t = float(now if now is not None else time.time())
    if not cfg.enabled or not cfg.next_run_at:
        return None
    if cfg.next_run_at <= t:
        # 如果 next_run_at 已过期，按当前配置重新计算一个未来时间点
        try:
            return compute_next_run_at(now=t, schedule_type=cfg.schedule_type, payload=cfg.schedule_payload, last_run_at=cfg.last_run_at)
        except Exception:
            return None
    return cfg.next_run_at


async def scheduled_publish_tick(context) -> None:
    """
    JobQueue tick：到期则发布；并在发布时附加 Slot Ads 键盘。
    """
    try:
        cfg = await get_config()
    except Exception as e:
        logger.error(f"读取定时发布配置失败: {e}", exc_info=True)
        return

    if not cfg.enabled:
        return

    now = time.time()
    if not cfg.next_run_at:
        try:
            next_run_at = compute_next_run_at(now=now, schedule_type=cfg.schedule_type, payload=cfg.schedule_payload, last_run_at=cfg.last_run_at)
        except Exception as e:
            logger.error(f"计算 next_run_at 失败: {e}")
            return
        await update_config_fields(next_run_at=float(next_run_at))
        return

    if float(cfg.next_run_at) > now:
        return

    # 构造键盘（按发布瞬间快照）
    try:
        slot_defaults = await get_slot_defaults()
        active = await get_active_orders(now=now)
        keyboard = build_channel_keyboard(slot_defaults=slot_defaults, active_orders=active)
    except Exception as e:
        logger.error(f"构造广告位键盘失败，将降级为无键盘: {e}", exc_info=True)
        keyboard = None

    text = render_message_template(cfg.message_text, now=now).strip()
    if not text:
        text = render_message_template("📌 定时消息 {datetime}", now=now)

    try:
        try:
            sent = await context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=keyboard,
            )
        except Exception:
            # 降级：避免 HTML 格式错误导致整条定时消息丢失
            sent = await context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=text,
                disable_web_page_preview=True,
                reply_markup=keyboard,
            )
    except Exception as e:
        logger.error(f"定时消息发送失败: {e}", exc_info=True)
        return

    prev_chat_id = cfg.last_message_chat_id
    prev_message_id = cfg.last_message_id

    if cfg.auto_pin:
        try:
            await context.bot.pin_chat_message(
                chat_id=int(sent.chat_id),
                message_id=int(sent.message_id),
                disable_notification=True,
            )
        except Exception as e:
            logger.warning(f"定时消息置顶失败（可忽略）: {e}", exc_info=True)

    if cfg.delete_prev and prev_chat_id and prev_message_id:
        if int(prev_chat_id) != int(sent.chat_id) or int(prev_message_id) != int(sent.message_id):
            try:
                await context.bot.delete_message(chat_id=int(prev_chat_id), message_id=int(prev_message_id))
            except Exception as e:
                logger.warning(f"删除上一条定时消息失败（可忽略）: {e}", exc_info=True)

    try:
        next_run_at = compute_next_run_at(now=now, schedule_type=cfg.schedule_type, payload=cfg.schedule_payload, last_run_at=now)
    except Exception as e:
        logger.error(f"计算下一次 next_run_at 失败: {e}")
        next_run_at = None

    await update_config_fields(
        last_run_at=float(now),
        next_run_at=float(next_run_at) if next_run_at else None,
        last_message_chat_id=int(sent.chat_id),
        last_message_id=int(sent.message_id),
    )
