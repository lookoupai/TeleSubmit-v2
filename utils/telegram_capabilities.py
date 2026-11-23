"""One update contract for both runners. Enable new types with their handlers."""

from utils import runtime_settings


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
