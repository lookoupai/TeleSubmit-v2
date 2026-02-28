"""
Web 管理后台（aiohttp）

说明：
- 仅在 RUN_MODE=WEBHOOK 且 ADMIN_WEB.ENABLED=true 时注册路由
- 鉴权：固定 token（支持多个），可用 header/query/cookie
- 仅管理“可热更新”的 DB 配置项（定时发布、slot 默认按钮、终止广告）
"""

from __future__ import annotations

import html
import json
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from aiohttp import web

from config.settings import ADMIN_WEB_PATH, ADMIN_WEB_TITLE, ADMIN_WEB_TOKENS, SLOT_AD_MAX_ROWS
from utils.scheduled_publish_service import compute_next_run_at, get_config as get_sched_config, update_config_fields
from utils.slot_ad_service import (
    build_channel_keyboard,
    get_active_orders,
    get_pending_orders,
    get_reserved_orders,
    get_slot_defaults,
    parse_default_buttons_lines,
    refresh_last_scheduled_message_keyboard,
    set_slot_default_buttons,
    set_slot_sell_enabled,
    terminate_active_order,
    update_slot_ad_order_creative_by_admin,
    validate_button_style,
    validate_button_text,
    validate_button_url,
    validate_icon_custom_emoji_id,
)
from utils.fallback_publish_service import (
    add_pool_item as fallback_add_pool_item,
    compute_next_run_at as compute_fallback_next_run_at,
    count_pool_items as fallback_count_pool_items,
    delete_pool_item as fallback_delete_pool_item,
    get_config as get_fallback_config,
    get_pool_item as fallback_get_pool_item,
    list_pool_items as fallback_list_pool_items,
    list_recent_runs as fallback_list_recent_runs,
    set_pool_enabled as fallback_set_pool_enabled,
    update_config_fields as update_fallback_config_fields,
    update_pool_item as fallback_update_pool_item,
)
from utils import runtime_settings
from utils import submit_policy

logger = logging.getLogger(__name__)


COOKIE_NAME = "ts_admin"


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _format_epoch(ts: Optional[float]) -> str:
    if ts is None:
        return "-"
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)


def _get_tg_app(request: web.Request):
    app = request.app.get("tg_application")
    if not app:
        raise web.HTTPInternalServerError(text="tg application not available")
    return app


def _token_ok(token: str) -> bool:
    token = (token or "").strip()
    if not token:
        return False
    return token in set(ADMIN_WEB_TOKENS or [])


