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

from config.settings import ADMIN_WEB_PATH, ADMIN_WEB_TITLE, ADMIN_WEB_TOKENS
from utils.scheduled_publish_service import compute_next_run_at, get_config as get_sched_config, update_config_fields
from utils.slot_ad_service import (
    build_channel_keyboard,
    get_active_orders,
    get_pending_orders,
    get_reserved_orders,
    get_slot_defaults,
    set_slot_default,
    set_slot_sell_enabled,
    terminate_active_order,
)

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
      <a href="{ADMIN_WEB_PATH}/schedule">定时发布</a>
      <a href="{ADMIN_WEB_PATH}/slots">广告位</a>
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
    <a href="{base}/schedule"><button>管理定时发布</button></a>
    <a href="{base}/slots"><button>管理广告位</button></a>
  </div>
  <p style="opacity:.75;margin-bottom:0">本后台仅管理已落库的热更新项；修改 <code>config.ini</code> 类配置仍需要重启生效。</p>
</div>
"""
    return _html_page(title="首页", body=body)


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
    <textarea name="message_text">{html.escape(cfg.message_text or "")}</textarea>
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


async def slots_get(request: web.Request) -> web.Response:
    _require_auth(request)
    slot_defaults = await get_slot_defaults()
    active = await get_active_orders()
    reserved = await get_reserved_orders()
    pending = await get_pending_orders()

    rows_html = []
    for slot_id in sorted(slot_defaults.keys()):
        d = slot_defaults[slot_id]
        a = active.get(slot_id)
        r = reserved.get(slot_id)
        p = pending.get(slot_id)
        sell_enabled = bool(d.get("sell_enabled"))
        default_text = d.get("default_text") or ""
        default_url = d.get("default_url") or ""

        active_html = "-"
        terminate_form = ""
        if a or r or p:
            target = a or r or p
            start_at = _format_epoch(target.get("start_at"))
            end_at = _format_epoch(target.get("end_at"))
            buyer = html.escape(str(target.get("buyer_user_id")))
            out_trade_no = html.escape(str(target.get("out_trade_no") or "-"))
            button_text = html.escape(str(target.get("button_text") or ""))
            button_url = html.escape(str(target.get("button_url") or ""))
            if a:
                active_html = (
                    "<div>"
                    "<span class='pill'>展示中</span> "
                    f"到期：{html.escape(end_at)}<br/>"
                    f"buyer: {buyer}<br/>"
                    f"order: {out_trade_no}<br/>"
                    f"button: {button_text}<br/>"
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
                    f"url: <a href=\"{button_url}\" target=\"_blank\" rel=\"noreferrer\">{button_url}</a>"
                    "</div>"
                )
            if a or r:
                terminate_form = f"""
                  <form method="post" action="{ADMIN_WEB_PATH}/slots/terminate" style="display:inline">
                    <input type="hidden" name="slot_id" value="{slot_id}" />
                    <input type="text" name="reason" placeholder="终止原因（可选）" style="width:240px" />
                    <button class="danger" type="submit">终止</button>
                  </form>
                """

        rows_html.append(f"""
          <tr>
            <td><b>{slot_id}</b></td>
            <td>{'✅' if sell_enabled else '❌'}</td>
            <td style="min-width:280px">
              <form method="post" action="{ADMIN_WEB_PATH}/slots/save">
                <input type="hidden" name="slot_id" value="{slot_id}" />
                <label>默认按钮文案</label>
                <input type="text" name="default_text" value="{html.escape(default_text)}" />
                <div style="height:6px"></div>
                <label>默认按钮链接（https）</label>
                <input type="text" name="default_url" value="{html.escape(default_url)}" />
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
            <td>{active_html}<div style="height:8px"></div>{terminate_form}</td>
          </tr>
        """)

    body = f"""
<div class="card">
  <h2 style="margin-top:0">广告位（slot_1..slot_10）</h2>
  <p style="opacity:.75;margin:0">保存后立即生效；若终止生效广告，会尝试立刻更新“最近一次定时消息”的按钮。</p>
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
    default_text = (str(form.get("default_text") or "").strip() or None) if not clear else None
    default_url = (str(form.get("default_url") or "").strip() or None) if not clear else None
    sell_enabled = str(form.get("sell_enabled") or "1").strip() == "1"

    await set_slot_default(slot_id, default_text, default_url)
    await set_slot_sell_enabled(slot_id, sell_enabled)
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
        sched = await get_sched_config()
        if sched.last_message_chat_id and sched.last_message_id:
            slot_defaults = await get_slot_defaults()
            active = await get_active_orders()
            keyboard = build_channel_keyboard(slot_defaults=slot_defaults, active_orders=active)
            await tg_app.bot.edit_message_reply_markup(
                chat_id=int(sched.last_message_chat_id),
                message_id=int(sched.last_message_id),
                reply_markup=keyboard,
            )
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
        ("GET", f"{base}/schedule", schedule_get),
        ("POST", f"{base}/schedule", schedule_post),
        ("GET", f"{base}/slots", slots_get),
        ("POST", f"{base}/slots/save", slots_save),
        ("POST", f"{base}/slots/terminate", slots_terminate),
    ]
