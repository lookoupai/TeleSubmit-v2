"""
投稿限制策略（Policy）与白名单（Profile/User）管理。

目标（KISS/DRY）：
- 全局默认：读取 utils.runtime_settings（已有热更新能力）
- 白名单：按“策略档位（Profile）”对特定用户放宽限制
- 热路径：投稿/审核时只做内存读取，不做 async DB I/O

数据模型：
- submit_policy_profiles：profile_id -> overrides_json（只存覆盖项，未填则继承全局默认）
- submit_policy_users：user_id -> profile_id
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

from database.db_manager import get_db
from utils import runtime_settings

logger = logging.getLogger(__name__)


JsonObj = Dict[str, Any]
Number = Union[int, float]


@dataclass(frozen=True)
class PolicyProfile:
    profile_id: str
    name: str
    overrides: JsonObj
    updated_at: float


@dataclass(frozen=True)
class PolicyUser:
    user_id: int
    profile_id: str
    username: str
    note: str
    updated_at: float


_profiles: Dict[str, PolicyProfile] = {}
_users: Dict[int, PolicyUser] = {}
_loaded_at: float = 0.0


_ALLOWED_OVERRIDES_SCHEMA: Dict[str, Dict[str, Any]] = {
    "ai_review": {
        "mode": str,
    },
    "rate_limit": {
        "enabled": bool,
        "count": int,
        "window_hours": int,
    },
    "duplicate_check": {
        "enabled": bool,
        "window_days": int,
        "similarity_threshold": (int, float),
        "check_urls": bool,
        "check_contacts": bool,
        "check_tg_links": bool,
        "check_user_bio": bool,
        "check_content_hash": bool,
        "auto_reject": bool,
        "notify_user": bool,
    },
    "text_length": {
        "min_len": int,
        "max_len": int,
    },
    "tags": {
        "enabled": bool,
        "max_tags": int,
    },
    "file_types": {
        "allowed_file_types": str,
    },
    "upload_limits": {
        "max_docs": int,
        "max_media_default": int,
        "max_media_media_mode": int,
        "media_mode_require_one": bool,
    },
    "bot": {
        "show_submitter": bool,
        "notify_owner": bool,
    },
    "rating": {
        "enabled": bool,
        "allow_update": bool,
    },
}


def _deep_merge(base: JsonObj, patch: JsonObj) -> JsonObj:
    """
    递归合并 dict：
    - patch 中的 dict 会递归合并
    - 其他类型直接覆盖
    """
    out: JsonObj = dict(base or {})
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _parse_json_obj(raw: str) -> JsonObj:
    s = (raw or "").strip()
    if not s:
        return {}
    v = json.loads(s)
    if not isinstance(v, dict):
        raise ValueError("overrides_json 必须是 JSON object")
    return v


def _validate_overrides(overrides: JsonObj) -> None:
    if not isinstance(overrides, dict):
        raise ValueError("overrides 必须是 dict")
    for section, content in overrides.items():
        if section not in _ALLOWED_OVERRIDES_SCHEMA:
            raise ValueError(f"不支持的 overrides section: {section}")
        if not isinstance(content, dict):
            raise ValueError(f"{section} 必须是 object")
        schema = _ALLOWED_OVERRIDES_SCHEMA[section]
        for key, value in content.items():
            if key not in schema:
                raise ValueError(f"不支持的 overrides key: {section}.{key}")
            expected = schema[key]
            # JSON 的 true/false 在 Python 中是 bool（也是 int 的子类），这里避免误判
            if expected is int and isinstance(value, bool):
                raise ValueError(f"{section}.{key} 类型错误，期望 int，实际 bool")
            if isinstance(expected, tuple) and any(t in (int, float) for t in expected) and isinstance(value, bool):
                raise ValueError(f"{section}.{key} 类型错误，期望 number，实际 bool")
            if not isinstance(value, expected):
                raise ValueError(f"{section}.{key} 类型错误，期望 {expected}，实际 {type(value)}")
            if section == "ai_review" and key == "mode":
                allowed = {"inherit", "skip", "run_no_auto_reject", "manual_only"}
                v = str(value or "").strip()
                if v not in allowed:
                    raise ValueError(f"ai_review.mode 必须是 {sorted(allowed)} 之一")


def build_global_policy() -> JsonObj:
    """
    生成全局默认策略（来源：runtime_settings，支持热更新）。
    注意：此函数为同步读（只读内存快照），可在热路径调用。
    """
    max_tags = int(runtime_settings.bot_allowed_tags())
    return {
        "ai_review": {
            "mode": "inherit",
        },
        "rate_limit": {
            "enabled": bool(runtime_settings.rate_limit_enabled()),
            "count": int(runtime_settings.rate_limit_count()),
            "window_hours": int(runtime_settings.rate_limit_window_hours()),
        },
        "duplicate_check": {
            "enabled": bool(runtime_settings.duplicate_check_enabled()),
            "window_days": int(runtime_settings.duplicate_check_window_days()),
            "similarity_threshold": float(runtime_settings.duplicate_similarity_threshold()),
            "check_urls": bool(runtime_settings.duplicate_check_urls()),
            "check_contacts": bool(runtime_settings.duplicate_check_contacts()),
            "check_tg_links": bool(runtime_settings.duplicate_check_tg_links()),
            "check_user_bio": bool(runtime_settings.duplicate_check_user_bio()),
            "check_content_hash": bool(runtime_settings.duplicate_check_content_hash()),
            "auto_reject": bool(runtime_settings.duplicate_auto_reject_duplicate()),
            "notify_user": bool(runtime_settings.duplicate_notify_user_duplicate()),
        },
        "text_length": {
            "min_len": int(runtime_settings.bot_min_text_length()),
            "max_len": int(runtime_settings.bot_max_text_length()),
        },
        "tags": {
            "enabled": bool(max_tags > 0),
            "max_tags": int(max_tags),
        },
        "file_types": {
            "allowed_file_types": str(runtime_settings.bot_allowed_file_types() or "").strip() or "*",
        },
        "upload_limits": {
            "max_docs": int(runtime_settings.upload_max_docs()),
            "max_media_default": int(runtime_settings.upload_max_media_default()),
            "max_media_media_mode": int(runtime_settings.upload_max_media_media_mode()),
            "media_mode_require_one": bool(runtime_settings.upload_media_mode_require_one()),
        },
        "bot": {
            "show_submitter": bool(runtime_settings.bot_show_submitter()),
            "notify_owner": bool(runtime_settings.bot_notify_owner()),
        },
        "rating": {
            "enabled": bool(runtime_settings.rating_enabled()),
            "allow_update": bool(runtime_settings.rating_allow_update()),
        },
    }


def is_whitelisted(user_id: int) -> bool:
    return int(user_id) in _users


def list_profiles() -> List[PolicyProfile]:
    return sorted(_profiles.values(), key=lambda p: p.profile_id)


def list_users() -> List[PolicyUser]:
    return sorted(_users.values(), key=lambda u: u.user_id)


def get_effective_policy(user_id: int) -> JsonObj:
    """
    获取用户生效策略（同步，无 DB I/O）。
    合并顺序：全局默认 -> profile overrides（若存在）。
    """
    base = build_global_policy()
    entry = _users.get(int(user_id))
    if not entry:
        return base
    profile = _profiles.get(entry.profile_id)
    if not profile:
        return base
    return _deep_merge(base, profile.overrides)


async def refresh() -> None:
    """从 DB 刷新内存快照。"""
    global _profiles, _users, _loaded_at

    profiles: Dict[str, PolicyProfile] = {}
    users: Dict[int, PolicyUser] = {}
    now = float(time.time())

    try:
        async with get_db() as conn:
            c = await conn.cursor()
            await c.execute("SELECT profile_id, name, overrides_json, updated_at FROM submit_policy_profiles")
            rows = await c.fetchall()
            for row in rows or []:
                pid = str(row["profile_id"] or "").strip()
                if not pid:
                    continue
                name = str(row["name"] or "").strip() or pid
                overrides = _parse_json_obj(str(row["overrides_json"] or ""))
                _validate_overrides(overrides)
                updated_at = float(row["updated_at"] or 0.0)
                profiles[pid] = PolicyProfile(profile_id=pid, name=name, overrides=overrides, updated_at=updated_at)

            await c.execute("SELECT user_id, profile_id, username, note, updated_at FROM submit_policy_users")
            rows = await c.fetchall()
            for row in rows or []:
                try:
                    uid = int(row["user_id"])
                except Exception:
                    continue
                pid = str(row["profile_id"] or "").strip()
                if not pid:
                    continue
                users[uid] = PolicyUser(
                    user_id=uid,
                    profile_id=pid,
                    username=str(row["username"] or "").strip(),
                    note=str(row["note"] or "").strip(),
                    updated_at=float(row["updated_at"] or 0.0),
                )
    except Exception as e:
        logger.error(f"刷新 submit_policy 缓存失败: {e}", exc_info=True)
        # 出错时不破坏旧缓存
        return

    _profiles = profiles
    _users = users
    _loaded_at = now
    logger.info(f"submit_policy 缓存已刷新: profiles={len(_profiles)}, users={len(_users)}")


async def init_submit_policy() -> None:
    """
    初始化（确保表存在 + 首次加载缓存）。
    注意：建表也会在 database.init_db 中做；此处做兜底保证。
    """
    try:
        async with get_db() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS submit_policy_profiles (
                    profile_id TEXT PRIMARY KEY,
                    name TEXT,
                    overrides_json TEXT NOT NULL,
                    updated_at REAL
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS submit_policy_users (
                    user_id INTEGER PRIMARY KEY,
                    profile_id TEXT NOT NULL,
                    username TEXT,
                    note TEXT,
                    updated_at REAL
                )
            """)
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_submit_policy_users_profile_id ON submit_policy_users(profile_id)")
    except Exception as e:
        logger.error(f"初始化 submit_policy 表失败: {e}", exc_info=True)

    await refresh()