def _extract_token(request: web.Request) -> str:
    token = (request.headers.get("X-Admin-Token") or "").strip()
    if token:
        return token
    auth = (request.headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    token = (request.query.get("token") or "").strip()
    if token:
        return token
    return (request.cookies.get(COOKIE_NAME) or "").strip()


def _require_auth(request: web.Request) -> None:
    if not ADMIN_WEB_TOKENS:
        raise web.HTTPServiceUnavailable(text="ADMIN_WEB token not configured")
    token = _extract_token(request)
    if not _token_ok(token):
        raise web.HTTPFound(location=f"{ADMIN_WEB_PATH}/login")


def _html_page(*, title: str, body: str) -> web.Response:
    html_text = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{html.escape(title)}</title>
  <style>
    :root {{ color-scheme: light dark; }}
    body {{ font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial; margin: 0; background: Canvas; color: CanvasText; }}
    header {{ padding: 14px 18px; border-bottom: 1px solid rgba(127,127,127,.25); display:flex; gap:12px; align-items:center; }}
    header .title {{ font-weight: 700; }}
    header .meta {{ opacity: .7; font-size: 12px; }}
    main {{ padding: 18px; max-width: 1100px; margin: 0 auto; }}
    a {{ color: inherit; }}
    .nav a {{ margin-right: 12px; }}
    .card {{ border: 1px solid rgba(127,127,127,.25); border-radius: 10px; padding: 14px; margin-bottom: 14px; }}
    .grid {{ display:grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
    .row {{ display:flex; gap: 10px; flex-wrap: wrap; align-items: center; }}
    label {{ display:block; font-size: 12px; opacity: .8; margin-bottom: 4px; }}
    input[type=text], textarea, select {{ width: 100%; box-sizing: border-box; padding: 8px 10px; border-radius: 8px; border: 1px solid rgba(127,127,127,.35); background: transparent; }}
    select option {{ color: CanvasText; background: Canvas; }}
    @supports not (color: CanvasText) {{
      select option {{ color: #111; background: #fff; }}
    }}
    @media (prefers-color-scheme: dark) {{
      @supports not (color: CanvasText) {{
        select option {{ color: #eee; background: #111; }}
      }}
    }}
    textarea {{ min-height: 120px; }}
    button {{ padding: 8px 10px; border-radius: 8px; border: 1px solid rgba(127,127,127,.35); background: rgba(127,127,127,.12); cursor: pointer; }}
    .danger {{ border-color: rgba(220, 38, 38, .6); }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ border-bottom: 1px solid rgba(127,127,127,.25); padding: 8px; text-align: left; vertical-align: top; }}
    th {{ font-size: 12px; opacity: .8; }}
    .pill {{ font-size: 12px; padding: 2px 8px; border-radius: 999px; border: 1px solid rgba(127,127,127,.35); display:inline-block; }}
  </style>
</head>
<body>
  <header>
    <div class="title">{html.escape(ADMIN_WEB_TITLE)}</div>
    <div class="meta">服务器时间：{html.escape(_now_text())}</div>
		    <div class="nav" style="margin-left:auto">
		      <a href="{ADMIN_WEB_PATH}">首页</a>
		      <a href="{ADMIN_WEB_PATH}/submit">投稿设置</a>
		      <a href="{ADMIN_WEB_PATH}/whitelist">投稿白名单</a>
		      <a href="{ADMIN_WEB_PATH}/schedule">定时发布</a>
		      <a href="{ADMIN_WEB_PATH}/fallback">兜底定时</a>
		      <a href="{ADMIN_WEB_PATH}/slots">广告位</a>
		      <a href="{ADMIN_WEB_PATH}/ads">广告参数</a>
		      <a href="{ADMIN_WEB_PATH}/ai">AI审核</a>
		      <a href="{ADMIN_WEB_PATH}/logout">退出</a>
		    </div>
	  </header>
  <main>
    {body}
  </main>
</body>
</html>
"""
    return web.Response(text=html_text, content_type="text/html")


async def login_get(request: web.Request) -> web.Response:
    body = f"""
<div class="card">
  <h2 style="margin-top:0">登录</h2>
  <form method="post" action="{ADMIN_WEB_PATH}/login">
    <div style="max-width:520px">
      <label>访问 Token</label>
      <input type="text" name="token" placeholder="请输入 ADMIN_WEB.TOKEN" />
      <div style="height:10px"></div>
      <button type="submit">登录</button>
    </div>
  </form>
  <p style="opacity:.75;margin-bottom:0">若未配置 token，请在 <code>config.ini</code> 的 <code>[ADMIN_WEB]</code> 中设置 <code>TOKEN</code> 后重启。</p>
</div>
"""
    return _html_page(title="登录", body=body)


async def login_post(request: web.Request) -> web.Response:
    data = await request.post()
    token = str(data.get("token") or "").strip()
    if not _token_ok(token):
        return _html_page(title="登录失败", body="<div class='card'><h2 style='margin-top:0'>登录失败</h2><p>Token 无效。</p></div>")
    resp = web.HTTPFound(location=f"{ADMIN_WEB_PATH}")
    resp.set_cookie(COOKIE_NAME, token, httponly=True, samesite="Strict", secure=bool(request.secure))
    raise resp


async def logout(request: web.Request) -> web.Response:
    resp = web.HTTPFound(location=f"{ADMIN_WEB_PATH}/login")
    resp.del_cookie(COOKIE_NAME)
    raise resp


async def index(request: web.Request) -> web.Response:
    _require_auth(request)
    base = ADMIN_WEB_PATH.rstrip("/")
    body = f"""
	<div class="card">
	  <h2 style="margin-top:0">概览</h2>
		  <div class="row">
		    <a href="{base}/submit"><button>投稿设置</button></a>
		    <a href="{base}/whitelist"><button>投稿白名单</button></a>
		    <a href="{base}/schedule"><button>管理定时发布</button></a>
		    <a href="{base}/fallback"><button>管理兜底定时</button></a>
		    <a href="{base}/slots"><button>管理广告位</button></a>
		    <a href="{base}/ads"><button>管理广告参数</button></a>
		    <a href="{base}/ai"><button>管理 AI 审核</button></a>
		  </div>
	  <p style="opacity:.75;margin-bottom:0">本后台仅管理已落库的热更新项；修改 <code>config.ini</code> 类配置仍需要重启生效。</p>
	</div>
	"""
    return _html_page(title="首页", body=body)


async def ads_get(request: web.Request) -> web.Response:
    _require_auth(request)

    def _src(key: str) -> str:
        return "DB" if runtime_settings.get_raw(key) is not None else "config.ini"

    body = f"""
<div class="card">
  <h2 style="margin-top:0">广告参数（热更新）</h2>
  <p style="opacity:.75;margin:0">此页仅管理非密钥项；UPAY_SECRET_KEY / AI_REVIEW_API_KEY 等仍需通过 <code>config.ini</code> 或环境变量配置。</p>
</div>

<div class="card">
  <h3 style="margin-top:0">付费广告（/ad）</h3>
  <form method="post" action="{ADMIN_WEB_PATH}/ads">
    <div class="grid">
      <div>
        <label>启用（来源：{_src(runtime_settings.KEY_PAID_AD_ENABLED)}）</label>
        <select name="paid_ad_enabled">
          <option value="1" {"selected" if runtime_settings.paid_ad_enabled() else ""}>启用</option>
          <option value="0" {"selected" if not runtime_settings.paid_ad_enabled() else ""}>关闭</option>
        </select>
      </div>
      <div>
        <label>币种展示（来源：{_src(runtime_settings.KEY_PAID_AD_CURRENCY)}）</label>
        <input type="text" name="paid_ad_currency" value="{html.escape(runtime_settings.paid_ad_currency())}" />
      </div>
      <div>
        <label>发布前缀（来源：{_src(runtime_settings.KEY_PAID_AD_PUBLISH_PREFIX)}）</label>
        <input type="text" name="paid_ad_publish_prefix" value="{html.escape(runtime_settings.paid_ad_publish_prefix())}" />
      </div>
      <div>
        <label>订单过期（分钟）（来源：{_src(runtime_settings.KEY_PAY_EXPIRE_MINUTES)}）</label>
        <input type="text" name="pay_expire_minutes" value="{html.escape(str(runtime_settings.pay_expire_minutes()))}" />
      </div>
      <div>
        <label>默认收款币种/网络（来源：{_src(runtime_settings.KEY_UPAY_DEFAULT_TYPE)}）</label>
        <input type="text" name="upay_default_type" value="{html.escape(runtime_settings.upay_default_type())}" />
      </div>
      <div>
        <label>可选币种/网络（逗号分隔）（来源：{_src(runtime_settings.KEY_UPAY_ALLOWED_TYPES)}）</label>
        <input type="text" name="upay_allowed_types" value="{html.escape(','.join(runtime_settings.upay_allowed_types() or []))}" />
      </div>
    </div>
    <div style="height:12px"></div>
    <label>套餐（次数:金额，逗号分隔）（来源：{_src(runtime_settings.KEY_PAID_AD_PACKAGES_RAW)}）</label>
    <input type="text" name="paid_ad_packages_raw" value="{html.escape(runtime_settings.paid_ad_packages_raw())}" />
    <div style="height:12px"></div>

    <h3 style="margin-top:0">按钮广告位（Slot Ads）</h3>
    <div class="grid">
      <div>
        <label>启用（来源：{_src(runtime_settings.KEY_SLOT_AD_ENABLED)}）</label>
        <select name="slot_ad_enabled">
          <option value="1" {"selected" if runtime_settings.slot_ad_enabled() else ""}>启用</option>
          <option value="0" {"selected" if not runtime_settings.slot_ad_enabled() else ""}>关闭</option>
        </select>
      </div>
      <div>
        <label>启用行数（前 N 行）（来源：{_src(runtime_settings.KEY_SLOT_AD_ACTIVE_ROWS_COUNT)}，MAX_ROWS={int(SLOT_AD_MAX_ROWS)}）</label>
        <input type="text" name="slot_ad_active_rows_count" value="{html.escape(str(runtime_settings.slot_ad_active_rows_count()))}" />
      </div>
      <div>
        <label>币种展示（来源：{_src(runtime_settings.KEY_SLOT_AD_CURRENCY)}）</label>
        <input type="text" name="slot_ad_currency" value="{html.escape(runtime_settings.slot_ad_currency())}" />
      </div>
      <div>
        <label>续期保护窗（天）（来源：{_src(runtime_settings.KEY_SLOT_AD_RENEW_PROTECT_DAYS)}）</label>
        <input type="text" name="slot_ad_renew_protect_days" value="{html.escape(str(runtime_settings.slot_ad_renew_protect_days()))}" />
      </div>
      <div>
        <label>按钮文案最大长度（来源：{_src(runtime_settings.KEY_SLOT_AD_BUTTON_TEXT_MAX_LEN)}）</label>
        <input type="text" name="slot_ad_button_text_max_len" value="{html.escape(str(runtime_settings.slot_ad_button_text_max_len()))}" />
      </div>
      <div>
        <label>URL 最大长度（来源：{_src(runtime_settings.KEY_SLOT_AD_URL_MAX_LEN)}）</label>
        <input type="text" name="slot_ad_url_max_len" value="{html.escape(str(runtime_settings.slot_ad_url_max_len()))}" />
      </div>
      <div>
        <label>到期提醒提前（天）（来源：{_src(runtime_settings.KEY_SLOT_AD_REMINDER_ADVANCE_DAYS)}）</label>
        <input type="text" name="slot_ad_reminder_advance_days" value="{html.escape(str(runtime_settings.slot_ad_reminder_advance_days()))}" />
      </div>
      <div>
        <label>每单每天允许修改次数（0=不限制）（来源：{_src(runtime_settings.KEY_SLOT_AD_EDIT_LIMIT_PER_ORDER_PER_DAY)}）</label>
        <input type="text" name="slot_ad_edit_limit_per_order_per_day" value="{html.escape(str(runtime_settings.slot_ad_edit_limit_per_order_per_day()))}" />
      </div>
      <div>
        <label>允许按钮样式 style（来源：{_src(runtime_settings.KEY_SLOT_AD_ALLOW_STYLE)}）</label>
        <select name="slot_ad_allow_style">
          <option value="1" {"selected" if runtime_settings.slot_ad_allow_style() else ""}>允许</option>
          <option value="0" {"selected" if not runtime_settings.slot_ad_allow_style() else ""}>禁用</option>
        </select>
      </div>
      <div>
        <label>允许会员自定义表情（来源：{_src(runtime_settings.KEY_SLOT_AD_ALLOW_CUSTOM_EMOJI)}）</label>
        <select name="slot_ad_allow_custom_emoji">
          <option value="1" {"selected" if runtime_settings.slot_ad_allow_custom_emoji() else ""}>允许</option>
          <option value="0" {"selected" if not runtime_settings.slot_ad_allow_custom_emoji() else ""}>禁用</option>
        </select>
      </div>
      <div>
        <label>会员表情策略（来源：{_src(runtime_settings.KEY_SLOT_AD_CUSTOM_EMOJI_MODE)}）</label>
        <select name="slot_ad_custom_emoji_mode">
          <option value="off" {"selected" if runtime_settings.slot_ad_custom_emoji_mode() == "off" else ""}>off（关闭）</option>
          <option value="auto" {"selected" if runtime_settings.slot_ad_custom_emoji_mode() == "auto" else ""}>auto（失败自动降级）</option>
          <option value="strict" {"selected" if runtime_settings.slot_ad_custom_emoji_mode() == "strict" else ""}>strict（失败即报错）</option>
        </select>
      </div>
      <div>
        <label>用户可设置高级字段（style/会员表情）（来源：{_src(runtime_settings.KEY_SLOT_AD_USER_CAN_SET_ADVANCED)}）</label>
        <select name="slot_ad_user_can_set_advanced">
          <option value="1" {"selected" if runtime_settings.slot_ad_user_can_set_advanced() else ""}>允许</option>
          <option value="0" {"selected" if not runtime_settings.slot_ad_user_can_set_advanced() else ""}>禁用</option>
        </select>
      </div>
    </div>
    <div style="height:12px"></div>
    <label>租期套餐（天数:金额，逗号分隔）（来源：{_src(runtime_settings.KEY_SLOT_AD_PLANS_RAW)}）</label>
    <input type="text" name="slot_ad_plans_raw" value="{html.escape(runtime_settings.slot_ad_plans_raw())}" />

    <div style="height:12px"></div>
    <button type="submit">保存</button>
  </form>
  <p style="opacity:.75;margin-bottom:0">提示：若你在启动时关闭了付费广告/广告位，Webhook 路由可能未注册；此页“从关闭切换到启用”可能仍需重启生效。</p>
</div>
"""
    return _html_page(title="广告参数", body=body)


async def ads_post(request: web.Request) -> web.Response:
    _require_auth(request)
    form = await request.post()

    def _t(name: str) -> str:
        return str(form.get(name) or "").strip()

    paid_enabled = _t("paid_ad_enabled") == "1"
    slot_enabled = _t("slot_ad_enabled") == "1"
    slot_allow_style = _t("slot_ad_allow_style") == "1"
    slot_allow_custom_emoji = _t("slot_ad_allow_custom_emoji") == "1"
    slot_custom_emoji_mode = (_t("slot_ad_custom_emoji_mode") or runtime_settings.slot_ad_custom_emoji_mode()).strip().lower()
    slot_user_can_set_advanced = _t("slot_ad_user_can_set_advanced") == "1"

    paid_packages_raw = _t("paid_ad_packages_raw")
    slot_plans_raw = _t("slot_ad_plans_raw")

    try:
        if paid_enabled:
            runtime_settings.validate_paid_ad_packages_raw(paid_packages_raw)
            if not paid_packages_raw.strip():
                raise ValueError("PAID_AD.PACKAGES 不能为空")
        if slot_enabled:
            runtime_settings.validate_slot_ad_plans_raw(slot_plans_raw)
            if not slot_plans_raw.strip():
                raise ValueError("SLOT_AD.PLANS 不能为空")

        pay_expire_minutes = int(_t("pay_expire_minutes") or "0")
        if pay_expire_minutes <= 0:
            raise ValueError("PAY_EXPIRE_MINUTES 必须为正整数")

        renew_protect_days = int(_t("slot_ad_renew_protect_days") or "0")
        if renew_protect_days < 0:
            raise ValueError("SLOT_AD.RENEW_PROTECT_DAYS 不能为负数")

        btn_max = int(_t("slot_ad_button_text_max_len") or "0")
        if btn_max <= 0:
            raise ValueError("SLOT_AD.BUTTON_TEXT_MAX_LEN 必须为正整数")

        url_max = int(_t("slot_ad_url_max_len") or "0")
        if url_max <= 0:
            raise ValueError("SLOT_AD.URL_MAX_LEN 必须为正整数")

        remind_days = int(_t("slot_ad_reminder_advance_days") or "0")
        if remind_days < 0:
            raise ValueError("SLOT_AD.REMINDER_ADVANCE_DAYS 不能为负数")

        raw_edit_limit = _t("slot_ad_edit_limit_per_order_per_day")
        if raw_edit_limit:
            slot_ad_edit_limit_per_order_per_day = int(raw_edit_limit)
        else:
            slot_ad_edit_limit_per_order_per_day = int(runtime_settings.slot_ad_edit_limit_per_order_per_day())
        runtime_settings.validate_slot_ad_edit_limit_per_order_per_day(slot_ad_edit_limit_per_order_per_day)
        runtime_settings.validate_slot_ad_custom_emoji_mode(slot_custom_emoji_mode)

        active_rows_count = int(_t("slot_ad_active_rows_count") or "0")
        if active_rows_count < 0:
            raise ValueError("SLOT_AD.ACTIVE_ROWS_COUNT 不能为负数")
        if active_rows_count > int(SLOT_AD_MAX_ROWS):
            raise ValueError(f"SLOT_AD.ACTIVE_ROWS_COUNT 不能大于 MAX_ROWS={int(SLOT_AD_MAX_ROWS)}")

        # 避免隐藏“已占用/支付中”的行，导致广告消失
        if slot_enabled:
            active = await get_active_orders()
            reserved = await get_reserved_orders()
            pending = await get_pending_orders()
            in_use = set((active or {}).keys()) | set((reserved or {}).keys()) | set((pending or {}).keys())
            max_in_use = max(in_use) if in_use else 0
            if active_rows_count < int(max_in_use):
                raise ValueError(f"启用行数不能小于当前占用的最大行号：{int(max_in_use)}")
    except Exception as e:
        return _html_page(title="保存失败", body=f"<div class='card'><h2 style='margin-top:0'>保存失败</h2><p>{html.escape(str(e))}</p></div>")

    await runtime_settings.set_many(values={
        runtime_settings.KEY_PAID_AD_ENABLED: "1" if paid_enabled else "0",
        runtime_settings.KEY_PAID_AD_PACKAGES_RAW: paid_packages_raw,
        runtime_settings.KEY_PAID_AD_CURRENCY: _t("paid_ad_currency"),
        runtime_settings.KEY_PAID_AD_PUBLISH_PREFIX: _t("paid_ad_publish_prefix"),
        runtime_settings.KEY_UPAY_DEFAULT_TYPE: _t("upay_default_type"),
        runtime_settings.KEY_UPAY_ALLOWED_TYPES: _t("upay_allowed_types"),
        runtime_settings.KEY_PAY_EXPIRE_MINUTES: str(pay_expire_minutes),

        runtime_settings.KEY_SLOT_AD_ENABLED: "1" if slot_enabled else "0",
        runtime_settings.KEY_SLOT_AD_PLANS_RAW: slot_plans_raw,
        runtime_settings.KEY_SLOT_AD_CURRENCY: _t("slot_ad_currency"),
        runtime_settings.KEY_SLOT_AD_ACTIVE_ROWS_COUNT: str(active_rows_count),
        runtime_settings.KEY_SLOT_AD_RENEW_PROTECT_DAYS: str(renew_protect_days),
        runtime_settings.KEY_SLOT_AD_BUTTON_TEXT_MAX_LEN: str(btn_max),
        runtime_settings.KEY_SLOT_AD_URL_MAX_LEN: str(url_max),
        runtime_settings.KEY_SLOT_AD_REMINDER_ADVANCE_DAYS: str(remind_days),
        runtime_settings.KEY_SLOT_AD_EDIT_LIMIT_PER_ORDER_PER_DAY: str(slot_ad_edit_limit_per_order_per_day),
        runtime_settings.KEY_SLOT_AD_ALLOW_STYLE: "1" if slot_allow_style else "0",
        runtime_settings.KEY_SLOT_AD_ALLOW_CUSTOM_EMOJI: "1" if slot_allow_custom_emoji else "0",
        runtime_settings.KEY_SLOT_AD_CUSTOM_EMOJI_MODE: slot_custom_emoji_mode,
        runtime_settings.KEY_SLOT_AD_USER_CAN_SET_ADVANCED: "1" if slot_user_can_set_advanced else "0",
    })
    raise web.HTTPFound(location=f"{ADMIN_WEB_PATH}/ads")


async def ai_get(request: web.Request) -> web.Response:
    _require_auth(request)

    def _src(key: str) -> str:
        return "DB" if runtime_settings.get_raw(key) is not None else "config.ini"

    enabled = runtime_settings.ai_review_enabled()
    body = f"""
<div class="card">
  <h2 style="margin-top:0">AI 审核（热更新）</h2>
  <div class="row">
    <span class="pill">enabled: {str(enabled)}</span>
    <span class="pill">model: {html.escape(runtime_settings.ai_review_model())}</span>
  </div>
</div>

<div class="card">
  <h3 style="margin-top:0">修改配置</h3>
  <form method="post" action="{ADMIN_WEB_PATH}/ai">
    <div class="grid">
      <div>
        <label>启用（来源：{_src(runtime_settings.KEY_AI_REVIEW_ENABLED)}）</label>
        <select name="ai_enabled">
          <option value="1" {"selected" if enabled else ""}>启用</option>
          <option value="0" {"selected" if not enabled else ""}>关闭</option>
        </select>
      </div>
      <div>
        <label>模型（来源：{_src(runtime_settings.KEY_AI_REVIEW_MODEL)}）</label>
        <input type="text" name="ai_model" value="{html.escape(runtime_settings.ai_review_model())}" />
      </div>
      <div>
        <label>频道主题（来源：{_src(runtime_settings.KEY_AI_REVIEW_CHANNEL_TOPIC)}）</label>
        <input type="text" name="ai_channel_topic" value="{html.escape(runtime_settings.ai_review_channel_topic())}" />
      </div>
      <div>
        <label>主题关键词（逗号分隔）（来源：{_src(runtime_settings.KEY_AI_REVIEW_TOPIC_KEYWORDS)}）</label>
        <input type="text" name="ai_topic_keywords" value="{html.escape(runtime_settings.ai_review_topic_keywords_csv())}" />
      </div>
      <div>
        <label>严格模式（来源：{_src(runtime_settings.KEY_AI_REVIEW_STRICT_MODE)}）</label>
        <select name="ai_strict_mode">
          <option value="1" {"selected" if runtime_settings.ai_review_strict_mode() else ""}>开启</option>
          <option value="0" {"selected" if not runtime_settings.ai_review_strict_mode() else ""}>关闭</option>
        </select>
      </div>
      <div>
        <label>不相关自动拒绝（来源：{_src(runtime_settings.KEY_AI_REVIEW_AUTO_REJECT)}）</label>
        <select name="ai_auto_reject">
          <option value="1" {"selected" if runtime_settings.ai_review_auto_reject() else ""}>开启</option>
          <option value="0" {"selected" if not runtime_settings.ai_review_auto_reject() else ""}>关闭</option>
        </select>
      </div>
      <div>
        <label>通知用户审核结果（来源：{_src(runtime_settings.KEY_AI_REVIEW_NOTIFY_USER)}）</label>
        <select name="ai_notify_user">
          <option value="1" {"selected" if runtime_settings.ai_review_notify_user() else ""}>开启</option>
          <option value="0" {"selected" if not runtime_settings.ai_review_notify_user() else ""}>关闭</option>
        </select>
      </div>
      <div>
        <label>API 失败降级策略（manual/pass/reject）（来源：{_src(runtime_settings.KEY_AI_REVIEW_FALLBACK_ON_ERROR)}）</label>
        <select name="ai_fallback">
          <option value="manual" {"selected" if runtime_settings.ai_review_fallback_on_error().lower() == "manual" else ""}>manual（转人工）</option>
          <option value="pass" {"selected" if runtime_settings.ai_review_fallback_on_error().lower() == "pass" else ""}>pass（直接通过）</option>
          <option value="reject" {"selected" if runtime_settings.ai_review_fallback_on_error().lower() == "reject" else ""}>reject（直接拒绝）</option>
        </select>
      </div>
    </div>

    <div style="height:12px"></div>
    <label>System Prompt（来源：{_src(runtime_settings.KEY_AI_REVIEW_SYSTEM_PROMPT)}）</label>
    <textarea name="ai_system_prompt">{html.escape(runtime_settings.ai_review_system_prompt())}</textarea>

    <div style="height:12px"></div>
    <label>审核策略文本（来源：{_src(runtime_settings.KEY_AI_REVIEW_POLICY_TEXT)}）</label>
    <p style="margin:6px 0 0;opacity:.8">
      可用占位符：<code>{{channel_topic}}</code>（对应“频道主题”）、
      <code>{{topic_keywords}}</code>（对应“主题关键词”）；保存后会在运行时替换为当前配置值。
    </p>
    <textarea name="ai_policy_text">{html.escape(runtime_settings.ai_review_policy_text())}</textarea>

    <div style="height:12px"></div>
    <h3 style="margin:0">按钮广告风控（Slot Ads）</h3>
    <p style="margin:6px 0 0;opacity:.8">
      该功能用于审核“按钮广告位”的按钮文案/链接（轻度风控）。复用同一套 AI API 配置（密钥仍在 <code>config.ini</code>）。
      Prompt 支持占位符：<code>{{button_text}}</code>、<code>{{button_url}}</code>。
    </p>

    <div style="height:12px"></div>
    <label>风控 System Prompt（来源：{_src(runtime_settings.KEY_AD_RISK_SYSTEM_PROMPT)}）</label>
    <textarea name="ad_risk_system_prompt">{html.escape(runtime_settings.ad_risk_system_prompt())}</textarea>

    <div style="height:12px"></div>
    <label>风控 Prompt 模板（来源：{_src(runtime_settings.KEY_AD_RISK_PROMPT_TEMPLATE)}）</label>
    <textarea name="ad_risk_prompt_template">{html.escape(runtime_settings.ad_risk_prompt_template())}</textarea>

    <div style="height:12px"></div>
    <button type="submit">保存</button>
  </form>
</div>
"""
    return _html_page(title="AI 审核", body=body)


async def ai_post(request: web.Request) -> web.Response:
    _require_auth(request)
    form = await request.post()

    def _t(name: str) -> str:
        return str(form.get(name) or "").strip()

    enabled = _t("ai_enabled") == "1"
    model = _t("ai_model")
    channel_topic = _t("ai_channel_topic")
    topic_keywords = _t("ai_topic_keywords")
    strict_mode = _t("ai_strict_mode") == "1"
    auto_reject = _t("ai_auto_reject") == "1"
    notify_user = _t("ai_notify_user") == "1"
    fallback = _t("ai_fallback").lower()
    system_prompt = _t("ai_system_prompt")
    policy_text = _t("ai_policy_text")
    ad_risk_system_prompt = _t("ad_risk_system_prompt")
    ad_risk_prompt_template = _t("ad_risk_prompt_template")

    try:
        if enabled:
            if not model:
                raise ValueError("MODEL 不能为空")
            if not channel_topic:
                raise ValueError("CHANNEL_TOPIC 不能为空")
        if fallback not in ("manual", "pass", "reject"):
            raise ValueError("FALLBACK_ON_ERROR 只能是 manual/pass/reject")
    except Exception as e:
        return _html_page(title="保存失败", body=f"<div class='card'><h2 style='margin-top:0'>保存失败</h2><p>{html.escape(str(e))}</p></div>")

    await runtime_settings.set_many(values={
        runtime_settings.KEY_AI_REVIEW_ENABLED: "1" if enabled else "0",
        runtime_settings.KEY_AI_REVIEW_MODEL: model,
        runtime_settings.KEY_AI_REVIEW_CHANNEL_TOPIC: channel_topic,
        runtime_settings.KEY_AI_REVIEW_TOPIC_KEYWORDS: topic_keywords,
        runtime_settings.KEY_AI_REVIEW_STRICT_MODE: "1" if strict_mode else "0",
        runtime_settings.KEY_AI_REVIEW_AUTO_REJECT: "1" if auto_reject else "0",
        runtime_settings.KEY_AI_REVIEW_NOTIFY_USER: "1" if notify_user else "0",
        runtime_settings.KEY_AI_REVIEW_FALLBACK_ON_ERROR: fallback,
        runtime_settings.KEY_AI_REVIEW_SYSTEM_PROMPT: system_prompt,
        runtime_settings.KEY_AI_REVIEW_POLICY_TEXT: policy_text,
        runtime_settings.KEY_AD_RISK_SYSTEM_PROMPT: ad_risk_system_prompt,
        runtime_settings.KEY_AD_RISK_PROMPT_TEMPLATE: ad_risk_prompt_template,
    })
    raise web.HTTPFound(location=f"{ADMIN_WEB_PATH}/ai")


def _bool_selected(v: bool, expected: bool) -> str:
    return "selected" if bool(v) == bool(expected) else ""


async def submit_get(request: web.Request) -> web.Response:
    _require_auth(request)

    def _src(key: str) -> str:
        return "DB" if runtime_settings.get_raw(key) is not None else "config.ini"

    min_len = int(runtime_settings.bot_min_text_length())
    max_len = int(runtime_settings.bot_max_text_length())
    allowed_tags = int(runtime_settings.bot_allowed_tags())
    allowed_file_types = str(runtime_settings.bot_allowed_file_types() or "").strip() or "*"
    show_submitter = bool(runtime_settings.bot_show_submitter())
    notify_owner = bool(runtime_settings.bot_notify_owner())
    max_docs = int(runtime_settings.upload_max_docs())
    max_media_default = int(runtime_settings.upload_max_media_default())
    max_media_media_mode = int(runtime_settings.upload_max_media_media_mode())
    media_mode_require_one = bool(runtime_settings.upload_media_mode_require_one())

    dup_enabled = bool(runtime_settings.duplicate_check_enabled())
    dup_window_days = int(runtime_settings.duplicate_check_window_days())
    dup_threshold = float(runtime_settings.duplicate_similarity_threshold())
    dup_check_urls = bool(runtime_settings.duplicate_check_urls())
    dup_check_contacts = bool(runtime_settings.duplicate_check_contacts())
    dup_check_tg_links = bool(runtime_settings.duplicate_check_tg_links())
    dup_check_user_bio = bool(runtime_settings.duplicate_check_user_bio())
    dup_check_content_hash = bool(runtime_settings.duplicate_check_content_hash())
    dup_auto_reject = bool(runtime_settings.duplicate_auto_reject_duplicate())
    dup_notify_user = bool(runtime_settings.duplicate_notify_user_duplicate())

    rate_enabled = bool(runtime_settings.rate_limit_enabled())
    rate_count = int(runtime_settings.rate_limit_count())
    rate_window_hours = int(runtime_settings.rate_limit_window_hours())

    rating_enabled = bool(runtime_settings.rating_enabled())
    rating_allow_update = bool(runtime_settings.rating_allow_update())

    body = f"""
<div class="card">
  <h2 style="margin-top:0">投稿设置（热更新）</h2>
  <p style="opacity:.75;margin:0">保存后立即生效；多实例部署可能存在短暂延迟，取决于各实例刷新策略。</p>
</div>

<div class="card">
  <form method="post" action="{ADMIN_WEB_PATH}/submit">
    <input type="hidden" name="action" value="save" />

	    <h3 style="margin-top:0" id="basic">基础限制</h3>
	    <div class="grid">
      <div>
        <label>最小字数（来源：{_src(runtime_settings.KEY_BOT_MIN_TEXT_LENGTH)}）</label>
        <input type="text" name="bot_min_text_length" value="{html.escape(str(min_len))}" />
      </div>
      <div>
        <label>最大字数（来源：{_src(runtime_settings.KEY_BOT_MAX_TEXT_LENGTH)}）</label>
        <input type="text" name="bot_max_text_length" value="{html.escape(str(max_len))}" />
      </div>
      <div>
        <label>最大标签数（0=不收集）（来源：{_src(runtime_settings.KEY_BOT_ALLOWED_TAGS)}）</label>
        <input type="text" name="bot_allowed_tags" value="{html.escape(str(allowed_tags))}" />
      </div>
      <div>
        <label>允许文件类型（* 或逗号分隔扩展名/MIME）（来源：{_src(runtime_settings.KEY_BOT_ALLOWED_FILE_TYPES)}）</label>
        <input type="text" name="bot_allowed_file_types" value="{html.escape(str(allowed_file_types))}" />
      </div>
      <div>
        <label>显示投稿人（来源：{_src(runtime_settings.KEY_BOT_SHOW_SUBMITTER)}）</label>
        <select name="bot_show_submitter">
          <option value="1" {_bool_selected(show_submitter, True)}>开启</option>
          <option value="0" {_bool_selected(show_submitter, False)}>关闭</option>
        </select>
      </div>
	      <div>
	        <label>通知所有者（来源：{_src(runtime_settings.KEY_BOT_NOTIFY_OWNER)}）</label>
	        <select name="bot_notify_owner">
	          <option value="1" {_bool_selected(notify_owner, True)}>开启</option>
	          <option value="0" {_bool_selected(notify_owner, False)}>关闭</option>
	        </select>
	      </div>
	    </div>

	    <div style="height:14px"></div>

	    <h3 style="margin:0 0 8px" id="upload">上传限制</h3>
	    <div class="grid">
	      <div>
	        <label>最多文档数量（来源：{_src(runtime_settings.KEY_UPLOAD_MAX_DOCS)}）</label>
	        <input type="text" name="upload_max_docs" value="{html.escape(str(max_docs))}" />
	      </div>
	      <div>
	        <label>最多媒体数量（非媒体模式）（来源：{_src(runtime_settings.KEY_UPLOAD_MAX_MEDIA_DEFAULT)}）</label>
	        <input type="text" name="upload_max_media_default" value="{html.escape(str(max_media_default))}" />
	      </div>
	      <div>
	        <label>最多媒体数量（媒体模式）（来源：{_src(runtime_settings.KEY_UPLOAD_MAX_MEDIA_MEDIA_MODE)}）</label>
	        <input type="text" name="upload_max_media_media_mode" value="{html.escape(str(max_media_media_mode))}" />
	      </div>
	      <div>
	        <label>媒体模式必须至少 1 个媒体（来源：{_src(runtime_settings.KEY_UPLOAD_MEDIA_MODE_REQUIRE_ONE)}）</label>
	        <select name="upload_media_mode_require_one">
	          <option value="1" {_bool_selected(media_mode_require_one, True)}>开启</option>
	          <option value="0" {_bool_selected(media_mode_require_one, False)}>关闭</option>
	        </select>
	      </div>
	    </div>

	    <h3 style="margin:0 0 8px" id="duplicate">重复检测 & 频率限制</h3>
	    <div class="grid">
      <div>
        <label>启用重复检测（来源：{_src(runtime_settings.KEY_DUPLICATE_CHECK_ENABLED)}）</label>
        <select name="dup_enabled">
          <option value="1" {_bool_selected(dup_enabled, True)}>开启</option>
          <option value="0" {_bool_selected(dup_enabled, False)}>关闭</option>
        </select>
      </div>
      <div>
        <label>检测窗口（天）（来源：{_src(runtime_settings.KEY_DUPLICATE_CHECK_WINDOW_DAYS)}）</label>
        <input type="text" name="dup_window_days" value="{html.escape(str(dup_window_days))}" />
      </div>
      <div>
        <label>相似度阈值（0~1）（来源：{_src(runtime_settings.KEY_DUPLICATE_SIMILARITY_THRESHOLD)}）</label>
        <input type="text" name="dup_similarity_threshold" value="{html.escape(f'{dup_threshold:.3f}')}" />
      </div>
      <div>
        <label>URL 检测（来源：{_src(runtime_settings.KEY_DUPLICATE_CHECK_URLS)}）</label>
        <select name="dup_check_urls">
          <option value="1" {_bool_selected(dup_check_urls, True)}>开启</option>
          <option value="0" {_bool_selected(dup_check_urls, False)}>关闭</option>
        </select>
      </div>
      <div>
        <label>联系方式检测（来源：{_src(runtime_settings.KEY_DUPLICATE_CHECK_CONTACTS)}）</label>
        <select name="dup_check_contacts">
          <option value="1" {_bool_selected(dup_check_contacts, True)}>开启</option>
          <option value="0" {_bool_selected(dup_check_contacts, False)}>关闭</option>
        </select>
      </div>
      <div>
        <label>TG 链接/用户名检测（来源：{_src(runtime_settings.KEY_DUPLICATE_CHECK_TG_LINKS)}）</label>
        <select name="dup_check_tg_links">
          <option value="1" {_bool_selected(dup_check_tg_links, True)}>开启</option>
          <option value="0" {_bool_selected(dup_check_tg_links, False)}>关闭</option>
        </select>
      </div>
      <div>
        <label>个人签名检测（来源：{_src(runtime_settings.KEY_DUPLICATE_CHECK_USER_BIO)}）</label>
        <select name="dup_check_user_bio">
          <option value="1" {_bool_selected(dup_check_user_bio, True)}>开启</option>
          <option value="0" {_bool_selected(dup_check_user_bio, False)}>关闭</option>
        </select>
      </div>
      <div>
        <label>内容相似度检测（来源：{_src(runtime_settings.KEY_DUPLICATE_CHECK_CONTENT_HASH)}）</label>
        <select name="dup_check_content_hash">
          <option value="1" {_bool_selected(dup_check_content_hash, True)}>开启</option>
          <option value="0" {_bool_selected(dup_check_content_hash, False)}>关闭</option>
        </select>
      </div>
      <div>
        <label>自动拒绝重复（来源：{_src(runtime_settings.KEY_DUPLICATE_AUTO_REJECT)}）</label>
        <select name="dup_auto_reject">
          <option value="1" {_bool_selected(dup_auto_reject, True)}>开启</option>
          <option value="0" {_bool_selected(dup_auto_reject, False)}>关闭</option>
        </select>
      </div>
      <div>
        <label>通知用户重复原因（来源：{_src(runtime_settings.KEY_DUPLICATE_NOTIFY_USER)}）</label>
        <select name="dup_notify_user">
          <option value="1" {_bool_selected(dup_notify_user, True)}>开启</option>
          <option value="0" {_bool_selected(dup_notify_user, False)}>关闭</option>
        </select>
      </div>
    </div>

    <div style="height:10px"></div>
    <div class="grid">
      <div>
        <label>启用频率限制（来源：{_src(runtime_settings.KEY_RATE_LIMIT_ENABLED)}）</label>
        <select name="rate_enabled">
          <option value="1" {_bool_selected(rate_enabled, True)}>开启</option>
          <option value="0" {_bool_selected(rate_enabled, False)}>关闭</option>
        </select>
      </div>
      <div>
        <label>窗口内最多投稿次数（来源：{_src(runtime_settings.KEY_RATE_LIMIT_COUNT)}）</label>
        <input type="text" name="rate_count" value="{html.escape(str(rate_count))}" />
      </div>
      <div>
        <label>窗口时长（小时）（来源：{_src(runtime_settings.KEY_RATE_LIMIT_WINDOW_HOURS)}）</label>
        <input type="text" name="rate_window_hours" value="{html.escape(str(rate_window_hours))}" />
      </div>
    </div>

    <div style="height:14px"></div>
    <h3 style="margin:0 0 8px" id="rating">评分</h3>
    <div class="grid">
      <div>
        <label>启用评分（来源：{_src(runtime_settings.KEY_RATING_ENABLED)}）</label>
        <select name="rating_enabled">
          <option value="1" {_bool_selected(rating_enabled, True)}>开启</option>
          <option value="0" {_bool_selected(rating_enabled, False)}>关闭</option>
        </select>
      </div>
      <div>
        <label>允许修改评分（来源：{_src(runtime_settings.KEY_RATING_ALLOW_UPDATE)}）</label>
        <select name="rating_allow_update">
          <option value="1" {_bool_selected(rating_allow_update, True)}>开启</option>
          <option value="0" {_bool_selected(rating_allow_update, False)}>关闭</option>
        </select>
      </div>
    </div>

    <div style="height:12px"></div>
    <button type="submit">保存</button>
    <button type="submit" name="action" value="clear" class="danger">清除 DB 覆盖（回退 config.ini）</button>
  </form>
</div>
"""
    return _html_page(title="投稿设置", body=body)


async def submit_post(request: web.Request) -> web.Response:
    _require_auth(request)
    form = await request.post()

    action = str(form.get("action") or "save").strip().lower()
    keys_all = [
        runtime_settings.KEY_BOT_MIN_TEXT_LENGTH,
        runtime_settings.KEY_BOT_MAX_TEXT_LENGTH,
        runtime_settings.KEY_BOT_ALLOWED_TAGS,
        runtime_settings.KEY_BOT_ALLOWED_FILE_TYPES,
        runtime_settings.KEY_BOT_SHOW_SUBMITTER,
        runtime_settings.KEY_BOT_NOTIFY_OWNER,
        runtime_settings.KEY_UPLOAD_MAX_DOCS,
        runtime_settings.KEY_UPLOAD_MAX_MEDIA_DEFAULT,
        runtime_settings.KEY_UPLOAD_MAX_MEDIA_MEDIA_MODE,
        runtime_settings.KEY_UPLOAD_MEDIA_MODE_REQUIRE_ONE,
        runtime_settings.KEY_DUPLICATE_CHECK_ENABLED,
        runtime_settings.KEY_DUPLICATE_CHECK_WINDOW_DAYS,
        runtime_settings.KEY_DUPLICATE_SIMILARITY_THRESHOLD,
        runtime_settings.KEY_DUPLICATE_CHECK_URLS,
        runtime_settings.KEY_DUPLICATE_CHECK_CONTACTS,
        runtime_settings.KEY_DUPLICATE_CHECK_TG_LINKS,
        runtime_settings.KEY_DUPLICATE_CHECK_USER_BIO,
        runtime_settings.KEY_DUPLICATE_CHECK_CONTENT_HASH,
        runtime_settings.KEY_DUPLICATE_AUTO_REJECT,
        runtime_settings.KEY_DUPLICATE_NOTIFY_USER,
        runtime_settings.KEY_RATE_LIMIT_ENABLED,
        runtime_settings.KEY_RATE_LIMIT_COUNT,
        runtime_settings.KEY_RATE_LIMIT_WINDOW_HOURS,
        runtime_settings.KEY_RATING_ENABLED,
        runtime_settings.KEY_RATING_ALLOW_UPDATE,
    ]

    if action == "clear":
        await runtime_settings.unset_many(keys=keys_all)
        raise web.HTTPFound(location=f"{ADMIN_WEB_PATH}/submit")

    def _t(name: str) -> str:
        return str(form.get(name) or "").strip()

    try:
        min_len = int(_t("bot_min_text_length") or "0")
        max_len = int(_t("bot_max_text_length") or "0")
        runtime_settings.validate_bot_text_length(min_len=min_len, max_len=max_len)

        allowed_tags = int(_t("bot_allowed_tags") or "0")
        runtime_settings.validate_bot_allowed_tags(allowed_tags)

        allowed_file_types = _t("bot_allowed_file_types") or "*"
        runtime_settings.validate_bot_allowed_file_types(allowed_file_types)

        show_submitter = _t("bot_show_submitter") == "1"
        notify_owner = _t("bot_notify_owner") == "1"

        upload_max_docs = int(_t("upload_max_docs") or "0")
        upload_max_media_default = int(_t("upload_max_media_default") or "0")
        upload_max_media_media_mode = int(_t("upload_max_media_media_mode") or "0")
        runtime_settings.validate_upload_limits(
            max_docs=upload_max_docs,
            max_media_default=upload_max_media_default,
            max_media_media_mode=upload_max_media_media_mode,
        )
        upload_media_mode_require_one = _t("upload_media_mode_require_one") == "1"

        dup_enabled = _t("dup_enabled") == "1"
        dup_window_days = int(_t("dup_window_days") or "0")
        runtime_settings.validate_duplicate_check_window_days(dup_window_days)

        dup_similarity_threshold = float(_t("dup_similarity_threshold") or "0")
        runtime_settings.validate_duplicate_similarity_threshold(dup_similarity_threshold)

        dup_check_urls = _t("dup_check_urls") == "1"
        dup_check_contacts = _t("dup_check_contacts") == "1"
        dup_check_tg_links = _t("dup_check_tg_links") == "1"
        dup_check_user_bio = _t("dup_check_user_bio") == "1"
        dup_check_content_hash = _t("dup_check_content_hash") == "1"
        dup_auto_reject = _t("dup_auto_reject") == "1"
        dup_notify_user = _t("dup_notify_user") == "1"

        rate_enabled = _t("rate_enabled") == "1"
        rate_count = int(_t("rate_count") or "0")
        rate_window_hours = int(_t("rate_window_hours") or "0")
        runtime_settings.validate_rate_limit(count=rate_count, window_hours=rate_window_hours)

        rating_enabled = _t("rating_enabled") == "1"
        rating_allow_update = _t("rating_allow_update") == "1"
    except Exception as e:
        return _html_page(title="保存失败", body=f"<div class='card'><h2 style='margin-top:0'>保存失败</h2><p>{html.escape(str(e))}</p></div>")

    await runtime_settings.set_many(values={
        runtime_settings.KEY_BOT_MIN_TEXT_LENGTH: str(min_len),
        runtime_settings.KEY_BOT_MAX_TEXT_LENGTH: str(max_len),
        runtime_settings.KEY_BOT_ALLOWED_TAGS: str(allowed_tags),
        runtime_settings.KEY_BOT_ALLOWED_FILE_TYPES: str(allowed_file_types).strip(),
        runtime_settings.KEY_BOT_SHOW_SUBMITTER: "1" if show_submitter else "0",
        runtime_settings.KEY_BOT_NOTIFY_OWNER: "1" if notify_owner else "0",
        runtime_settings.KEY_UPLOAD_MAX_DOCS: str(upload_max_docs),
        runtime_settings.KEY_UPLOAD_MAX_MEDIA_DEFAULT: str(upload_max_media_default),
        runtime_settings.KEY_UPLOAD_MAX_MEDIA_MEDIA_MODE: str(upload_max_media_media_mode),
        runtime_settings.KEY_UPLOAD_MEDIA_MODE_REQUIRE_ONE: "1" if upload_media_mode_require_one else "0",
        runtime_settings.KEY_DUPLICATE_CHECK_ENABLED: "1" if dup_enabled else "0",
        runtime_settings.KEY_DUPLICATE_CHECK_WINDOW_DAYS: str(dup_window_days),
        runtime_settings.KEY_DUPLICATE_SIMILARITY_THRESHOLD: str(dup_similarity_threshold),
        runtime_settings.KEY_DUPLICATE_CHECK_URLS: "1" if dup_check_urls else "0",
        runtime_settings.KEY_DUPLICATE_CHECK_CONTACTS: "1" if dup_check_contacts else "0",
        runtime_settings.KEY_DUPLICATE_CHECK_TG_LINKS: "1" if dup_check_tg_links else "0",
        runtime_settings.KEY_DUPLICATE_CHECK_USER_BIO: "1" if dup_check_user_bio else "0",
        runtime_settings.KEY_DUPLICATE_CHECK_CONTENT_HASH: "1" if dup_check_content_hash else "0",
        runtime_settings.KEY_DUPLICATE_AUTO_REJECT: "1" if dup_auto_reject else "0",
        runtime_settings.KEY_DUPLICATE_NOTIFY_USER: "1" if dup_notify_user else "0",
        runtime_settings.KEY_RATE_LIMIT_ENABLED: "1" if rate_enabled else "0",
        runtime_settings.KEY_RATE_LIMIT_COUNT: str(rate_count),
        runtime_settings.KEY_RATE_LIMIT_WINDOW_HOURS: str(rate_window_hours),
        runtime_settings.KEY_RATING_ENABLED: "1" if rating_enabled else "0",
        runtime_settings.KEY_RATING_ALLOW_UPDATE: "1" if rating_allow_update else "0",
    })
    raise web.HTTPFound(location=f"{ADMIN_WEB_PATH}/submit")


def _safe_json_textarea_value(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True)
    except Exception:
        return "{}"


def _tri_bool_selected(value: Optional[bool], expected: Optional[bool]) -> str:
    return "selected" if value is expected else ""


def _tri_bool_select(*, name: str, value: Optional[bool], label_inherit: str = "继承（全局默认）") -> str:
    return (
        f"<select name=\"{html.escape(name)}\">"
        f"<option value=\"\" {_tri_bool_selected(value, None)}>{html.escape(label_inherit)}</option>"
        f"<option value=\"1\" {_tri_bool_selected(value, True)}>开启</option>"
        f"<option value=\"0\" {_tri_bool_selected(value, False)}>关闭</option>"
        f"</select>"
    )


def _ov_get(overrides: Any, section: str, key: str) -> Any:
    if not isinstance(overrides, dict):
        return None
    sec = overrides.get(section)
    if not isinstance(sec, dict):
        return None
    return sec.get(key)


def _ov_bool(overrides: Any, section: str, key: str) -> Optional[bool]:
    v = _ov_get(overrides, section, key)
    if isinstance(v, bool):
        return v
    return None


def _ov_num_str(overrides: Any, section: str, key: str) -> str:
    v = _ov_get(overrides, section, key)
    if v is None:
        return ""
    return str(v)


def _profile_simple_fields(*, overrides: dict) -> str:
    """
    小白友好的策略档位编辑表单：
    - 布尔：三态（继承/开启/关闭）
    - 数值：留空=继承
    """
    # 频率限制
    rl_enabled = _ov_bool(overrides, "rate_limit", "enabled")
    rl_count = _ov_num_str(overrides, "rate_limit", "count")
    rl_window = _ov_num_str(overrides, "rate_limit", "window_hours")

    ai_mode = str(_ov_get(overrides, "ai_review", "mode") or "").strip()
    if ai_mode not in ("skip", "run_no_auto_reject", "manual_only"):
        ai_mode = ""

    # 重复检测
    dc_enabled = _ov_bool(overrides, "duplicate_check", "enabled")
    dc_window = _ov_num_str(overrides, "duplicate_check", "window_days")
    dc_threshold = _ov_num_str(overrides, "duplicate_check", "similarity_threshold")
    dc_urls = _ov_bool(overrides, "duplicate_check", "check_urls")
    dc_contacts = _ov_bool(overrides, "duplicate_check", "check_contacts")
    dc_tg = _ov_bool(overrides, "duplicate_check", "check_tg_links")
    dc_bio = _ov_bool(overrides, "duplicate_check", "check_user_bio")
    dc_hash = _ov_bool(overrides, "duplicate_check", "check_content_hash")
    dc_auto_reject = _ov_bool(overrides, "duplicate_check", "auto_reject")
    dc_notify_user = _ov_bool(overrides, "duplicate_check", "notify_user")

    # 基础限制
    tl_min = _ov_num_str(overrides, "text_length", "min_len")
    tl_max = _ov_num_str(overrides, "text_length", "max_len")
    tags_enabled = _ov_bool(overrides, "tags", "enabled")
    tags_max = _ov_num_str(overrides, "tags", "max_tags")
    allowed_file_types = str(_ov_get(overrides, "file_types", "allowed_file_types") or "")

    # 上传限制
    ul_docs = _ov_num_str(overrides, "upload_limits", "max_docs")
    ul_media_default = _ov_num_str(overrides, "upload_limits", "max_media_default")
    ul_media_media_mode = _ov_num_str(overrides, "upload_limits", "max_media_media_mode")
    ul_media_req_one = _ov_bool(overrides, "upload_limits", "media_mode_require_one")

    # 其它
    bot_show_submitter = _ov_bool(overrides, "bot", "show_submitter")
    bot_notify_owner = _ov_bool(overrides, "bot", "notify_owner")
    rating_enabled = _ov_bool(overrides, "rating", "enabled")
    rating_allow_update = _ov_bool(overrides, "rating", "allow_update")

    def _num_input(name: str, value: str, placeholder: str) -> str:
        return (
            f"<input type=\"text\" name=\"{html.escape(name)}\" value=\"{html.escape(value)}\" "
            f"placeholder=\"{html.escape(placeholder)}\" />"
        )

    return f"""
    <div class="card" style="margin-top:10px">
      <h4 style="margin:0 0 10px">简单模式（推荐）</h4>
      <p style="opacity:.75;margin:0 0 10px">留空/继承 = 使用全局默认（见“投稿设置”页面）。</p>

      <h4 style="margin:10px 0 8px">AI 审核</h4>
      <div class="grid">
        <div>
          <label>AI 审核模式</label>
          <select name="ai_mode">
            <option value="" {"selected" if ai_mode=="" else ""}>继承（全局默认）</option>
            <option value="skip" {"selected" if ai_mode=="skip" else ""}>跳过 AI 审核（直接按其他规则）</option>
            <option value="run_no_auto_reject" {"selected" if ai_mode=="run_no_auto_reject" else ""}>运行 AI，但不自动拒绝（命中则转人工）</option>
            <option value="manual_only" {"selected" if ai_mode=="manual_only" else ""}>仅人工审核（不运行 AI）</option>
          </select>
        </div>
      </div>

      <h4 style="margin:10px 0 8px">频率限制</h4>
      <div class="grid">
        <div>
          <label>启用</label>
          {_tri_bool_select(name="rl_enabled", value=rl_enabled)}
        </div>
        <div>
          <label>次数上限（1~20，留空=继承）</label>
          {_num_input("rl_count", rl_count, "留空=继承")}
        </div>
        <div>
          <label>窗口小时（1~168，留空=继承）</label>
          {_num_input("rl_window_hours", rl_window, "留空=继承")}
        </div>
      </div>

      <h4 style="margin:12px 0 8px">重复检测</h4>
      <div class="grid">
        <div>
          <label>启用</label>
          {_tri_bool_select(name="dc_enabled", value=dc_enabled)}
        </div>
        <div>
          <label>检测窗口天数（1~3650，留空=继承）</label>
          {_num_input("dc_window_days", dc_window, "留空=继承")}
        </div>
        <div>
          <label>相似度阈值（0~1，留空=继承）</label>
          {_num_input("dc_similarity_threshold", dc_threshold, "留空=继承")}
        </div>
        <div>
          <label>URL 检测</label>
          {_tri_bool_select(name="dc_check_urls", value=dc_urls)}
        </div>
        <div>
          <label>联系方式检测</label>
          {_tri_bool_select(name="dc_check_contacts", value=dc_contacts)}
        </div>
        <div>
          <label>TG 链接/用户名检测</label>
          {_tri_bool_select(name="dc_check_tg_links", value=dc_tg)}
        </div>
        <div>
          <label>个人签名检测</label>
          {_tri_bool_select(name="dc_check_user_bio", value=dc_bio)}
        </div>
        <div>
          <label>内容相似度检测</label>
          {_tri_bool_select(name="dc_check_content_hash", value=dc_hash)}
        </div>
        <div>
          <label>命中后自动拒绝</label>
          {_tri_bool_select(name="dc_auto_reject", value=dc_auto_reject)}
        </div>
        <div>
          <label>通知用户重复原因</label>
          {_tri_bool_select(name="dc_notify_user", value=dc_notify_user)}
        </div>
      </div>

      <h4 style="margin:12px 0 8px">基础限制</h4>
      <div class="grid">
        <div>
          <label>最小字数（1~4000，留空=继承）</label>
          {_num_input("tl_min_len", tl_min, "留空=继承")}
        </div>
        <div>
          <label>最大字数（1~4000，留空=继承）</label>
          {_num_input("tl_max_len", tl_max, "留空=继承")}
        </div>
        <div>
          <label>收集标签</label>
          {_tri_bool_select(name="tags_enabled", value=tags_enabled)}
        </div>
        <div>
          <label>最大标签数（0~50，留空=继承）</label>
          {_num_input("tags_max_tags", tags_max, "留空=继承")}
        </div>
        <div>
          <label>允许文件类型（* 或逗号分隔扩展名/MIME，留空=继承）</label>
          <input type="text" name="file_allowed_types" value="{html.escape(allowed_file_types)}" placeholder="留空=继承" />
        </div>
      </div>

      <h4 style="margin:12px 0 8px">上传限制</h4>
      <div class="grid">
        <div>
          <label>最多文档数量（1~50，留空=继承）</label>
          {_num_input("ul_max_docs", ul_docs, "留空=继承")}
        </div>
        <div>
          <label>最多媒体数量（非媒体模式，0~50，留空=继承）</label>
          {_num_input("ul_max_media_default", ul_media_default, "留空=继承")}
        </div>
        <div>
          <label>最多媒体数量（媒体模式，1~200，留空=继承）</label>
          {_num_input("ul_max_media_media_mode", ul_media_media_mode, "留空=继承")}
        </div>
        <div>
          <label>媒体模式必须至少 1 个媒体</label>
          {_tri_bool_select(name="ul_media_mode_require_one", value=ul_media_req_one)}
        </div>
      </div>

      <h4 style="margin:12px 0 8px">其它</h4>
      <div class="grid">
        <div>
          <label>显示投稿人</label>
          {_tri_bool_select(name="bot_show_submitter", value=bot_show_submitter)}
        </div>
        <div>
          <label>通知所有者</label>
          {_tri_bool_select(name="bot_notify_owner", value=bot_notify_owner)}
        </div>
        <div>
          <label>启用评分</label>
          {_tri_bool_select(name="rating_enabled", value=rating_enabled)}
        </div>
        <div>
          <label>允许修改评分</label>
          {_tri_bool_select(name="rating_allow_update", value=rating_allow_update)}
        </div>
      </div>
    </div>
    """


async def whitelist_users_get(request: web.Request) -> web.Response:
    _require_auth(request)
    base = ADMIN_WEB_PATH.rstrip("/")

    profiles = submit_policy.list_profiles()
    users = submit_policy.list_users()

    def _profile_options(*, selected: str = "") -> str:
        sel = str(selected or "").strip()
        return "\n".join(
            (
                f"<option value=\"{html.escape(p.profile_id)}\" "
                f"{'selected' if p.profile_id == sel else ''}>"
                f"{html.escape(p.profile_id)} - {html.escape(p.name)}"
                f"</option>"
            )
            for p in profiles
        )

    rows = []
    for u in users:
        rows.append(
            f"""
            <tr>
              <td>{html.escape(str(u.user_id))}</td>
              <td>
                <form method="post" action="{ADMIN_WEB_PATH}/whitelist" class="row" style="margin:0">
                  <input type="hidden" name="action" value="user_save" />
                  <input type="hidden" name="user_id" value="{html.escape(str(u.user_id))}" />
                  <select name="profile_id" style="min-width:220px">
                    {_profile_options(selected=u.profile_id)}
                  </select>
              </td>
              <td>
                  <input type="text" name="username" value="{html.escape(u.username or '')}" placeholder="@someone（可不填）" />
              </td>
              <td>
                  <input type="text" name="note" value="{html.escape(u.note or '')}" placeholder="备注" />
              </td>
              <td>
                  <button type="submit">保存</button>
                </form>
                <form method="post" action="{ADMIN_WEB_PATH}/whitelist" class="row" style="margin:0;margin-top:6px">
                  <input type="hidden" name="action" value="user_delete" />
                  <input type="hidden" name="user_id" value="{html.escape(str(u.user_id))}" />
                  <button type="submit" class="danger" onclick="return confirm('确认移除该白名单用户？');">移除</button>
                </form>
              </td>
            </tr>
            """
        )

    if not profiles:
        add_hint = (
            f"<p style='opacity:.75;margin:0'>尚未创建策略档位，请先到 "
            f"<a href='{base}/whitelist/profiles'>策略档位</a> 新建。</p>"
        )
    else:
        add_hint = ""

    body = f"""
<div class="card">
  <h2 style="margin-top:0">投稿白名单</h2>
  <p style="opacity:.75;margin:0">白名单用户会绑定到某个“策略档位（Profile）”，以放宽/关闭常见限制（热更新）。</p>
  <div style="height:8px"></div>
  <div class="row">
    <a href="{base}/whitelist/profiles"><button>管理策略档位</button></a>
    <a href="{base}/submit#basic"><button>全局默认（投稿设置）</button></a>
  </div>
</div>

<div class="card">
  <h3 style="margin-top:0">添加/更新白名单用户</h3>
  {add_hint}
  <form method="post" action="{ADMIN_WEB_PATH}/whitelist">
    <input type="hidden" name="action" value="user_save" />
    <div class="grid">
      <div>
        <label>用户 ID（数字）</label>
        <input type="text" name="user_id" placeholder="例如 123456789" />
      </div>
      <div>
        <label>策略档位（Profile）</label>
        <select name="profile_id">
          {_profile_options()}
        </select>
      </div>
      <div>
        <label>用户名（可选，仅用于展示）</label>
        <input type="text" name="username" placeholder="@someone（可不填）" />
      </div>
      <div>
        <label>备注（可选）</label>
        <input type="text" name="note" placeholder="例如 赞助/VIP/特殊合作" />
      </div>
    </div>
    <div style="height:12px"></div>
    <button type="submit" {"disabled" if not profiles else ""}>保存</button>
  </form>
</div>

<div class="card">
  <h3 style="margin-top:0">当前白名单用户</h3>
  <table>
    <thead>
      <tr>
        <th>user_id</th>
        <th>profile</th>
        <th>username</th>
        <th>note</th>
        <th>操作</th>
      </tr>
    </thead>
    <tbody>
      {''.join(rows) if rows else '<tr><td colspan=\"5\" style=\"opacity:.75\">暂无白名单用户</td></tr>'}
    </tbody>
  </table>
</div>
"""
    return _html_page(title="投稿白名单", body=body)


async def whitelist_users_post(request: web.Request) -> web.Response:
    _require_auth(request)
    form = await request.post()
    action = str(form.get("action") or "").strip().lower()

    try:
        if action == "user_save":
            user_id = int(str(form.get("user_id") or "0").strip())
            profile_id = str(form.get("profile_id") or "").strip()
            username = str(form.get("username") or "").strip()
            note = str(form.get("note") or "").strip()
            if user_id <= 0:
                raise ValueError("user_id 必须是正整数")
            await submit_policy.upsert_user(user_id=user_id, profile_id=profile_id, username=username, note=note)
        elif action == "user_delete":
            user_id = int(str(form.get("user_id") or "0").strip())
            if user_id <= 0:
                raise ValueError("user_id 必须是正整数")
            await submit_policy.delete_user(user_id=user_id)
        else:
            raise ValueError("不支持的 action")
    except Exception as e:
        return _html_page(title="保存失败", body=f"<div class='card'><h2 style='margin-top:0'>保存失败</h2><p>{html.escape(str(e))}</p></div>")

    raise web.HTTPFound(location=f"{ADMIN_WEB_PATH}/whitelist")


async def whitelist_profiles_get(request: web.Request) -> web.Response:
    _require_auth(request)
    base = ADMIN_WEB_PATH.rstrip("/")

    profiles = submit_policy.list_profiles()
    users = submit_policy.list_users()
    used_count: Dict[str, int] = {}
    for u in users:
        used_count[u.profile_id] = used_count.get(u.profile_id, 0) + 1

    example = {
        "duplicate_check": {"auto_reject": False},
        "rate_limit": {"enabled": False},
        "upload_limits": {"max_media_media_mode": 80},
    }

    profile_cards = []
    for p in profiles:
        count = used_count.get(p.profile_id, 0)
        profile_cards.append(
            f"""
            <div class="card">
              <h3 style="margin-top:0">{html.escape(p.profile_id)} <span class="pill">users: {count}</span></h3>
              <form method="post" action="{ADMIN_WEB_PATH}/whitelist/profiles">
                <input type="hidden" name="action" value="profile_save" />
                <input type="hidden" name="profile_id" value="{html.escape(p.profile_id)}" />
                <div class="grid">
                  <div>
                    <label>名称</label>
                    <input type="text" name="name" value="{html.escape(p.name)}" />
                  </div>
                </div>
                {_profile_simple_fields(overrides=p.overrides)}
                <details class="card" style="margin-top:10px">
                  <summary style="cursor:pointer">高级：直接编辑 overrides_json（可选）</summary>
                  <div style="height:10px"></div>
                  <label>overrides_json（只写要覆盖的字段；未写则继承全局默认）</label>
                  <textarea name="overrides_json" placeholder="留空则使用简单模式生成的覆盖项">{html.escape(_safe_json_textarea_value(p.overrides))}</textarea>
                  <div style="height:8px"></div>
                  <label><input type="checkbox" name="use_raw_json" value="1" /> 使用上面的 JSON 覆盖（勾选才会生效）</label>
                </details>
                <div class="row" style="margin-top:10px">
                  <button type="submit">保存</button>
                  <button type="submit" name="action" value="profile_delete" class="danger" formaction="{ADMIN_WEB_PATH}/whitelist/profiles" formmethod="post"
                    onclick="return confirm('确认删除该策略档位？（若仍被用户使用将被拒绝）');"
                  >删除</button>
                </div>
              </form>
            </div>
            """
        )

    body = f"""
<div class="card">
  <h2 style="margin-top:0">策略档位（Profiles）</h2>
  <p style="opacity:.75;margin:0">
    说明：Profile 仅存“覆盖项”，未填写的字段会继承全局默认（见 <a href="{base}/submit">投稿设置</a>）。
    修改后立即生效（多实例部署可能存在短暂延迟）。
  </p>
  <div style="height:8px"></div>
  <div class="row">
    <a href="{base}/whitelist"><button>返回白名单用户</button></a>
  </div>
</div>

<div class="card">
  <h3 style="margin-top:0">新建策略档位</h3>
  <form method="post" action="{ADMIN_WEB_PATH}/whitelist/profiles">
    <input type="hidden" name="action" value="profile_save" />
    <div class="grid">
      <div>
        <label>profile_id（英文/数字/下划线，建议如 trusted/vip）</label>
        <input type="text" name="profile_id" placeholder="trusted" />
      </div>
      <div>
        <label>名称（可选）</label>
        <input type="text" name="name" placeholder="可信投稿人" />
      </div>
    </div>
    {_profile_simple_fields(overrides={})}
    <details class="card" style="margin-top:10px">
      <summary style="cursor:pointer">高级：直接编辑 overrides_json（可选）</summary>
      <div style="height:10px"></div>
      <label>overrides_json（示例）</label>
      <textarea name="overrides_json">{html.escape(_safe_json_textarea_value(example))}</textarea>
      <div style="height:8px"></div>
      <label><input type="checkbox" name="use_raw_json" value="1" /> 使用上面的 JSON 覆盖（勾选才会生效）</label>
    </details>
    <div style="height:12px"></div>
    <button type="submit">创建/保存</button>
  </form>
  <p style="opacity:.75;margin-bottom:0">
    支持的字段：rate_limit / duplicate_check / text_length / tags / file_types / upload_limits / bot / rating。
  </p>
</div>

{''.join(profile_cards) if profile_cards else "<div class='card' style='opacity:.75'>暂无策略档位</div>"}
"""
    return _html_page(title="策略档位", body=body)


async def whitelist_profiles_post(request: web.Request) -> web.Response:
    _require_auth(request)
    form = await request.post()
    action = str(form.get("action") or "").strip().lower()

    def _t(name: str) -> str:
        return str(form.get(name) or "").strip()

    def _tri_bool(name: str) -> Optional[bool]:
        v = _t(name)
        if not v:
            return None
        if v == "1":
            return True
        if v == "0":
            return False
        raise ValueError(f"{name} 值无效")

    def _opt_int(name: str) -> Optional[int]:
        s = _t(name)
        if not s:
            return None
        try:
            return int(s)
        except Exception:
            raise ValueError(f"{name} 必须是整数")

    def _opt_float(name: str) -> Optional[float]:
        s = _t(name)
        if not s:
            return None
        try:
            return float(s)
        except Exception:
            raise ValueError(f"{name} 必须是数字")

    def _in_range(name: str, value: int, lo: int, hi: int) -> None:
        if value < lo or value > hi:
            raise ValueError(f"{name} 范围应为 {lo}~{hi}")

    def _float_range(name: str, value: float, lo: float, hi: float) -> None:
        if value < lo or value > hi:
            raise ValueError(f"{name} 范围应为 {lo}~{hi}")

    def _build_overrides_from_simple_form() -> dict:
        """
        从简单模式表单构建 overrides：
        - 三态 bool：空=继承，不落库
        - 数字：空=继承，不落库
        """
        defaults = submit_policy.build_global_policy()
        overrides: Dict[str, Dict[str, Any]] = {}

        def put(section: str, key: str, value: Any) -> None:
            overrides.setdefault(section, {})[key] = value

        # 频率限制
        rl_enabled = _tri_bool("rl_enabled")
        if rl_enabled is not None:
            put("rate_limit", "enabled", rl_enabled)
        rl_count = _opt_int("rl_count")
        if rl_count is not None:
            _in_range("次数上限", rl_count, 1, 20)
            put("rate_limit", "count", rl_count)
        rl_window = _opt_int("rl_window_hours")
        if rl_window is not None:
            _in_range("窗口小时", rl_window, 1, 168)
            put("rate_limit", "window_hours", rl_window)

        # AI 审核
        ai_mode = _t("ai_mode")
        if ai_mode:
            allowed = {"skip", "run_no_auto_reject", "manual_only"}
            if ai_mode not in allowed:
                raise ValueError(f"AI 审核模式必须是 {sorted(allowed)} 之一")
            put("ai_review", "mode", ai_mode)

        # 重复检测
        dc_enabled = _tri_bool("dc_enabled")
        if dc_enabled is not None:
            put("duplicate_check", "enabled", dc_enabled)
        dc_window = _opt_int("dc_window_days")
        if dc_window is not None:
            _in_range("检测窗口天数", dc_window, 1, 3650)
            put("duplicate_check", "window_days", dc_window)
        dc_threshold = _opt_float("dc_similarity_threshold")
        if dc_threshold is not None:
            _float_range("相似度阈值", dc_threshold, 0.0, 1.0)
            put("duplicate_check", "similarity_threshold", dc_threshold)

        for field, key in (
            ("dc_check_urls", "check_urls"),
            ("dc_check_contacts", "check_contacts"),
            ("dc_check_tg_links", "check_tg_links"),
            ("dc_check_user_bio", "check_user_bio"),
            ("dc_check_content_hash", "check_content_hash"),
            ("dc_auto_reject", "auto_reject"),
            ("dc_notify_user", "notify_user"),
        ):
            v = _tri_bool(field)
            if v is not None:
                put("duplicate_check", key, v)

        # 基础限制
        tl_min = _opt_int("tl_min_len")
        tl_max = _opt_int("tl_max_len")
        if tl_min is not None:
            _in_range("最小字数", tl_min, 1, 4000)
        if tl_max is not None:
            _in_range("最大字数", tl_max, 1, 4000)
        eff_min = tl_min if tl_min is not None else int((defaults.get("text_length") or {}).get("min_len", 10))
        eff_max = tl_max if tl_max is not None else int((defaults.get("text_length") or {}).get("max_len", 4000))
        if eff_max < eff_min:
            raise ValueError("最大字数不能小于最小字数")
        if tl_min is not None:
            put("text_length", "min_len", tl_min)
        if tl_max is not None:
            put("text_length", "max_len", tl_max)

        tags_enabled = _tri_bool("tags_enabled")
        if tags_enabled is not None:
            put("tags", "enabled", tags_enabled)
        tags_max = _opt_int("tags_max_tags")
        if tags_max is not None:
            _in_range("最大标签数", tags_max, 0, 50)
            put("tags", "max_tags", tags_max)

        aft = _t("file_allowed_types")
        if aft:
            runtime_settings.validate_bot_allowed_file_types(aft)
            put("file_types", "allowed_file_types", aft)

        # 上传限制
        ul_docs = _opt_int("ul_max_docs")
        if ul_docs is not None:
            _in_range("最多文档数量", ul_docs, 1, 50)
            put("upload_limits", "max_docs", ul_docs)
        ul_media_default = _opt_int("ul_max_media_default")
        if ul_media_default is not None:
            _in_range("最多媒体数量（非媒体模式）", ul_media_default, 0, 50)
            put("upload_limits", "max_media_default", ul_media_default)
        ul_media_media_mode = _opt_int("ul_max_media_media_mode")
        if ul_media_media_mode is not None:
            _in_range("最多媒体数量（媒体模式）", ul_media_media_mode, 1, 200)
            put("upload_limits", "max_media_media_mode", ul_media_media_mode)
        ul_req_one = _tri_bool("ul_media_mode_require_one")
        if ul_req_one is not None:
            put("upload_limits", "media_mode_require_one", ul_req_one)

        # 其它
        v = _tri_bool("bot_show_submitter")
        if v is not None:
            put("bot", "show_submitter", v)
        v = _tri_bool("bot_notify_owner")
        if v is not None:
            put("bot", "notify_owner", v)
        v = _tri_bool("rating_enabled")
        if v is not None:
            put("rating", "enabled", v)
        v = _tri_bool("rating_allow_update")
        if v is not None:
            put("rating", "allow_update", v)

        return overrides

    try:
        if action == "profile_save":
            profile_id = str(form.get("profile_id") or "").strip()
            name = str(form.get("name") or "").strip()
            use_raw_json = _t("use_raw_json") == "1"
            if use_raw_json:
                overrides_raw = str(form.get("overrides_json") or "").strip() or "{}"
                overrides = json.loads(overrides_raw)
                if not isinstance(overrides, dict):
                    raise ValueError("overrides_json 必须是 JSON object")
            else:
                overrides = _build_overrides_from_simple_form()
            await submit_policy.upsert_profile(profile_id=profile_id, name=name, overrides=overrides)
        elif action == "profile_delete":
            profile_id = str(form.get("profile_id") or "").strip()
            await submit_policy.delete_profile(profile_id=profile_id)
        else:
            raise ValueError("不支持的 action")
    except Exception as e:
        return _html_page(title="保存失败", body=f"<div class='card'><h2 style='margin-top:0'>保存失败</h2><p>{html.escape(str(e))}</p></div>")

    raise web.HTTPFound(location=f"{ADMIN_WEB_PATH}/whitelist/profiles")


async def duplicate_get(request: web.Request) -> web.Response:
    _require_auth(request)
    raise web.HTTPFound(location=f"{ADMIN_WEB_PATH}/submit#duplicate")


async def duplicate_post(request: web.Request) -> web.Response:
    _require_auth(request)
    raise web.HTTPFound(location=f"{ADMIN_WEB_PATH}/submit#duplicate")


async def schedule_get(request: web.Request) -> web.Response:
    _require_auth(request)
    cfg = await get_sched_config()
    schedule_type = cfg.schedule_type
    payload = cfg.schedule_payload or {}
    time_value = str(payload.get("time") or "09:00")
    hours_value = str(payload.get("hours") or "24")

    body = f"""
<div class="card">
  <h2 style="margin-top:0">定时发布</h2>
  <div class="row">
    <span class="pill">enabled: {str(cfg.enabled)}</span>
    <span class="pill">auto_pin: {str(getattr(cfg, "auto_pin", False))}</span>
    <span class="pill">delete_prev: {str(getattr(cfg, "delete_prev", False))}</span>
    <span class="pill">next_run_at: {html.escape(_format_epoch(cfg.next_run_at))}</span>
    <span class="pill">last_run_at: {html.escape(_format_epoch(cfg.last_run_at))}</span>
    <span class="pill">last_message_id: {html.escape(str(cfg.last_message_id or '-'))}</span>
  </div>
</div>

<div class="card">
  <h3 style="margin-top:0">修改配置</h3>
  <form method="post" action="{ADMIN_WEB_PATH}/schedule">
    <div class="grid">
      <div>
        <label>启用</label>
        <select name="enabled">
          <option value="1" {"selected" if cfg.enabled else ""}>启用</option>
          <option value="0" {"selected" if not cfg.enabled else ""}>关闭</option>
        </select>
      </div>
      <div>
        <label>调度类型</label>
        <select name="schedule_type">
          <option value="daily_at" {"selected" if schedule_type=="daily_at" else ""}>每天固定时间（daily_at）</option>
          <option value="every_n_hours" {"selected" if schedule_type=="every_n_hours" else ""}>每 N 小时（every_n_hours）</option>
        </select>
      </div>
      <div>
        <label>daily_at 时间（HH:MM）</label>
        <input type="text" name="daily_time" value="{html.escape(time_value)}" />
      </div>
      <div>
        <label>every_n_hours 间隔（小时）</label>
        <input type="text" name="every_hours" value="{html.escape(hours_value)}" />
      </div>
      <div>
        <label>发出后自动置顶</label>
        <select name="auto_pin">
          <option value="1" {"selected" if getattr(cfg, "auto_pin", False) else ""}>开启</option>
          <option value="0" {"selected" if not getattr(cfg, "auto_pin", False) else ""}>关闭</option>
        </select>
      </div>
      <div>
        <label>发出后删除上一条定时消息</label>
        <select name="delete_prev">
          <option value="1" {"selected" if getattr(cfg, "delete_prev", False) else ""}>开启</option>
          <option value="0" {"selected" if not getattr(cfg, "delete_prev", False) else ""}>关闭</option>
        </select>
      </div>
    </div>
    <div style="height:12px"></div>
    <label>定时消息正文（支持 {{date}} / {{datetime}} 占位符；HTML 解析失败会自动降级为纯文本）</label>
    <p style="margin:6px 0 0;opacity:.8">
      占位符说明：<code>{{date}}</code> -> 服务器日期（YYYY-MM-DD），<code>{{datetime}}</code> -> 服务器时间（YYYY-MM-DD HH:MM:SS）。
    </p>
    <textarea name="message_text">{html.escape(cfg.message_text or "")}</textarea>
    <div style="height:12px"></div>
    <details style="border:1px solid rgba(127,127,127,.25);border-radius:10px;padding:12px 12px 10px">
      <summary style="cursor:pointer"><b>Telegram HTML 常用格式速查（点开/收起）</b></summary>
      <div style="height:10px"></div>
      <p style="margin-top:0;opacity:.8">本机器人以 <code>ParseMode.HTML</code> 发送定时消息。常用写法如下（复制到正文即可）：</p>
      <table>
        <thead>
          <tr><th>效果</th><th>写法</th></tr>
        </thead>
        <tbody>
          <tr><td>粗体</td><td><code>&lt;b&gt;粗体&lt;/b&gt;</code></td></tr>
          <tr><td>斜体</td><td><code>&lt;i&gt;斜体&lt;/i&gt;</code></td></tr>
          <tr><td>下划线</td><td><code>&lt;u&gt;下划线&lt;/u&gt;</code></td></tr>
          <tr><td>删除线</td><td><code>&lt;s&gt;删除线&lt;/s&gt;</code></td></tr>
          <tr><td>等宽（行内代码）</td><td><code>&lt;code&gt;code&lt;/code&gt;</code></td></tr>
          <tr><td>代码块</td><td><code>&lt;pre&gt;code block&lt;/pre&gt;</code></td></tr>
          <tr><td>引用</td><td><code>&lt;blockquote&gt;引用内容&lt;/blockquote&gt;</code></td></tr>
          <tr><td>剧透</td><td><code>&lt;span class="tg-spoiler"&gt;剧透内容&lt;/span&gt;</code></td></tr>
          <tr><td>链接</td><td><code>&lt;a href="https://example.com"&gt;链接文字&lt;/a&gt;</code></td></tr>
        </tbody>
      </table>
      <p style="opacity:.8;margin-bottom:0">提示：若 HTML 写法不合法，发送会自动降级为纯文本，避免整条定时消息丢失。</p>
    </details>
    <div style="height:12px"></div>
    <button type="submit">保存</button>
  </form>
</div>
"""
    return _html_page(title="定时发布", body=body)


async def schedule_post(request: web.Request) -> web.Response:
    _require_auth(request)
    form = await request.post()
    enabled = str(form.get("enabled") or "0").strip() == "1"
    schedule_type = str(form.get("schedule_type") or "daily_at").strip()
    daily_time = str(form.get("daily_time") or "09:00").strip()
    every_hours = str(form.get("every_hours") or "24").strip()
    auto_pin = str(form.get("auto_pin") or "0").strip() == "1"
    delete_prev = str(form.get("delete_prev") or "0").strip() == "1"
    message_text = str(form.get("message_text") or "")

    payload: Dict[str, Any]
    if schedule_type == "every_n_hours":
        try:
            hours = int(every_hours)
        except Exception:
            hours = 24
        payload = {"hours": max(1, hours)}
    else:
        payload = {"time": daily_time}
        schedule_type = "daily_at"

    now = time.time()
    try:
        next_run_at = compute_next_run_at(now=now, schedule_type=schedule_type, payload=payload, last_run_at=None)
    except Exception as e:
        return _html_page(title="保存失败", body=f"<div class='card'><h2 style='margin-top:0'>保存失败</h2><p>{html.escape(str(e))}</p></div>")

    await update_config_fields(
        enabled=1 if enabled else 0,
        schedule_type=schedule_type,
        schedule_payload=json.dumps(payload, ensure_ascii=False),
        message_text=message_text,
        auto_pin=1 if auto_pin else 0,
        delete_prev=1 if delete_prev else 0,
        next_run_at=float(next_run_at) if enabled else None,
    )
    raise web.HTTPFound(location=f"{ADMIN_WEB_PATH}/schedule")


def _preview_text(value: str, max_len: int = 80) -> str:
    s = (value or "").strip().replace("\r\n", "\n").replace("\r", "\n")
    s = " ".join([p for p in s.split("\n") if p.strip()])
    if len(s) <= max_len:
        return s
    return s[: max(0, max_len - 1)] + "…"


async def fallback_get(request: web.Request) -> web.Response:
    _require_auth(request)
    cfg = await get_fallback_config()
    payload = cfg.schedule_payload or {}
    time_value = str(payload.get("time") or "23:00")

    pool_enabled = await fallback_count_pool_items(enabled_only=True)
    pool_remaining = await fallback_count_pool_items(enabled_only=True, unused_cycle_id=int(cfg.cycle_id))
    runs = await fallback_list_recent_runs(limit=20)

    rows_html = []
    items = await fallback_list_pool_items(limit=100, offset=0)
    for it in items:
        pid = int(it.get("id") or 0)
        enabled = bool(int(it.get("enabled") or 0))
        used_cycle_id = int(it.get("used_cycle_id") or 0)
        used_tag = "已用" if used_cycle_id == int(cfg.cycle_id) else "未用"
        dn = html.escape(str(it.get("display_name") or ""))
        domain = html.escape(str(it.get("platform_domain") or ""))
        tg = html.escape(str(it.get("platform_tg_username") or ""))
        preview = html.escape(_preview_text(str(it.get("message_text") or "")))
        toggle_to = "0" if enabled else "1"
        toggle_text = "禁用" if enabled else "启用"

        rows_html.append(
            "<tr>"
            f"<td><code>{pid}</code></td>"
            f"<td>{'✅' if enabled else '❌'} <span class='pill'>{html.escape(used_tag)}</span></td>"
            f"<td>{dn}</td>"
            f"<td><code>{domain or '-'}</code></td>"
            f"<td><code>{('@' + tg) if tg else '-'}</code></td>"
            f"<td style='max-width:520px'>{preview}</td>"
            "<td>"
            f"<a href=\"{ADMIN_WEB_PATH}/fallback/pool/{pid}\"><button>编辑</button></a> "
            f"<form method=\"post\" action=\"{ADMIN_WEB_PATH}/fallback/pool/{pid}/toggle\" style=\"display:inline\">"
            f"<input type=\"hidden\" name=\"enabled\" value=\"{html.escape(toggle_to)}\" />"
            f"<button type=\"submit\" class=\"{'danger' if enabled else ''}\">{html.escape(toggle_text)}</button>"
            "</form>"
            "</td>"
            "</tr>"
        )

    runs_html = []
    for r in runs:
        rk = html.escape(str(r.get("run_key") or ""))
        st = html.escape(str(r.get("status") or ""))
        sat = _format_epoch(r.get("scheduled_at"))
        cnt = int(r.get("published_posts_count") or 0)
        picked = r.get("picked_pool_id")
        msg_id = r.get("sent_message_id")
        runs_html.append(
            "<tr>"
            f"<td><code>{rk}</code></td>"
            f"<td>{html.escape(sat)}</td>"
            f"<td><span class='pill'>{st}</span></td>"
            f"<td><code>{cnt}</code></td>"
            f"<td><code>{picked if picked is not None else '-'}</code></td>"
            f"<td><code>{msg_id if msg_id is not None else '-'}</code></td>"
            "</tr>"
        )

    body = f"""
<div class="card">
  <h2 style="margin-top:0">兜底定时发布（当天无投稿才发）</h2>
  <div class="row">
    <span class="pill">enabled: {str(cfg.enabled)}</span>
    <span class="pill">next_run_at: {html.escape(_format_epoch(cfg.next_run_at))}</span>
    <span class="pill">last_run_at: {html.escape(_format_epoch(cfg.last_run_at))}</span>
    <span class="pill">cycle_id: {html.escape(str(cfg.cycle_id))}</span>
    <span class="pill">miss_tolerance: {html.escape(str(cfg.miss_tolerance_seconds))}s</span>
    <span class="pill">header_len: {html.escape(str(len(getattr(cfg, "header_text", "") or "")))}</span>
    <span class="pill">footer_len: {html.escape(str(len(getattr(cfg, "footer_text", "") or "")))}</span>
    <span class="pill">pool_enabled: {html.escape(str(pool_enabled))}</span>
    <span class="pill">pool_remaining: {html.escape(str(pool_remaining))}</span>
  </div>
  <p style="opacity:.75;margin:10px 0 0">到点触发时会先查询 <code>published_posts</code> 当天是否已有发布记录；有则跳过，无则从预存池随机取一条（本周期不重复）。</p>
</div>

<div class="card">
  <h3 style="margin-top:0">配置</h3>
  <form method="post" action="{ADMIN_WEB_PATH}/fallback/config">
    <div class="grid">
      <div>
        <label>启用</label>
        <select name="enabled">
          <option value="1" {"selected" if cfg.enabled else ""}>启用</option>
          <option value="0" {"selected" if not cfg.enabled else ""}>关闭</option>
        </select>
      </div>
      <div>
        <label>每天固定时间（HH:MM）</label>
        <input type="text" name="daily_time" value="{html.escape(time_value)}" />
      </div>
      <div>
        <label>错过触发跳过阈值（秒）</label>
        <input type="text" name="miss_tolerance_seconds" value="{html.escape(str(cfg.miss_tolerance_seconds))}" />
      </div>
    </div>
    <div style="height:12px"></div>
    <label>固定前文（可选，留空则不显示）</label>
    <p style="margin:6px 0 0;opacity:.8">
      占位符：<code>{{date}}</code>、<code>{{datetime}}</code>、<code>{{platform_name}}</code>、<code>{{platform_domain}}</code>、<code>{{platform_tg_username}}</code>
    </p>
    <textarea name="header_text" placeholder="例如：📌 今日兜底推荐（{{date}}）">{html.escape(str(getattr(cfg, "header_text", "") or ""))}</textarea>
    <div style="height:12px"></div>
    <label>固定后文（可选，留空则不显示）</label>
    <textarea name="footer_text" placeholder="例如：#接码 #平台介绍  投稿请私信机器人">{html.escape(str(getattr(cfg, "footer_text", "") or ""))}</textarea>
    <div style="height:12px"></div>
    <button type="submit">保存</button>
  </form>
  <p style="opacity:.75;margin-bottom:0">提示：本功能发布后的按钮与投稿消息一致（评分键盘，如启用）。</p>
</div>

<div class="card">
  <h3 style="margin-top:0">新增预存消息</h3>
  <form method="post" action="{ADMIN_WEB_PATH}/fallback/pool/add">
    <div class="grid">
      <div>
        <label>平台名称（可选，仅用于后台展示）</label>
        <input type="text" name="display_name" placeholder="例如：XX接码平台" />
      </div>
      <div>
        <label>平台官网域名（优先）</label>
        <input type="text" name="platform_domain" placeholder="例如：example.com 或 https://example.com" />
      </div>
      <div>
        <label>TG 频道用户名（备用）</label>
        <input type="text" name="platform_tg_username" placeholder="例如：@channel_username 或 https://t.me/channel_username" />
      </div>
      <div>
        <label>启用</label>
        <select name="enabled">
          <option value="1" selected>启用</option>
          <option value="0">禁用</option>
        </select>
      </div>
    </div>
    <div style="height:12px"></div>
    <label>预存文案（支持 {{date}} / {{datetime}} 占位符；HTML 解析失败会自动降级为纯文本）</label>
    <textarea name="message_text" placeholder="请输入兜底发布文案（建议包含平台域名 / TG 频道 / 联系方式等）"></textarea>
    <div style="height:12px"></div>
    <button type="submit">添加</button>
  </form>
</div>

<div class="card">
  <h3 style="margin-top:0">消息池（最多显示 100 条）</h3>
  <table>
    <thead>
      <tr>
        <th>ID</th>
        <th>状态</th>
        <th>平台</th>
        <th>domain</th>
        <th>tg</th>
        <th>预览</th>
        <th>操作</th>
      </tr>
    </thead>
    <tbody>
      {"".join(rows_html) if rows_html else "<tr><td colspan='7' style='opacity:.8'>暂无预存消息</td></tr>"}
    </tbody>
  </table>
</div>

<div class="card">
  <h3 style="margin-top:0">最近运行记录</h3>
  <table>
    <thead>
      <tr>
        <th>run_key</th>
        <th>scheduled_at</th>
        <th>status</th>
        <th>published_count</th>
        <th>picked_pool_id</th>
        <th>sent_message_id</th>
      </tr>
    </thead>
    <tbody>
      {"".join(runs_html) if runs_html else "<tr><td colspan='6' style='opacity:.8'>暂无记录</td></tr>"}
    </tbody>
  </table>
</div>
"""
    return _html_page(title="兜底定时", body=body)


async def fallback_config_post(request: web.Request) -> web.Response:
    _require_auth(request)
    form = await request.post()

    enabled = str(form.get("enabled") or "0").strip() == "1"
    daily_time = str(form.get("daily_time") or "23:00").strip()
    miss_tolerance_raw = str(form.get("miss_tolerance_seconds") or "300").strip()
    header_text = str(form.get("header_text") or "")
    footer_text = str(form.get("footer_text") or "")
    try:
        miss_tolerance = int(miss_tolerance_raw)
    except Exception:
        miss_tolerance = 300
    miss_tolerance = max(0, miss_tolerance)

    payload = {"time": daily_time}
    now = time.time()
    try:
        next_run_at = compute_fallback_next_run_at(now=now, schedule_type="daily_at", payload=payload)
    except Exception as e:
        return _html_page(title="保存失败", body=f"<div class='card'><h2 style='margin-top:0'>保存失败</h2><p>{html.escape(str(e))}</p></div>")

    await update_fallback_config_fields(
        enabled=1 if enabled else 0,
        schedule_type="daily_at",
        schedule_payload=json.dumps(payload, ensure_ascii=False),
        header_text=header_text,
        footer_text=footer_text,
        miss_tolerance_seconds=int(miss_tolerance),
        next_run_at=float(next_run_at) if enabled else None,
    )
    raise web.HTTPFound(location=f"{ADMIN_WEB_PATH}/fallback")


async def fallback_pool_add(request: web.Request) -> web.Response:
    _require_auth(request)
    form = await request.post()

    display_name = str(form.get("display_name") or "").strip()
    platform_domain = str(form.get("platform_domain") or "").strip()
    platform_tg_username = str(form.get("platform_tg_username") or "").strip()
    enabled = str(form.get("enabled") or "1").strip() == "1"
    message_text = str(form.get("message_text") or "")

    try:
        if not message_text.strip():
            raise ValueError("预存文案不能为空")
        await fallback_add_pool_item(
            display_name=display_name,
            platform_domain=platform_domain,
            platform_tg_username=platform_tg_username,
            message_text=message_text,
            enabled=enabled,
        )
    except Exception as e:
        return _html_page(title="添加失败", body=f"<div class='card'><h2 style='margin-top:0'>添加失败</h2><p>{html.escape(str(e))}</p></div>")

    raise web.HTTPFound(location=f"{ADMIN_WEB_PATH}/fallback")


async def fallback_pool_edit_get(request: web.Request) -> web.Response:
    _require_auth(request)
    try:
        pool_id = int(str(request.match_info.get("pool_id") or "0"))
    except Exception:
        raise web.HTTPBadRequest(text="bad pool_id")
    item = await fallback_get_pool_item(pool_id)
    if not item:
        raise web.HTTPNotFound(text="pool item not found")

    body = f"""
<div class="card">
  <h2 style="margin-top:0">编辑预存消息 <code>{html.escape(str(pool_id))}</code></h2>
  <div class="row">
    <a href="{ADMIN_WEB_PATH}/fallback"><button>返回</button></a>
  </div>
</div>

<div class="card">
  <form method="post" action="{ADMIN_WEB_PATH}/fallback/pool/{pool_id}/save">
    <div class="grid">
      <div>
        <label>启用</label>
        <select name="enabled">
          <option value="1" {"selected" if bool(int(item.get("enabled") or 0)) else ""}>启用</option>
          <option value="0" {"selected" if not bool(int(item.get("enabled") or 0)) else ""}>禁用</option>
        </select>
      </div>
      <div>
        <label>平台名称（可选）</label>
        <input type="text" name="display_name" value="{html.escape(str(item.get("display_name") or ""))}" />
      </div>
      <div>
        <label>平台官网域名（优先）</label>
        <input type="text" name="platform_domain" value="{html.escape(str(item.get("platform_domain") or ""))}" />
      </div>
      <div>
        <label>TG 频道用户名（备用）</label>
        <input type="text" name="platform_tg_username" value="{html.escape(str(item.get("platform_tg_username") or ""))}" />
      </div>
    </div>
    <div style="height:12px"></div>
    <label>预存文案</label>
    <textarea name="message_text">{html.escape(str(item.get("message_text") or ""))}</textarea>
    <div style="height:12px"></div>
    <button type="submit">保存</button>
  </form>
</div>

<div class="card">
  <h3 style="margin-top:0;color:rgb(220,38,38)">删除（危险操作）</h3>
  <p style="opacity:.8;margin-top:0">
    删除后将从消息池永久移除该条预存消息（不可恢复）。历史运行记录仍会保留 <code>picked_pool_id</code> 引用。
    评分实体与评分数据不会自动删除（避免误删历史评分）。
  </p>
  <form method="post" action="{ADMIN_WEB_PATH}/fallback/pool/{pool_id}/delete">
    <label>请输入 <code>DELETE</code> 以确认删除</label>
    <input type="text" name="confirm" placeholder="DELETE" />
    <div style="height:10px"></div>
    <button type="submit" class="danger">删除该条</button>
  </form>
</div>
"""
    return _html_page(title="编辑兜底消息", body=body)


async def fallback_pool_edit_post(request: web.Request) -> web.Response:
    _require_auth(request)
    try:
        pool_id = int(str(request.match_info.get("pool_id") or "0"))
    except Exception:
        raise web.HTTPBadRequest(text="bad pool_id")
    form = await request.post()

    display_name = str(form.get("display_name") or "").strip()
    platform_domain = str(form.get("platform_domain") or "").strip()
    platform_tg_username = str(form.get("platform_tg_username") or "").strip()
    enabled = str(form.get("enabled") or "1").strip() == "1"
    message_text = str(form.get("message_text") or "")

    try:
        if not message_text.strip():
            raise ValueError("预存文案不能为空")
        await fallback_update_pool_item(
            pool_id=int(pool_id),
            display_name=display_name,
            platform_domain=platform_domain,
            platform_tg_username=platform_tg_username,
            message_text=message_text,
            enabled=enabled,
        )
    except Exception as e:
        return _html_page(title="保存失败", body=f"<div class='card'><h2 style='margin-top:0'>保存失败</h2><p>{html.escape(str(e))}</p></div>")

    raise web.HTTPFound(location=f"{ADMIN_WEB_PATH}/fallback")


async def fallback_pool_toggle(request: web.Request) -> web.Response:
    _require_auth(request)
    try:
        pool_id = int(str(request.match_info.get("pool_id") or "0"))
    except Exception:
        raise web.HTTPBadRequest(text="bad pool_id")
    form = await request.post()
    enabled = str(form.get("enabled") or "0").strip() == "1"
    await fallback_set_pool_enabled(pool_id=int(pool_id), enabled=enabled)
    raise web.HTTPFound(location=f"{ADMIN_WEB_PATH}/fallback")


async def fallback_pool_delete_post(request: web.Request) -> web.Response:
    _require_auth(request)
    try:
        pool_id = int(str(request.match_info.get("pool_id") or "0"))
    except Exception:
        raise web.HTTPBadRequest(text="bad pool_id")
    form = await request.post()
    confirm = str(form.get("confirm") or "").strip()
    if confirm != "DELETE":
        return _html_page(
            title="删除失败",
            body="<div class='card'><h2 style='margin-top:0'>删除失败</h2><p>请输入 DELETE 以确认删除。</p></div>",
        )
    ok = await fallback_delete_pool_item(pool_id=int(pool_id))
    if not ok:
        return _html_page(
            title="删除失败",
            body="<div class='card'><h2 style='margin-top:0'>删除失败</h2><p>记录不存在或已删除。</p></div>",
        )
    raise web.HTTPFound(location=f"{ADMIN_WEB_PATH}/fallback")


async def slots_get(request: web.Request) -> web.Response:
    _require_auth(request)
    slot_defaults = await get_slot_defaults()
    active = await get_active_orders()
    reserved = await get_reserved_orders()
    pending = await get_pending_orders()
    allow_style = runtime_settings.slot_ad_allow_style()
    allow_custom_emoji = runtime_settings.slot_ad_allow_custom_emoji() and runtime_settings.slot_ad_custom_emoji_mode() != "off"

    rows_html = []
    for slot_id in sorted(slot_defaults.keys()):
        d = slot_defaults[slot_id]
        a = active.get(slot_id)
        r = reserved.get(slot_id)
        p = pending.get(slot_id)
        sell_enabled = bool(d.get("sell_enabled"))
        default_buttons = d.get("default_buttons") or []
        lines = []
        for btn in default_buttons:
            if not isinstance(btn, dict):
                continue
            text = str(btn.get("text") or "").strip()
            url = str(btn.get("url") or "").strip()
            if not text or not url:
                continue
            parts = [text, url]
            style = str(btn.get("style") or "").strip()
            icon_custom_emoji_id = str(btn.get("icon_custom_emoji_id") or "").strip()
            if style:
                parts.append(style)
            if icon_custom_emoji_id:
                parts.append(icon_custom_emoji_id)
            lines.append(" | ".join(parts))
        default_buttons_lines = "\n".join(lines)
        visual_rows = []
        for idx in range(1, 9):
            item = default_buttons[idx - 1] if idx - 1 < len(default_buttons) and isinstance(default_buttons[idx - 1], dict) else {}
            row_text = html.escape(str(item.get("text") or ""))
            row_url = html.escape(str(item.get("url") or ""))
            row_style = str(item.get("style") or "").strip()
            row_icon = html.escape(str(item.get("icon_custom_emoji_id") or ""))
            style_options = "".join(
                f"<option value=\"{s}\" {'selected' if row_style == s else ''}>{s}</option>"
                for s in ("primary", "success", "danger")
            )
            visual_rows.append(
                f"""
                <tr>
                  <td style="white-space:nowrap;width:56px">#{idx}</td>
                  <td><input type="text" name="default_btn_text_{idx}" value="{row_text}" placeholder="按钮文案" /></td>
                  <td><input type="text" name="default_btn_url_{idx}" value="{row_url}" placeholder="https://example.com" /></td>
                  <td>
                    <select name="default_btn_style_{idx}">
                      <option value="">无</option>
                      {style_options}
                    </select>
                  </td>
                  <td><input type="text" name="default_btn_icon_{idx}" value="{row_icon}" placeholder="可选" /></td>
                </tr>
                """
            )
        visual_rows_html = "".join(visual_rows)

        active_html = "-"
        terminate_form = ""
        edit_form = ""
        if a or r or p:
            target = a or r or p
            start_at = _format_epoch(target.get("start_at"))
            end_at = _format_epoch(target.get("end_at"))
            buyer = html.escape(str(target.get("buyer_user_id")))
            out_trade_no = html.escape(str(target.get("out_trade_no") or "-"))
            button_text = html.escape(str(target.get("button_text") or ""))
            button_url = html.escape(str(target.get("button_url") or ""))
            button_style = html.escape(str(target.get("button_style") or ""))
            icon_custom_emoji_id = html.escape(str(target.get("icon_custom_emoji_id") or ""))
            advanced_lines = ""
            if button_style:
                advanced_lines += f"style: {button_style}<br/>"
            if icon_custom_emoji_id:
                advanced_lines += f"emoji_id: {icon_custom_emoji_id}<br/>"
            if a:
                active_html = (
                    "<div>"
                    "<span class='pill'>展示中</span> "
                    f"到期：{html.escape(end_at)}<br/>"
                    f"buyer: {buyer}<br/>"
                    f"order: {out_trade_no}<br/>"
                    f"button: {button_text}<br/>"
                    f"{advanced_lines}"
                    f"url: <a href=\"{button_url}\" target=\"_blank\" rel=\"noreferrer\">{button_url}</a>"
                    "</div>"
                )
            elif r:
                active_html = (
                    "<div>"
                    "<span class='pill'>已支付待生效</span> "
                    f"生效：{html.escape(start_at)}<br/>"
                    f"到期：{html.escape(end_at)}<br/>"
                    f"buyer: {buyer}<br/>"
                    f"order: {out_trade_no}<br/>"
                    f"button: {button_text}<br/>"
                    f"{advanced_lines}"
                    f"url: <a href=\"{button_url}\" target=\"_blank\" rel=\"noreferrer\">{button_url}</a>"
                    "</div>"
                )
            else:
                expires_at = _format_epoch(target.get("expires_at"))
                trade_id = html.escape(str(target.get("upay_trade_id") or "-"))
                active_html = (
                    "<div>"
                    "<span class='pill'>待支付/待确认</span> "
                    f"生效：{html.escape(start_at)}<br/>"
                    f"到期：{html.escape(end_at)}<br/>"
                    f"订单过期：{html.escape(expires_at)}<br/>"
                    f"buyer: {buyer}<br/>"
                    f"order: {out_trade_no}<br/>"
                    f"trade_id: {trade_id}<br/>"
                    f"button: {button_text}<br/>"
                    f"{advanced_lines}"
                    f"url: <a href=\"{button_url}\" target=\"_blank\" rel=\"noreferrer\">{button_url}</a>"
                    "</div>"
                )
            if a or r:
                style_options = "".join(
                    f"<option value=\"{s}\" {'selected' if button_style == s else ''}>{s}</option>"
                    for s in ("primary", "success", "danger")
                )
                terminate_form = f"""
                  <form method="post" action="{ADMIN_WEB_PATH}/slots/terminate" style="display:inline">
                    <input type="hidden" name="slot_id" value="{slot_id}" />
                    <input type="text" name="reason" placeholder="终止原因（可选）" style="width:240px" />
                    <button class="danger" type="submit">终止</button>
                  </form>
                """
                edit_form = f"""
                  <div style="height:10px"></div>
                  <form method="post" action="{ADMIN_WEB_PATH}/slots/order/edit">
                    <input type="hidden" name="out_trade_no" value="{out_trade_no}" />
                    <label>修改按钮广告内容（立即生效，且尝试刷新最近一次定时消息）</label>
                    <div class="row">
                      <div style="min-width:220px;flex:1">
                        <label>按钮文案</label>
                        <input type="text" name="button_text" value="{button_text}" />
                      </div>
                      <div style="min-width:320px;flex:2">
                        <label>按钮链接（https://）</label>
                        <input type="text" name="button_url" value="{button_url}" />
                      </div>
                    </div>
                    <div style="height:6px"></div>
                    <div class="row">
                      <div style="min-width:220px;flex:1">
                        <label>按钮样式 style（可选）</label>
                        {"<select name='button_style'><option value=''>无</option>" + style_options + "</select>" if allow_style else "<input type='hidden' name='button_style' value='' /><input type='text' value='已禁用（在广告参数页开启）' disabled />"}
                      </div>
                      <div style="min-width:320px;flex:2">
                        <label>会员表情 ID（可选）</label>
                        {"<input type='text' name='icon_custom_emoji_id' value='" + icon_custom_emoji_id + "' placeholder='例如 5390937358942362430' />" if allow_custom_emoji else "<input type='hidden' name='icon_custom_emoji_id' value='' /><input type='text' value='已禁用（在广告参数页开启）' disabled />"}
                      </div>
                    </div>
                    <div style="height:6px"></div>
                    <div class="row">
                      <label style="display:flex;gap:8px;align-items:center;margin:0">
                        <input type="checkbox" name="force" value="1" />
                        强制（忽略“每单每天修改次数限制”）
                      </label>
                      <input type="text" name="note" placeholder="备注（可选）" style="width:260px" />
                      <button type="submit">保存修改</button>
                    </div>
                  </form>
                """

        rows_html.append(f"""
          <tr>
            <td><b>{slot_id}</b></td>
            <td>{'✅' if sell_enabled else '❌'}</td>
            <td style="min-width:280px">
              <form method="post" action="{ADMIN_WEB_PATH}/slots/save">
                <input type="hidden" name="slot_id" value="{slot_id}" />
                <label>默认按钮（可视化编辑，推荐）</label>
                <p style="margin:6px 0 8px;opacity:.8">
                  每行代表 1 个按钮，留空会忽略该行。样式仅支持 <code>primary/success/danger</code>，会员表情填写 <code>icon_custom_emoji_id</code>（数字字符串）。
                </p>
                <table>
                  <thead>
                    <tr>
                      <th>行</th>
                      <th>文案</th>
                      <th>链接</th>
                      <th>样式</th>
                      <th>会员表情ID</th>
                    </tr>
                  </thead>
                  <tbody>
                    {visual_rows_html}
                  </tbody>
                </table>
                <div style="height:10px"></div>
                <label>文本模式（兼容旧格式）</label>
                <p style="margin:6px 0 8px;opacity:.8">
                  语法：每行 <code>文案 | https://链接 [| style] [| emoji_id]</code>。<br/>
                  示例：<code>客服 | https://b.com | success | 5390937358942362430</code>。<br/>
                  若“可视化编辑”有填写内容，保存时优先使用可视化数据。
                </p>
                <textarea name="default_buttons" rows="4" style="width:100%">{html.escape(default_buttons_lines)}</textarea>
                <div style="height:6px"></div>
                <label>售卖开关</label>
                <select name="sell_enabled">
                  <option value="1" {'selected' if sell_enabled else ''}>开启</option>
                  <option value="0" {'selected' if not sell_enabled else ''}>关闭</option>
                </select>
                <div style="height:8px"></div>
                <button type="submit">保存</button>
                <button type="submit" name="clear" value="1">清空默认</button>
              </form>
            </td>
            <td>{active_html}<div style="height:8px"></div>{terminate_form}{edit_form}</td>
          </tr>
        """)

    active_rows_count = int(runtime_settings.slot_ad_active_rows_count())
    body = f"""
<div class="card">
  <h2 style="margin-top:0">广告位（slot_1..slot_{len(slot_defaults)}）</h2>
  <p style="opacity:.75;margin:0">当前启用行数：前 {active_rows_count} 行；保存后立即生效；若终止生效广告，会尝试立刻更新“最近一次定时消息”的按钮。</p>
</div>

<div class="card">
  <table>
    <thead>
      <tr>
        <th>slot</th>
        <th>可售卖</th>
        <th>默认按钮/设置</th>
        <th>当前广告</th>
      </tr>
    </thead>
    <tbody>
      {''.join(rows_html)}
    </tbody>
  </table>
</div>
"""
    return _html_page(title="广告位", body=body)


async def slots_save(request: web.Request) -> web.Response:
    _require_auth(request)
    form = await request.post()
    try:
        slot_id = int(str(form.get("slot_id") or "0"))
    except Exception:
        raise web.HTTPBadRequest(text="bad slot_id")

    clear = str(form.get("clear") or "").strip() == "1"
    sell_enabled = str(form.get("sell_enabled") or "1").strip() == "1"

    raw_buttons = "" if clear else str(form.get("default_buttons") or "")
    try:
        if clear:
            default_buttons = []
        else:
            visual_buttons = []
            visual_has_input = False
            for idx in range(1, 9):
                t = str(form.get(f"default_btn_text_{idx}") or "").strip()
                u = str(form.get(f"default_btn_url_{idx}") or "").strip()
                s = str(form.get(f"default_btn_style_{idx}") or "").strip()
                icon = str(form.get(f"default_btn_icon_{idx}") or "").strip()
                if not (t or u or s or icon):
                    continue
                visual_has_input = True
                if not t or not u:
                    raise ValueError(f"可视化编辑第 {idx} 行需要同时填写“文案”和“链接”，或整行留空")
                item = {
                    "text": validate_button_text(t),
                    "url": validate_button_url(u),
                }
                style = validate_button_style(s)
                icon_custom_emoji_id = validate_icon_custom_emoji_id(icon)
                if style:
                    item["style"] = style
                if icon_custom_emoji_id:
                    item["icon_custom_emoji_id"] = icon_custom_emoji_id
                visual_buttons.append(item)

            if visual_has_input:
                default_buttons = visual_buttons
            else:
                default_buttons = parse_default_buttons_lines(raw_buttons)
    except Exception as e:
        return _html_page(
            title="保存失败",
            body=f"<div class='card'><h2 style='margin-top:0'>保存失败</h2><p>{html.escape(str(e))}</p></div>",
        )

    await set_slot_default_buttons(slot_id, default_buttons)
    await set_slot_sell_enabled(slot_id, sell_enabled)
    raise web.HTTPFound(location=f"{ADMIN_WEB_PATH}/slots")

async def slots_order_edit(request: web.Request) -> web.Response:
    _require_auth(request)
    form = await request.post()
    out_trade_no = str(form.get("out_trade_no") or "").strip()
    button_text = str(form.get("button_text") or "").strip()
    button_url = str(form.get("button_url") or "").strip()
    button_style = str(form.get("button_style") or "").strip()
    icon_custom_emoji_id = str(form.get("icon_custom_emoji_id") or "").strip()
    force = str(form.get("force") or "").strip() == "1"
    note = str(form.get("note") or "").strip()

    if not out_trade_no:
        raise web.HTTPBadRequest(text="missing out_trade_no")

    try:
        await update_slot_ad_order_creative_by_admin(
            out_trade_no=out_trade_no,
            button_text=button_text,
            button_url=button_url,
            button_style=(button_style or None),
            icon_custom_emoji_id=(icon_custom_emoji_id or None),
            force=bool(force),
            note=(note or "admin_web_edit"),
        )
    except Exception as e:
        return _html_page(
            title="保存失败",
            body=f"<div class='card'><h2 style='margin-top:0'>保存失败</h2><p>{html.escape(str(e))}</p></div>",
        )

    tg_app = _get_tg_app(request)
    try:
        await refresh_last_scheduled_message_keyboard(bot=tg_app.bot)
    except Exception as e:
        logger.warning(f"Web 修改素材后更新键盘失败（可忽略，后续定时消息会生效）: {e}", exc_info=True)

    raise web.HTTPFound(location=f"{ADMIN_WEB_PATH}/slots")


async def slots_terminate(request: web.Request) -> web.Response:
    _require_auth(request)
    form = await request.post()
    try:
        slot_id = int(str(form.get("slot_id") or "0"))
    except Exception:
        raise web.HTTPBadRequest(text="bad slot_id")
    reason = str(form.get("reason") or "违规内容").strip() or "违规内容"

    tg_app = _get_tg_app(request)
    ok = await terminate_active_order(slot_id=slot_id, reason=reason)
    if not ok:
        # 兼容：若未能终止（无 active）直接返回
        raise web.HTTPFound(location=f"{ADMIN_WEB_PATH}/slots")

    # 立刻更新最近一次定时消息的键盘（不改正文）
    try:
        await refresh_last_scheduled_message_keyboard(bot=tg_app.bot)
    except Exception as e:
        logger.warning(f"Web 终止后更新键盘失败（可忽略，后续定时消息会生效）: {e}", exc_info=True)

    raise web.HTTPFound(location=f"{ADMIN_WEB_PATH}/slots")


def build_admin_routes() -> List[Tuple[str, str, Any]]:
    """
    返回可直接传给 WebhookServer(extra_routes) 的路由列表。
    """
    base = ADMIN_WEB_PATH.rstrip("/")
    return [
        ("GET", f"{base}", index),
        ("GET", f"{base}/login", login_get),
        ("POST", f"{base}/login", login_post),
        ("GET", f"{base}/logout", logout),
        ("GET", f"{base}/submit", submit_get),
        ("POST", f"{base}/submit", submit_post),
        ("GET", f"{base}/whitelist", whitelist_users_get),
        ("POST", f"{base}/whitelist", whitelist_users_post),
        ("GET", f"{base}/whitelist/profiles", whitelist_profiles_get),
        ("POST", f"{base}/whitelist/profiles", whitelist_profiles_post),
        ("GET", f"{base}/schedule", schedule_get),
        ("POST", f"{base}/schedule", schedule_post),
        ("GET", f"{base}/fallback", fallback_get),
        ("POST", f"{base}/fallback/config", fallback_config_post),
        ("POST", f"{base}/fallback/pool/add", fallback_pool_add),
        ("GET", f"{base}/fallback/pool/{{pool_id}}", fallback_pool_edit_get),
        ("POST", f"{base}/fallback/pool/{{pool_id}}/save", fallback_pool_edit_post),
        ("POST", f"{base}/fallback/pool/{{pool_id}}/toggle", fallback_pool_toggle),
        ("POST", f"{base}/fallback/pool/{{pool_id}}/delete", fallback_pool_delete_post),
        ("GET", f"{base}/slots", slots_get),
        ("POST", f"{base}/slots/save", slots_save),
        ("POST", f"{base}/slots/order/edit", slots_order_edit),
        ("POST", f"{base}/slots/terminate", slots_terminate),
        ("GET", f"{base}/ads", ads_get),
        ("POST", f"{base}/ads", ads_post),
        ("GET", f"{base}/ai", ai_get),
        ("POST", f"{base}/ai", ai_post),
        ("GET", f"{base}/duplicate", duplicate_get),
        ("POST", f"{base}/duplicate", duplicate_post),
    ]
