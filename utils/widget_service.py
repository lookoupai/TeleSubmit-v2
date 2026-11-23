"""Private payment cards backed by order snapshots and persistent refresh jobs."""

import asyncio
import json
import logging
import time
import zlib
from pathlib import Path

from telegram import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, TelegramError

from database.db_manager import get_db
from utils import paid_ad_service, slot_ad_service
from utils.content_normalizer import web_url
from utils.telegram_api import TelegramAPIError
from utils.telegram_capabilities import feature_enabled

logger = logging.getLogger(__name__)
ORDER_HTML = Path(__file__).resolve().parents[1] / "ui" / "widgets" / "order_v1.html"
ORDER_KINDS = {
    "ad_widget": ("ad_orders", "user_id", "order_widgets"),
    "slot_widget": ("slot_ad_orders", "buyer_user_id", "slot_order_widgets"),
}
STATUS_LABELS = {
    "waiting": "等待支付",
    "checking": "正在核对支付",
    "unconfirmed": "暂未确认到账",
    "paid": "已到账",
    "window_ended": "支付时间已到，请先查询状态",
    "unavailable": "查询暂不可用",
    "closed": "订单已结束",
}


def encode_data(data):
    value = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if len(value.encode("utf-8")) > 1024:
        raise ValueError("Widget state exceeds the byte limit")
    return value


def widget_html():
    html = ORDER_HTML.read_text(encoding="utf-8")
    data = html.encode("utf-8")
    if not 1 <= len(data) <= 32768 or len(zlib.compress(data, level=9)) > 4096:
        raise ValueError("Widget HTML exceeds the byte limit")
    return html


async def load_order(conn, kind, order_id):
    table, owner, _ = ORDER_KINDS[kind]
    return await (
        await conn.execute(
            f"SELECT *, {owner} AS widget_owner FROM {table} WHERE out_trade_no=?",
            (order_id,),
        )
    ).fetchone()


def payment_state(order, *, revision, checked=None, outcome=None):
    expires = int(order["expires_at"] or 0)
    state = "waiting"
    if order["paid_at"] is not None or order["status"] in {"active", "paid"}:
        state = "paid"
    elif order["status"] != "created":
        state = "closed"
    elif expires and expires <= time.time():
        state = "window_ended"
    if state == "waiting" and outcome:
        state = outcome
    product = (
        f"广告发布 {order['credits']} 次"
        if "credits" in order.keys()
        else f"广告位 {order['slot_id']} · {order['plan_days']} 天"
    )
    return {
        "r": revision,
        "state": state,
        "ref": order["out_trade_no"],
        "amount": order["pay_amount"] or "",
        "currency": order["currency"] or "",
        "network": order["pay_type"] or "",
        "product": product,
        "exp": expires,
        "checked": int(checked or 0),
    }


def widget_caption(order, state):
    fields = [state["product"], f"订单号 {state['ref']}", STATUS_LABELS[state["state"]]]
    if state["state"] == "paid" and "credits" in order.keys():
        fields[-1] += f"，已增加 {order['credits']} 次"
    if state["network"]:
        fields.append(f"收款网络 {state['network']}")
    if state["amount"]:
        fields.append(f"应付金额 {state['amount']} {state['currency']}")
    return "\n".join(fields)


def widget_markup(order, state):
    order_id = state["ref"]
    rows = []
    if state["state"] not in {"paid", "closed"}:
        if not state["exp"] or state["exp"] > time.time():
            url = web_url(order["payment_url"])
            address = order["pay_address"]
            if url:
                rows.append([InlineKeyboardButton("打开支付页", url=url)])
            if address and len(address.encode("utf-16-le")) <= 512:
                rows.append(
                    [
                        InlineKeyboardButton(
                            "复制收款地址", copy_text=CopyTextButton(address)
                        )
                    ]
                )
        rows.append(
            [
                InlineKeyboardButton(
                    "查询支付状态", callback_data=f"widget_check_{order_id}"
                )
            ]
        )
    if "credits" in order.keys():
        rows.append([InlineKeyboardButton("查看余额", callback_data="widget_balance")])
    elif order["status"] in {"active", "paid"}:
        rows.append(
            [
                InlineKeyboardButton(
                    "修改广告内容", callback_data=f"slot_edit_{order_id}"
                )
            ]
        )
    return InlineKeyboardMarkup(rows)


