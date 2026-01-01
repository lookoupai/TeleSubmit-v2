"""
兜底定时发布服务（Fallback Publish）

需求：
- 到达设定时间点时，若“当天没有任何投稿发布（published_posts）”，则发布一条预存消息
- 预存消息支持多条：不重复随机，直到用完再重置
- 按服务器本地时区计算“当天”
- 若错过触发时间（超过 miss_tolerance_seconds），则跳过并等待下一天
- 发布后的按钮与投稿消息一致：评分键盘（如启用）+ CUSTOM_BUTTON_ROWS
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from telegram.constants import ParseMode

from config.settings import CHANNEL_ID
from database.db_manager import get_db
from ui.keyboards import Keyboards
from utils import runtime_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FallbackPublishConfig:
    enabled: bool
    schedule_type: str
    schedule_payload: Dict[str, Any]
    next_run_at: Optional[float]
    last_run_at: Optional[float]
    cycle_id: int
    miss_tolerance_seconds: int


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


def compute_next_run_at(*, now: float, schedule_type: str, payload: Dict[str, Any]) -> float:
    st = (schedule_type or "daily_at").strip().lower()
    dt_now = datetime.fromtimestamp(float(now))

    if st != "daily_at":
        raise ValueError(f"不支持的 schedule_type: {schedule_type}")

    hhmm = str(payload.get("time") or "23:00")
    hh, mm = _parse_hhmm(hhmm)
    candidate = dt_now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if candidate <= dt_now:
        candidate = candidate + timedelta(days=1)
    return candidate.timestamp()


def _now_ts() -> float:
    return float(time.time())


def _safe_json_loads_dict(raw: Any) -> Dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _normalize_domain(value: str) -> str:
    s = (value or "").strip()
    if not s:
        return ""
    if "://" not in s:
        parsed = urlparse("https://" + s)
    else:
        parsed = urlparse(s)
    host = (parsed.netloc or "").strip()
    if not host:
        host = (parsed.path or "").split("/", 1)[0].strip()
    host = host.split("@")[-1]
    host = host.split(":")[0]
    host = host.strip().lower()
    if host.startswith("www."):
        host = host[4:]
    return host


_TG_USERNAME_RE = re.compile(r"^[a-z0-9_]{5,32}$", re.IGNORECASE)


def _normalize_tg_username(value: str) -> str:
    s = (value or "").strip()
    if not s:
        return ""
    if s.startswith("@"):
        s = s[1:].strip()
    if "t.me/" in s or "telegram.me/" in s:
        if "://" not in s:
            parsed = urlparse("https://" + s)
        else:
            parsed = urlparse(s)
        path = (parsed.path or "").strip("/")
        if path:
            s = path.split("/", 1)[0].strip()
    s = s.strip().lstrip("@").strip().lower()
    if not s:
        return ""
    if not _TG_USERNAME_RE.match(s):
        raise ValueError("TG 频道用户名格式无效（示例：@channel_username）")
    return s


async def get_config() -> FallbackPublishConfig:
    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute("SELECT * FROM fallback_publish_config WHERE id = 1")
        row = await cursor.fetchone()
        if not row:
            return FallbackPublishConfig(
                enabled=False,
                schedule_type="daily_at",
                schedule_payload={},
                next_run_at=None,
                last_run_at=None,
                cycle_id=1,
                miss_tolerance_seconds=300,
            )
        payload = _safe_json_loads_dict(row["schedule_payload"])
        return FallbackPublishConfig(
            enabled=bool(int(row["enabled"])),
            schedule_type=str(row["schedule_type"] or "daily_at"),
            schedule_payload=payload,
            next_run_at=float(row["next_run_at"]) if row["next_run_at"] is not None else None,
            last_run_at=float(row["last_run_at"]) if row["last_run_at"] is not None else None,
            cycle_id=int(row["cycle_id"] or 1),
            miss_tolerance_seconds=int(row["miss_tolerance_seconds"] or 300),
        )


async def update_config_fields(**fields: Any) -> None:
    if not fields:
        return
    now = _now_ts()
    fields = dict(fields)
    fields["updated_at"] = now

    columns = ", ".join([f"{k} = ?" for k in fields.keys()])
    values = list(fields.values())
    values.append(1)

    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute(f"UPDATE fallback_publish_config SET {columns} WHERE id = ?", tuple(values))


def render_message_template(message_text: str, now: Optional[float] = None) -> str:
    t = float(now if now is not None else _now_ts())
    dt = datetime.fromtimestamp(t)
    return (
        (message_text or "")
        .replace("{date}", dt.strftime("%Y-%m-%d"))
        .replace("{datetime}", dt.strftime("%Y-%m-%d %H:%M:%S"))
    )


async def _ensure_platform_subject_id(
    *,
    platform_domain: str,
    platform_tg_username: str,
    display_name: str,
) -> int:
    domain = _normalize_domain(platform_domain)
    tg_username = _normalize_tg_username(platform_tg_username) if platform_tg_username else ""

    if domain:
        subject_type = "domain"
        subject_key = domain
    elif tg_username:
        subject_type = "tg_username"
        subject_key = tg_username
    else:
        raise ValueError("必须提供平台官网域名或 TG 频道用户名之一")

    identifiers: List[Tuple[str, str]] = []
    if domain:
        identifiers.append(("domain", domain))
    if tg_username:
        identifiers.append(("tg_username", tg_username))

    async with get_db() as conn:
        cursor = await conn.cursor()

        await cursor.execute(
            "SELECT id FROM rating_subjects WHERE subject_type = ? AND subject_key = ?",
            (subject_type, subject_key),
        )
        row = await cursor.fetchone()
        if row:
            subject_id = int(row["id"])
        else:
            now_ts = datetime.now().timestamp()
            dn = (display_name or subject_key).strip() or subject_key
            await cursor.execute(
                """
                INSERT INTO rating_subjects
                (subject_type, subject_key, display_name, score_sum, vote_count, avg_score, created_at, updated_at)
                VALUES (?, ?, ?, 0, 0, 0.0, ?, ?)
                """,
                (subject_type, subject_key, dn, now_ts, now_ts),
            )
            subject_id = int(cursor.lastrowid)

        # 绑定标识：domain 优先为主键，但若 tg_username 提供，则作为附加标识绑定到同一 subject
        for ident_type, ident_value in identifiers:
            await cursor.execute(
                """
                SELECT subject_id FROM rating_subject_identifiers
                WHERE identifier_type = ? AND identifier_value = ?
                """,
                (ident_type, ident_value),
            )
            existing = await cursor.fetchone()
            if existing and int(existing["subject_id"]) != subject_id:
                raise ValueError(f"标识已被其他评分实体占用：{ident_type}={ident_value}")
            await cursor.execute(
                """
                INSERT OR IGNORE INTO rating_subject_identifiers
                (subject_id, identifier_type, identifier_value)
                VALUES (?, ?, ?)
                """,
                (subject_id, ident_type, ident_value),
            )

    return subject_id


async def add_pool_item(
    *,
    display_name: str,
    platform_domain: str,
    platform_tg_username: str,
    message_text: str,
    enabled: bool = True,
) -> int:
    subject_id = await _ensure_platform_subject_id(
        platform_domain=platform_domain,
        platform_tg_username=platform_tg_username,
        display_name=display_name,
    )
    now = _now_ts()
    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            """
            INSERT INTO fallback_message_pool
            (enabled, display_name, platform_domain, platform_tg_username, rating_subject_id,
             message_text, used_cycle_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                1 if enabled else 0,
                (display_name or "").strip() or None,
                _normalize_domain(platform_domain) or None,
                _normalize_tg_username(platform_tg_username) if platform_tg_username else None,
                int(subject_id),
                str(message_text or "").strip(),
                now,
                now,
            ),
        )
        return int(cursor.lastrowid)


