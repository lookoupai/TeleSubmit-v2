"""
投稿流程运行时设置快照（用于保证同一次投稿流程中的一致性）

约定：
- 基础限制（字数/标签/文件类型/展示与通知/评分开关）走“会话快照”，只对新会话生效
- 重复检测/限流等风控项走“即时读取”，不放入快照
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional

from utils import runtime_settings
from utils.submit_policy import get_effective_policy

SNAPSHOT_KEY = "submit_settings_snapshot"


def _snapshot_from_policy(policy: Dict[str, Any]) -> Dict[str, Any]:
    tags = policy.get("tags") or {}
    upload = policy.get("upload_limits") or {}
    return {
        "loaded_at": time.time(),
        "min_text_length": int((policy.get("text_length") or {}).get("min_len", 10)),
        "max_text_length": int((policy.get("text_length") or {}).get("max_len", 4000)),
        "allowed_tags": int(tags.get("max_tags", 30)) if bool(tags.get("enabled", True)) else 0,
        "allowed_file_types": str((policy.get("file_types") or {}).get("allowed_file_types") or "").strip() or "*",
        "show_submitter": bool((policy.get("bot") or {}).get("show_submitter", True)),
        "notify_owner": bool((policy.get("bot") or {}).get("notify_owner", True)),
        "rating_enabled": bool((policy.get("rating") or {}).get("enabled", True)),
        "rating_allow_update": bool((policy.get("rating") or {}).get("allow_update", True)),
        "max_docs": int(upload.get("max_docs", 10)),
        "max_media_default": int(upload.get("max_media_default", 10)),
        "max_media_media_mode": int(upload.get("max_media_media_mode", 50)),
        "media_mode_require_one": bool(upload.get("media_mode_require_one", True)),
    }


def build_snapshot() -> Dict[str, Any]:
    """
    兼容旧调用：仅使用全局默认（runtime_settings）。
    新逻辑应优先调用 build_snapshot_for_user/ensure_snapshot(user_id=...)。
    """
    max_tags = int(runtime_settings.bot_allowed_tags())
    policy = {
        "text_length": {
            "min_len": int(runtime_settings.bot_min_text_length()),
            "max_len": int(runtime_settings.bot_max_text_length()),
        },
        "tags": {
            "enabled": bool(max_tags > 0),
            "max_tags": max_tags,
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
    return _snapshot_from_policy(policy)


def build_snapshot_for_user(user_id: int) -> Dict[str, Any]:
    policy = get_effective_policy(int(user_id))
    return _snapshot_from_policy(policy)


def ensure_snapshot(context: Any, *, user_id: Optional[int] = None) -> Dict[str, Any]:
    """
    确保当前用户会话存在快照；若不存在则创建。

    Args:
        context: telegram CallbackContext（此处用 Any 避免强依赖）
    """
    user_data = getattr(context, "user_data", None)
    if not isinstance(user_data, dict):
        return build_snapshot_for_user(int(user_id)) if user_id is not None else build_snapshot()
    snap = user_data.get(SNAPSHOT_KEY)
    if isinstance(snap, dict):
        return snap
    snap = build_snapshot_for_user(int(user_id)) if user_id is not None else build_snapshot()
    user_data[SNAPSHOT_KEY] = snap
    return snap


def get_snapshot(context: Any) -> Dict[str, Any]:
    user_data = getattr(context, "user_data", None)
    if not isinstance(user_data, dict):
        return {}
    snap = user_data.get(SNAPSHOT_KEY)
    return snap if isinstance(snap, dict) else {}