async def send_order_widget(context, kind, order_id, *, chat):
    """Return True when the widget was sent or its delivery is unknown."""
    _, _, switch = ORDER_KINDS[kind]
    api = getattr(context, "bot_data", {}).get("telegram_api")
    if (
        not api
        or not chat
        or chat.type != "private"
        or not feature_enabled(switch, chat.id)
    ):
        return False
    async with get_db() as conn:
        order = await load_order(conn, kind, order_id)
    if not order or order["widget_owner"] != chat.id or not order["pay_amount"]:
        return False
    try:
        state = payment_state(order, revision=0)
        payload = {
            "chat_id": chat.id,
            "html": widget_html(),
            "chat_data": encode_data(state),
            "allow_queries": True,
            "is_shared": False,
            "protect_content": True,
            "caption": widget_caption(order, state),
            "reply_markup": widget_markup(order, state).to_dict(),
        }
        if len(payload["caption"].encode("utf-16-le")) > 2048:
            return False
    except (ValueError, OSError):
        logger.warning("订单 Widget 超出展示限制或模板不可读取")
        return False
    try:
        result = await api.call("sendWidget", payload)
    except TelegramAPIError as exc:
        logger.info("订单 Widget 发送结果: %s", exc.category)
        return exc.category == "unknown_delivery"
    message_id = result.get("message_id") if isinstance(result, dict) else None
    if type(message_id) is not int or message_id <= 0:
        logger.warning("订单 Widget 缺少发送回执")
        return True
    try:
        async with get_db() as conn:
            await conn.execute(
                """INSERT INTO bot_message_views(entity_kind,entity_id,owner_user_id,chat_id,message_id,renderer)
                   VALUES(?,?,?,?,?,'widget') ON CONFLICT DO NOTHING""",
                (kind, order_id, chat.id, str(chat.id), message_id),
            )
            # Payment can commit while sendWidget is in flight.
            await conn.execute(
                """INSERT INTO ui_refresh_jobs(job_kind,entity_id,next_run)
                   VALUES(?,?,?) ON CONFLICT(job_kind,entity_id) DO UPDATE SET
                   next_run=excluded.next_run, revision=ui_refresh_jobs.revision+1""",
                (kind, order_id, time.time()),
            )
    except Exception as exc:
        logger.warning("订单 Widget 回执保存失败: %s", type(exc).__name__)
    return True


async def queue_order_check(user_id, chat_id, message_id, order_id):
    if user_id != chat_id or not 0 < user_id < 2**63 or not 0 < message_id < 2**63:
        return "订单不存在或无权访问"
    async with get_db() as conn:
        await conn.execute("PRAGMA busy_timeout=700")
        await conn.execute("BEGIN IMMEDIATE")
        view = await (
            await conn.execute(
                """SELECT entity_kind FROM bot_message_views
               WHERE owner_user_id=? AND chat_id=? AND message_id=? AND entity_id=?
               AND invalid=0 AND renderer='widget'""",
                (user_id, str(chat_id), message_id, order_id),
            )
        ).fetchone()
        if not view or view["entity_kind"] not in ORDER_KINDS:
            return "订单不存在或无权访问"
        kind = view["entity_kind"]
        order = await load_order(conn, kind, order_id)
        if not order or order["widget_owner"] != user_id:
            return "订单不存在或无权访问"
        if order["paid_at"] is not None or order["status"] in {"paid", "active"}:
            return "已确认到账"
        if order["status"] != "created":
            return "订单已结束"
        now = time.time()
        active = await (
            await conn.execute(
                """SELECT j.needs_check, j.last_check_at, j.entity_id FROM ui_refresh_jobs j
               JOIN bot_message_views v ON v.entity_id=j.entity_id AND v.entity_kind=j.job_kind
               WHERE v.owner_user_id=? AND v.invalid=0 AND
               ((j.entity_id=? AND j.needs_check=1) OR j.last_check_at>?) LIMIT 1""",
                (user_id, order_id, now - 2),
            )
        ).fetchone()
        if active:
            return (
                "正在核对支付"
                if active["needs_check"] and active["entity_id"] == order_id
                else "请稍后再查"
            )
        await conn.execute(
            """INSERT INTO ui_refresh_jobs(job_kind,entity_id,next_run,needs_check,last_check_at)
               VALUES(?,?,?,1,?) ON CONFLICT(job_kind,entity_id) DO UPDATE SET
               next_run=excluded.next_run, needs_check=1, last_check_at=excluded.last_check_at,
               attempts=0, revision=ui_refresh_jobs.revision+1""",
            (kind, order_id, now, now),
        )
    return "正在核对支付"