async def update_pool_item(
    *,
    pool_id: int,
    display_name: str,
    platform_domain: str,
    platform_tg_username: str,
    message_text: str,
    enabled: bool,
) -> None:
    subject_id = await _ensure_platform_subject_id(
        platform_domain=platform_domain,
        platform_tg_username=platform_tg_username,
        display_name=display_name,
    )
    now = _now_ts()
    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            """
            UPDATE fallback_message_pool
            SET enabled = ?,
                display_name = ?,
                platform_domain = ?,
                platform_tg_username = ?,
                rating_subject_id = ?,
                message_text = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                1 if enabled else 0,
                (display_name or "").strip() or None,
                _normalize_domain(platform_domain) or None,
                _normalize_tg_username(platform_tg_username) if platform_tg_username else None,
                int(subject_id),
                str(message_text or "").strip(),
                now,
                int(pool_id),
            ),
        )


async def set_pool_enabled(*, pool_id: int, enabled: bool) -> None:
    now = _now_ts()
    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            "UPDATE fallback_message_pool SET enabled = ?, updated_at = ? WHERE id = ?",
            (1 if enabled else 0, now, int(pool_id)),
        )


async def delete_pool_item(*, pool_id: int) -> bool:
    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute("DELETE FROM fallback_message_pool WHERE id = ?", (int(pool_id),))
        return bool(cursor.rowcount and int(cursor.rowcount) > 0)


async def get_pool_item(pool_id: int) -> Optional[Dict[str, Any]]:
    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute("SELECT * FROM fallback_message_pool WHERE id = ?", (int(pool_id),))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def list_pool_items(*, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            """
            SELECT *
            FROM fallback_message_pool
            ORDER BY enabled DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (int(limit), int(offset)),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def count_pool_items(*, enabled_only: bool = False, unused_cycle_id: Optional[int] = None) -> int:
    clauses = []
    params: List[Any] = []
    if enabled_only:
        clauses.append("enabled = 1")
    if unused_cycle_id is not None:
        clauses.append("used_cycle_id != ?")
        params.append(int(unused_cycle_id))
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute(f"SELECT COUNT(*) AS cnt FROM fallback_message_pool {where}", tuple(params))
        row = await cursor.fetchone()
        return int(row["cnt"] or 0) if row else 0


