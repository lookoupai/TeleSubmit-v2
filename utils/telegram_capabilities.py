"""One update contract for both runners. Enable new types with their handlers."""

from utils import runtime_settings

UI_FEATURES = {
    "disabled_buttons": "原生禁用按钮",
    "ephemeral_feedback": "频道个人评分反馈",
    "rich_review": "富消息审核卡片",
    "rich_posts": "富消息频道投稿",
    "order_widgets": "广告次数订单 Widget",
    "slot_order_widgets": "广告位订单 Widget",
}


def normalize_pilot_user_ids(value):
    value = value.strip()
    if value in ("", "all"):
        return ""
    ids = [item.strip() for item in value.split(",")]
    if len(ids) > 100 or not all(
        item.isascii() and item.isdigit() and 1 <= len(item) <= 19
        and 0 < int(item) < 2**63
        for item in ids
    ):
        raise ValueError("请输入最多 100 个有效的 Telegram 用户 ID，用逗号分隔。")
    return ",".join(dict.fromkeys(str(int(item)) for item in ids))


def feature_enabled(feature, user_id=None):
    if not runtime_settings.get_bool("telegram_ui." + feature, False):
        return False
    users = runtime_settings.get_raw("telegram_ui.pilot_user_ids")
    return not users or str(user_id) in users.split(",")


def allowed_updates():
    return [
        "message",
        "edited_message",
        "channel_post",
        "edited_channel_post",
        "callback_query",
        "inline_query",
        "widget_query",
    ]