async def process_order_job(context, job):
    kind, order_id, revision = job["job_kind"], job["entity_id"], job["revision"]
    api = context.bot_data["telegram_api"]
    async with get_db() as conn:
        order = await load_order(conn, kind, order_id)
        views = await (
            await conn.execute(
                """SELECT * FROM bot_message_views
               WHERE entity_kind=? AND entity_id=? AND renderer='widget' AND invalid=0""",
                (kind, order_id),
            )
        ).fetchall()
    outcome = job["outcome"]
    if order and job["needs_check"] and order["status"] == "created":
        lookup = (
            paid_ad_service.confirm_paid_by_trade_id
            if kind == "ad_widget"
            else slot_ad_service.confirm_paid_by_trade_id
        )
        try:
            paid = await asyncio.wait_for(lookup(order_id), timeout=10)
            outcome = None if paid else "unconfirmed"
        except Exception as exc:
            outcome = "unavailable"
            logger.warning("Widget 查单失败: %s", type(exc).__name__)
        async with get_db() as conn:
            await conn.execute(
                """UPDATE ui_refresh_jobs SET needs_check=0, outcome=?, revision=revision+1
                   WHERE job_kind=? AND entity_id=? AND revision=?""",
                (outcome, kind, order_id, revision),
            )
        return

    retry_after = None
    if order:
        state = payment_state(
            order, revision=revision, checked=job["last_check_at"], outcome=outcome
        )
        for view in views:
            try:
                await refresh_widget(context, api, view, order, state)
            except (TelegramAPIError, TelegramError) as exc:
                category = getattr(exc, "category", "")
                missing = (
                    isinstance(exc, Forbidden)
                    or (
                        isinstance(exc, BadRequest)
                        and "message to edit not found" in str(exc).lower()
                    )
                    or category in {"missing_message", "forbidden"}
                )
                if missing:
                    async with get_db() as conn:
                        await conn.execute(
                            "UPDATE bot_message_views SET invalid=1 WHERE id=?",
                            (view["id"],),
                        )
                elif category not in {"invalid_payload", "unsupported"}:
                    delay = getattr(exc, "retry_after", None)
                    if hasattr(delay, "total_seconds"):
                        delay = delay.total_seconds()
                    retry_after = max(
                        retry_after or 0,
                        float(delay or min(300, 2 ** min(job["attempts"] + 1, 8))),
                    )
                else:
                    logger.warning("订单 Widget 刷新被拒绝: %s", category)
    next_run = None
    if retry_after:
        next_run = time.time() + retry_after
    elif (
        order
        and views
        and order["status"] == "created"
        and (order["expires_at"] or 0) > time.time()
    ):
        next_run = order["expires_at"]
    async with get_db() as conn:
        await conn.execute(
            """UPDATE ui_refresh_jobs SET next_run=?, attempts=?, needs_check=0, revision=revision+?
               WHERE job_kind=? AND entity_id=? AND revision=?""",
            (
                next_run,
                job["attempts"] + 1 if retry_after else 0,
                int(next_run is not None and not retry_after),
                kind,
                order_id,
                revision,
            ),
        )


async def refresh_widget(context, api, view, order, state):
    target = {"chat_id": view["chat_id"], "message_id": view["message_id"]}
    try:
        await api.call(
            "editMessageWidgetData", {**target, "chat_data": encode_data(state)}
        )
    except TelegramAPIError as exc:
        if exc.category != "unchanged":
            raise
    try:
        await context.bot.edit_message_caption(
            **target,
            caption=widget_caption(order, state),
            reply_markup=widget_markup(order, state),
        )
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


async def widget_refresh_tick(context):
    if not context.bot_data.get("telegram_api"):
        return
    async with get_db() as conn:
        jobs = await (
            await conn.execute(
                "SELECT * FROM ui_refresh_jobs WHERE next_run<=? ORDER BY next_run LIMIT 3",
                (time.time(),),
            )
        ).fetchall()
    results = await asyncio.gather(
        *(process_order_job(context, job) for job in jobs),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, Exception):
            logger.warning("订单 Widget 后台任务未完成: %s", type(result).__name__)