async def list_recent_runs(*, limit: int = 20) -> List[Dict[str, Any]]:
    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            """
            SELECT *
            FROM fallback_publish_runs
            ORDER BY scheduled_at DESC, id DESC
            LIMIT ?
            """,
            (int(limit),),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def _try_start_run(*, run_key: str, scheduled_at: float) -> bool:
    async with get_db() as conn:
        cursor = await conn.cursor()
        try:
            await cursor.execute(
                """
                INSERT INTO fallback_publish_runs
                (run_key, scheduled_at, status, created_at)
                VALUES (?, ?, 'starting', ?)
                """,
                (str(run_key), float(scheduled_at), _now_ts()),
            )
            return True
        except Exception as e:
            # 幂等：run_key UNIQUE，已存在则表示已处理（或处理中）
            msg = str(e).lower()
            if "unique" in msg or "constraint" in msg:
                return False
            raise


async def _finish_run(
    *,
    run_key: str,
    status: str,
    published_posts_count: int = 0,
    picked_pool_id: Optional[int] = None,
    sent_message_chat_id: Optional[int] = None,
    sent_message_id: Optional[int] = None,
    error: Optional[str] = None,
) -> None:
    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            """
            UPDATE fallback_publish_runs
            SET status = ?,
                published_posts_count = ?,
                picked_pool_id = ?,
                sent_message_chat_id = ?,
                sent_message_id = ?,
                error = ?
            WHERE run_key = ?
            """,
            (
                str(status),
                int(published_posts_count),
                int(picked_pool_id) if picked_pool_id is not None else None,
                int(sent_message_chat_id) if sent_message_chat_id is not None else None,
                int(sent_message_id) if sent_message_id is not None else None,
                str(error) if error else None,
                str(run_key),
            ),
        )


