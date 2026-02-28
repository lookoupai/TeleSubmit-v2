"""
Slot Ads 高级按钮字段（style / icon_custom_emoji_id）测试
"""

import pytest


@pytest.mark.asyncio
async def test_build_channel_keyboard_respects_advanced_switches():
    from utils import runtime_settings
    from utils.slot_ad_service import build_channel_keyboard

    orig_snapshot = dict(runtime_settings._snapshot)  # type: ignore[attr-defined]
    try:
        runtime_settings._snapshot = {  # type: ignore[attr-defined]
            runtime_settings.KEY_SLOT_AD_ACTIVE_ROWS_COUNT: "20",
            runtime_settings.KEY_SLOT_AD_ALLOW_STYLE: "1",
            runtime_settings.KEY_SLOT_AD_ALLOW_CUSTOM_EMOJI: "1",
            runtime_settings.KEY_SLOT_AD_CUSTOM_EMOJI_MODE: "auto",
        }

        slot_defaults = {
            1: {
                "default_text": None,
                "default_url": None,
                "default_buttons": [
                    {
                        "text": "默认按钮",
                        "url": "https://example.com",
                        "style": "danger",
                        "icon_custom_emoji_id": "5390937358942362430",
                    }
                ],
                "sell_enabled": False,
            }
        }

        markup = build_channel_keyboard(slot_defaults=slot_defaults, active_orders={})
        btn = markup.inline_keyboard[0][0]
        d = btn.to_dict()
        assert d.get("style") == "danger"
        assert d.get("icon_custom_emoji_id") == "5390937358942362430"

        runtime_settings._snapshot[runtime_settings.KEY_SLOT_AD_ALLOW_CUSTOM_EMOJI] = "0"  # type: ignore[attr-defined]
        markup_no_emoji = build_channel_keyboard(slot_defaults=slot_defaults, active_orders={})
        d2 = markup_no_emoji.inline_keyboard[0][0].to_dict()
        assert d2.get("style") == "danger"
        assert "icon_custom_emoji_id" not in d2

        runtime_settings._snapshot[runtime_settings.KEY_SLOT_AD_ALLOW_STYLE] = "0"  # type: ignore[attr-defined]
        markup_plain = build_channel_keyboard(slot_defaults=slot_defaults, active_orders={})
        d3 = markup_plain.inline_keyboard[0][0].to_dict()
        assert "style" not in d3
        assert "icon_custom_emoji_id" not in d3
    finally:
        runtime_settings._snapshot = orig_snapshot  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_refresh_last_keyboard_auto_downgrades_custom_emoji():
    from utils import runtime_settings, slot_ad_service
    from utils.scheduled_publish_service import ScheduledPublishConfig
    from utils.slot_ad_service import markup_has_custom_emoji

    orig_snapshot = dict(runtime_settings._snapshot)  # type: ignore[attr-defined]
    runtime_settings._snapshot = {  # type: ignore[attr-defined]
        runtime_settings.KEY_SLOT_AD_ACTIVE_ROWS_COUNT: "20",
        runtime_settings.KEY_SLOT_AD_ALLOW_STYLE: "1",
        runtime_settings.KEY_SLOT_AD_ALLOW_CUSTOM_EMOJI: "1",
        runtime_settings.KEY_SLOT_AD_CUSTOM_EMOJI_MODE: "auto",
    }

    async def _fake_get_slot_defaults():
        return {
            1: {
                "default_text": None,
                "default_url": None,
                "default_buttons": [
                    {
                        "text": "按钮",
                        "url": "https://example.com",
                        "style": "success",
                        "icon_custom_emoji_id": "5390937358942362430",
                    }
                ],
                "sell_enabled": False,
            }
        }

    async def _fake_get_active_orders(now=None):
        return {}

    async def _fake_get_sched_config():
        return ScheduledPublishConfig(
            enabled=True,
            schedule_type="daily_at",
            schedule_payload={"time": "09:00"},
            message_text="x",
            auto_pin=False,
            delete_prev=False,
            next_run_at=None,
            last_run_at=None,
            last_message_chat_id=10001,
            last_message_id=20002,
        )

    class _FakeBot:
        def __init__(self):
            self.reply_markups = []

        async def edit_message_reply_markup(self, *, chat_id, message_id, reply_markup):
            self.reply_markups.append(reply_markup)
            if len(self.reply_markups) == 1 and markup_has_custom_emoji(reply_markup):
                raise RuntimeError("custom emoji unavailable")
            return True

    import utils.scheduled_publish_service as sps

    orig_get_slot_defaults = slot_ad_service.get_slot_defaults
    orig_get_active_orders = slot_ad_service.get_active_orders
    orig_get_config = sps.get_config
    try:
        slot_ad_service.get_slot_defaults = _fake_get_slot_defaults  # type: ignore[assignment]
        slot_ad_service.get_active_orders = _fake_get_active_orders  # type: ignore[assignment]
        sps.get_config = _fake_get_sched_config  # type: ignore[assignment]

        bot = _FakeBot()
        ok = await slot_ad_service.refresh_last_scheduled_message_keyboard(bot=bot)
        assert ok is True
        assert len(bot.reply_markups) == 2
        assert markup_has_custom_emoji(bot.reply_markups[0]) is True
        assert markup_has_custom_emoji(bot.reply_markups[1]) is False
    finally:
        runtime_settings._snapshot = orig_snapshot  # type: ignore[attr-defined]
        slot_ad_service.get_slot_defaults = orig_get_slot_defaults  # type: ignore[assignment]
        slot_ad_service.get_active_orders = orig_get_active_orders  # type: ignore[assignment]
        sps.get_config = orig_get_config  # type: ignore[assignment]
