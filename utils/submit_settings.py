"""
投稿流程运行时设置快照（用于保证同一次投稿流程中的一致性）

约定：
- 基础限制（字数/标签/文件类型/展示与通知/评分开关）走“会话快照”，只对新会话生效
- 重复检测/限流等风控项走“即时读取”，不放入快照
"""
from __future__ import annotations

import time
from typing import Any, Dict

from utils import runtime_settings

SNAPSHOT_KEY = "submit_settings_snapshot"


def build_snapshot() -> Dict[str, Any]:
    return {
        "loaded_at": time.time(),
        "min_text_length": int(runtime_settings.bot_min_text_length()),
        "max_text_length": int(runtime_settings.bot_max_text_length()),
        "allowed_tags": int(runtime_settings.bot_allowed_tags()),
        "allowed_file_types": str(runtime_settings.bot_allowed_file_types() or "").strip() or "*",
        "show_submitter": bool(runtime_settings.bot_show_submitter()),
        "notify_owner": bool(runtime_settings.bot_notify_owner()),
        "rating_enabled": bool(runtime_settings.rating_enabled()),
        "rating_allow_update": bool(runtime_settings.rating_allow_update()),
    }


def ensure_snapshot(context: Any) -> Dict[str, Any]:
    """
    确保当前用户会话存在快照；若不存在则创建。

    Args:
        context: telegram CallbackContext（此处用 Any 避免强依赖）
    """
    user_data = getattr(context, "user_data", None)
    if not isinstance(user_data, dict):
        return build_snapshot()
    snap = user_data.get(SNAPSHOT_KEY)
    if isinstance(snap, dict):
        return snap
    snap = build_snapshot()
    user_data[SNAPSHOT_KEY] = snap
    return snap


def get_snapshot(context: Any) -> Dict[str, Any]:
    user_data = getattr(context, "user_data", None)
    if not isinstance(user_data, dict):
        return {}
    snap = user_data.get(SNAPSHOT_KEY)
    return snap if isinstance(snap, dict) else {}