async def _count_published_posts_in_day(*, dt_scheduled: datetime) -> int:
    day_start_dt = dt_scheduled.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end_dt = day_start_dt + timedelta(days=1)
    start_ts = day_start_dt.timestamp()
    end_ts = day_end_dt.timestamp()
    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM published_posts
            WHERE is_deleted = 0
              AND publish_time >= ?
              AND publish_time < ?
            """,
            (float(start_ts), float(end_ts)),
        )
        row = await cursor.fetchone()
        return int(row["cnt"] or 0) if row else 0


async def _pick_pool_item(*, cycle_id: int) -> Optional[Dict[str, Any]]:
    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            """
            SELECT *
            FROM fallback_message_pool
            WHERE enabled = 1
              AND used_cycle_id != ?
            ORDER BY RANDOM()
            LIMIT 1
            """,
            (int(cycle_id),),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def _mark_pool_used(*, pool_id: int, cycle_id: int) -> None:
    now = _now_ts()
    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            """
            UPDATE fallback_message_pool
            SET used_cycle_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (int(cycle_id), now, int(pool_id)),
        )


async def _get_subject_stats(subject_id: int) -> Tuple[float, int]:
    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            "SELECT avg_score, vote_count FROM rating_subjects WHERE id = ?",
            (int(subject_id),),
        )
        row = await cursor.fetchone()
        if not row:
            return 0.0, 0
        return float(row["avg_score"] or 0.0), int(row["vote_count"] or 0)


