"""Exercise the admin routes with real HTTP requests and a temporary database."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from multidict import MultiDict

from database import db_manager
from handlers import admin_web, telegram_ui_admin
from utils import runtime_settings
from utils.telegram_capabilities import UI_FEATURES, feature_enabled

AUTH = {"X-Admin-Token": "web-test-token"}
PATH = "/console/telegram-ui"


@pytest_asyncio.fixture
async def admin_client(tmp_path, monkeypatch):
    monkeypatch.setattr(db_manager, "DB_PATH", str(tmp_path / "admin.db"))
    monkeypatch.setattr(runtime_settings, "_snapshot", {})
    monkeypatch.setattr(runtime_settings, "_snapshot_loaded_at", 0)
    monkeypatch.setattr(admin_web, "ADMIN_WEB_TOKENS", ["web-test-token"])
    monkeypatch.setattr(admin_web, "ADMIN_WEB_PATH", "/console")
    await db_manager.init_db()
    application = web.Application()
    for method, path, handler in admin_web.build_admin_routes():
        application.router.add_route(method, path, handler)
    async with TestClient(TestServer(application)) as client:
        yield client


def submitted_settings(**overrides):
    return {**dict.fromkeys(UI_FEATURES, "true"), "pilot_user_ids": "", **overrides}


@pytest.mark.asyncio
async def test_web_settings_require_existing_admin_auth(admin_client):
    response = await admin_client.get(PATH, allow_redirects=False)
    assert response.status == 302
    assert response.headers["Location"] == "/console/login"
    response = await admin_client.post(PATH, data=submitted_settings(), allow_redirects=False)
    assert response.status == 302
    assert not feature_enabled("rich_review", 11)


@pytest.mark.asyncio
async def test_admin_home_and_navigation_link_to_settings(admin_client):
    response = await admin_client.get("/console", headers=AUTH)
    text = await response.text()
    assert response.status == 200
    assert text.count(f'href="{PATH}"') == 2
    response = await admin_client.get(PATH, headers=AUTH)
    text = await response.text()
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert text.count('<option value="false" selected>') == len(UI_FEATURES)
    assert all(f'name="{key}"' in text for key in UI_FEATURES)
    assert 'for="pilot_user_ids"' in text


@pytest.mark.asyncio
async def test_web_save_updates_database_and_bot_runtime_with_cookie_login(admin_client):
    response = await admin_client.post(
        "/console/login", data={"token": AUTH["X-Admin-Token"]}, allow_redirects=False
    )
    assert response.status == 302
    response = await admin_client.post(
        PATH, data=submitted_settings(pilot_user_ids="0011, 22,11"), allow_redirects=False
    )
    assert response.status == 303
    assert response.headers["Location"] == PATH + "?saved=1"
    assert feature_enabled("rich_review", 11)
    assert feature_enabled("order_widgets", 22)
    assert not feature_enabled("rich_posts", 33)
    assert runtime_settings.disabled_buttons_enabled()
    async with db_manager.get_db() as conn:
        saved = dict(await (await conn.execute(
            "SELECT key,value FROM runtime_settings WHERE key LIKE 'telegram_ui.%'"
        )).fetchall())
    assert saved["telegram_ui.pilot_user_ids"] == "11,22"
    assert all(saved["telegram_ui." + name] == "true" for name in UI_FEATURES)
    response = await admin_client.get(PATH + "?saved=1")
    text = await response.text()
    assert "设置已保存。" in text
    assert text.count('<option value="true" selected>') == len(UI_FEATURES)
    assert 'value="11,22"' in text
    # Reload from persistent settings, as happens on startup.
    runtime_settings._snapshot.clear()
    await runtime_settings.refresh()
    assert feature_enabled("rich_review", 11)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_ids", ["0", "-1", "9223372036854775808", "１２３", "11,,22",
                                     ",".join(str(i) for i in range(1, 102)), '"><script>bad()</script>'])
async def test_invalid_pilot_users_preserve_form_without_partial_write(admin_client, bad_ids):
    response = await admin_client.post(PATH, headers=AUTH, data=submitted_settings(pilot_user_ids=bad_ids))
    assert response.status == 400
    text = await response.text()
    assert 'aria-invalid="true"' in text
    assert text.count('<option value="true" selected>') == len(UI_FEATURES)
    assert "<script>bad()" not in text
    assert not feature_enabled("rich_review", 11)
    async with db_manager.get_db() as conn:
        count = await (await conn.execute("SELECT count(*) FROM runtime_settings")).fetchone()
    assert count[0] == 0


@pytest.mark.asyncio
async def test_missing_and_duplicate_switches_cannot_silently_disable_features(admin_client):
    await runtime_settings.set_many(values={"telegram_ui.rich_review": "true"})
    data = submitted_settings()
    del data["rich_review"]
    response = await admin_client.post(PATH, headers=AUTH, data=data)
    assert response.status == 400
    duplicate = MultiDict(submitted_settings())
    duplicate.add("rich_review", "false")
    response = await admin_client.post(PATH, headers=AUTH, data=duplicate)
    assert response.status == 400
    assert feature_enabled("rich_review", 11)


@pytest.mark.asyncio
async def test_command_changes_are_visible_on_web_and_can_be_disabled(admin_client, monkeypatch):
    monkeypatch.setattr(telegram_ui_admin, "ADMIN_IDS", [11])
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=11),
        effective_message=SimpleNamespace(reply_text=AsyncMock()),
    )
    await telegram_ui_admin.ui_settings(update, SimpleNamespace(args=["rich_review", "on"]))
    await telegram_ui_admin.ui_settings(update, SimpleNamespace(args=["pilot_user_ids", "0011,22"]))
    text = await (await admin_client.get(PATH, headers=AUTH)).text()
    assert 'value="11,22"' in text
    assert text.count('<option value="true" selected>') == 1
    response = await admin_client.post(
        PATH, headers=AUTH,
        data={**dict.fromkeys(UI_FEATURES, "false"), "pilot_user_ids": ""},
        allow_redirects=False,
    )
    assert response.status == 303
    assert not feature_enabled("rich_review", 11)
    assert runtime_settings.get_raw("telegram_ui.pilot_user_ids") == ""
    await telegram_ui_admin.ui_settings(update, SimpleNamespace(args=[]))
    assert "富消息审核卡片：关闭" in update.effective_message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_database_failure_keeps_values_and_shows_retry_feedback(admin_client, monkeypatch):
    monkeypatch.setattr(runtime_settings, "set_many", AsyncMock(side_effect=OSError("test failure")))
    response = await admin_client.post(PATH, headers=AUTH, data=submitted_settings(pilot_user_ids="11"))
    assert response.status == 503
    text = await response.text()
    assert "保存失败，请稍后重试。" in text
    assert 'value="11"' in text
    assert text.count('<option value="true" selected>') == len(UI_FEATURES)
