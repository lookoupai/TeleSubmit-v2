"""
UPAY_PRO 支付回调（aiohttp）
"""
import asyncio
import logging

from aiohttp import web

from config.settings import PAID_AD_ENABLED, SLOT_AD_ENABLED, UPAY_SECRET_KEY
from utils.paid_ad_service import handle_upay_notify
from utils.upay_pro_client import verify_signature
from utils.slot_ad_service import get_slot_order_for_user_notice, process_upay_notify_for_slot_ads

logger = logging.getLogger(__name__)


def _get_tg_app(request: web.Request):
    return request.app.get("tg_application")


async def _notify_slot_ad_paid_if_possible(request: web.Request, out_trade_no: str) -> None:
    tg_app = _get_tg_app(request)
    if not tg_app:
        return

    order = await get_slot_order_for_user_notice(out_trade_no)
    if not order:
        return
    if order.get("status") not in ("active", "paid"):
        return

    buyer_user_id = order.get("buyer_user_id")
    if buyer_user_id is None:
        return

    try:
        slot_id = int(order.get("slot_id") or 0) or "-"
    except Exception:
        slot_id = "-"

    start_at = order.get("start_at")
    end_at = order.get("end_at")

    def _fmt(ts) -> str:
        if not isinstance(ts, (int, float)) or float(ts) <= 0:
            return "-"
        try:
            from utils.slot_ad_service import format_epoch
            return format_epoch(float(ts))
        except Exception:
            return str(ts)

    text = (
        "✅ 已收到支付成功（按钮广告位）\n\n"
        f"订单号：{order.get('out_trade_no')}\n"
        f"广告位：{slot_id}\n"
        f"按钮文案：{order.get('button_text')}\n"
        f"按钮链接：{order.get('button_url')}\n"
        f"预计生效时间：{_fmt(start_at)}（服务器时间）\n"
        f"预计到期时间：{_fmt(end_at)}（服务器时间）\n\n"
        "说明：按钮广告会在下一次频道定时消息发送时生效，无需再次操作。"
    )

    try:
        await tg_app.bot.send_message(
            chat_id=int(buyer_user_id),
            text=text,
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.warning(f"发送 Slot Ads 支付成功通知失败（可忽略）: {e}", exc_info=True)


async def upay_notify(request: web.Request) -> web.Response:
    if not (PAID_AD_ENABLED or SLOT_AD_ENABLED):
        return web.Response(status=404, text="not enabled")

    try:
        payload = await request.json()
    except Exception:
        return web.Response(status=400, text="bad json")

    if not isinstance(payload, dict):
        return web.Response(status=400, text="bad payload")

    if not UPAY_SECRET_KEY:
        logger.error("UPAY_SECRET_KEY 未配置，拒绝处理回调")
        return web.Response(status=500, text="config error")

    if not verify_signature(payload, UPAY_SECRET_KEY):
        logger.warning("UPAY_PRO 回调验签失败")
        return web.Response(status=401, text="signature error")

    order_id = str(payload.get("order_id") or "").strip()
    if order_id.startswith("SLT"):
        ok, msg, activated, out_trade_no = await process_upay_notify_for_slot_ads(payload)
        if ok and activated and out_trade_no:
            asyncio.create_task(_notify_slot_ad_paid_if_possible(request, str(out_trade_no)))
    else:
        ok, msg = await handle_upay_notify(payload)
    if ok:
        return web.Response(status=200, text="ok")
    return web.Response(status=400, text=msg or "error")