async def upsert_profile(*, profile_id: str, name: str, overrides: JsonObj) -> None:
    pid = str(profile_id or "").strip()
    if not pid:
        raise ValueError("profile_id 不能为空")
    nm = str(name or "").strip() or pid
    if not isinstance(overrides, dict):
        raise ValueError("overrides 必须是 object")
    _validate_overrides(overrides)

    raw = json.dumps(overrides, ensure_ascii=False, separators=(",", ":"))
    now = float(time.time())
    async with get_db() as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO submit_policy_profiles(profile_id, name, overrides_json, updated_at) VALUES (?, ?, ?, ?)",
            (pid, nm, raw, now),
        )
    await refresh()


async def delete_profile(*, profile_id: str) -> None:
    pid = str(profile_id or "").strip()
    if not pid:
        raise ValueError("profile_id 不能为空")
    used_by = [u.user_id for u in _users.values() if u.profile_id == pid]
    if used_by:
        raise ValueError(f"该档位仍被 {len(used_by)} 个用户使用，无法删除")
    async with get_db() as conn:
        await conn.execute("DELETE FROM submit_policy_profiles WHERE profile_id = ?", (pid,))
    await refresh()


async def upsert_user(*, user_id: int, profile_id: str, username: str = "", note: str = "") -> None:
    uid = int(user_id)
    pid = str(profile_id or "").strip()
    if not pid:
        raise ValueError("profile_id 不能为空")
    if pid not in _profiles:
        raise ValueError(f"profile 不存在: {pid}")
    now = float(time.time())
    async with get_db() as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO submit_policy_users(user_id, profile_id, username, note, updated_at) VALUES (?, ?, ?, ?, ?)",
            (uid, pid, str(username or "").strip(), str(note or "").strip(), now),
        )
    await refresh()


async def delete_user(*, user_id: int) -> None:
    uid = int(user_id)
    async with get_db() as conn:
        await conn.execute("DELETE FROM submit_policy_users WHERE user_id = ?", (uid,))
    await refresh()