async def fallback_publish_tick(context) -> None:
    try:
        cfg = await get_config()
    except Exception as e:
        logger.error(f"读取兜底定时发布配置失败: {e}", exc_info=True)
        return

    if not cfg.enabled:
        return

    now = _now_ts()
    if not cfg.next_run_at:
        try:
            next_run_at = compute_next_run_at(now=now, schedule_type=cfg.schedule_type, payload=cfg.schedule_payload)
        except Exception as e:
            logger.error(f"计算兜底 next_run_at 失败: {e}")
            return
        await update_config_fields(next_run_at=float(next_run_at))
        return

    if float(cfg.next_run_at) > now:
        return

    scheduled_at = float(cfg.next_run_at)
    dt_scheduled = datetime.fromtimestamp(scheduled_at)
    run_key = dt_scheduled.strftime("%Y-%m-%d")

    # 错过触发时间：跳过并推进到下一天
    if now - scheduled_at > float(max(0, cfg.miss_tolerance_seconds)):
        started = await _try_start_run(run_key=run_key, scheduled_at=scheduled_at)
        if started:
            await _finish_run(run_key=run_key, status="skipped_missed")
        try:
            next_run_at = compute_next_run_at(now=now, schedule_type=cfg.schedule_type, payload=cfg.schedule_payload)
        except Exception as e:
            logger.error(f"计算兜底下一次 next_run_at 失败: {e}")
            next_run_at = None
        await update_config_fields(last_run_at=float(now), next_run_at=float(next_run_at) if next_run_at else None)
        return

    # 幂等：同一天只决策一次
    started = await _try_start_run(run_key=run_key, scheduled_at=scheduled_at)
    if not started:
        # 已处理过：仍推进 next_run_at，避免重复 tick 卡住
        try:
            next_run_at = compute_next_run_at(now=now, schedule_type=cfg.schedule_type, payload=cfg.schedule_payload)
        except Exception as e:
            logger.error(f"计算兜底下一次 next_run_at 失败: {e}")
            next_run_at = None
        await update_config_fields(last_run_at=float(now), next_run_at=float(next_run_at) if next_run_at else None)
        return

    try:
        published_count = await _count_published_posts_in_day(dt_scheduled=dt_scheduled)
        if published_count > 0:
            await _finish_run(run_key=run_key, status="skipped_has_posts", published_posts_count=published_count)
            try:
                next_run_at = compute_next_run_at(now=now, schedule_type=cfg.schedule_type, payload=cfg.schedule_payload)
            except Exception as e:
                logger.error(f"计算兜底下一次 next_run_at 失败: {e}")
                next_run_at = None
            await update_config_fields(last_run_at=float(now), next_run_at=float(next_run_at) if next_run_at else None)
            return

        # 不重复随机：先从当前 cycle 里挑未用的；若全部用完则 cycle+1 重置
        pool_item = await _pick_pool_item(cycle_id=int(cfg.cycle_id))
        cycle_id = int(cfg.cycle_id)
        if not pool_item:
            enabled_total = await count_pool_items(enabled_only=True)
            if enabled_total <= 0:
                await _finish_run(run_key=run_key, status="skipped_no_pool", published_posts_count=0)
                try:
                    next_run_at = compute_next_run_at(now=now, schedule_type=cfg.schedule_type, payload=cfg.schedule_payload)
                except Exception as e:
                    logger.error(f"计算兜底下一次 next_run_at 失败: {e}")
                    next_run_at = None
                await update_config_fields(last_run_at=float(now), next_run_at=float(next_run_at) if next_run_at else None)
                return

            cycle_id = int(cfg.cycle_id) + 1
            await update_config_fields(cycle_id=int(cycle_id))
            pool_item = await _pick_pool_item(cycle_id=int(cycle_id))

        if not pool_item:
            await _finish_run(run_key=run_key, status="skipped_no_pool", published_posts_count=0)
            try:
                next_run_at = compute_next_run_at(now=now, schedule_type=cfg.schedule_type, payload=cfg.schedule_payload)
            except Exception as e:
                logger.error(f"计算兜底下一次 next_run_at 失败: {e}")
                next_run_at = None
            await update_config_fields(last_run_at=float(now), next_run_at=float(next_run_at) if next_run_at else None)
            return

        pool_id = int(pool_item["id"])
        text = render_message_template(str(pool_item.get("message_text") or ""), now=now).strip()
        if not text:
            raise ValueError("预存消息为空")

        try:
            try:
                sent = await context.bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except Exception:
                sent = await context.bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=text,
                    disable_web_page_preview=True,
                )
        except Exception as e:
            await _finish_run(run_key=run_key, status="failed", error=str(e))
            raise

        # 按投稿一致：评分启用才附加评分键盘（包含自定义按钮行）
        subject_id = pool_item.get("rating_subject_id")
        if runtime_settings.rating_enabled():
            try:
                sid = int(subject_id) if subject_id is not None else 0
                if sid <= 0:
                    sid = await _ensure_platform_subject_id(
                        platform_domain=str(pool_item.get("platform_domain") or ""),
                        platform_tg_username=str(pool_item.get("platform_tg_username") or ""),
                        display_name=str(pool_item.get("display_name") or ""),
                    )
                    async with get_db() as conn:
                        cursor = await conn.cursor()
                        await cursor.execute(
                            "UPDATE fallback_message_pool SET rating_subject_id = ?, updated_at = ? WHERE id = ?",
                            (int(sid), _now_ts(), int(pool_id)),
                        )
                avg_score, vote_count = await _get_subject_stats(int(sid))
                keyboard = Keyboards.rating_keyboard(int(sid), float(avg_score), int(vote_count))
                await context.bot.edit_message_reply_markup(
                    chat_id=CHANNEL_ID,
                    message_id=int(sent.message_id),
                    reply_markup=keyboard,
                )
            except Exception as e:
                logger.warning(f"兜底消息附加评分键盘失败（可忽略）: {e}", exc_info=True)

        await _mark_pool_used(pool_id=pool_id, cycle_id=int(cycle_id))
        await _finish_run(
            run_key=run_key,
            status="sent",
            published_posts_count=0,
            picked_pool_id=pool_id,
            sent_message_chat_id=int(sent.chat_id),
            sent_message_id=int(sent.message_id),
        )

        try:
            next_run_at = compute_next_run_at(now=now, schedule_type=cfg.schedule_type, payload=cfg.schedule_payload)
        except Exception as e:
            logger.error(f"计算兜底下一次 next_run_at 失败: {e}")
            next_run_at = None
        await update_config_fields(last_run_at=float(now), next_run_at=float(next_run_at) if next_run_at else None)

    except Exception as e:
        logger.error(f"兜底定时发布执行失败: {e}", exc_info=True)
        try:
            await _finish_run(run_key=run_key, status="failed", error=str(e))
        except Exception:
            pass
