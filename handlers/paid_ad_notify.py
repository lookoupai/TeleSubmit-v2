"""
UPAY_PRO 支付回调（aiohttp）
"""
import logging

from aiohttp import web

from config.settings import PAID_AD_ENABLED, UPAY_SECRET_KEY
from utils.paid_ad_service import handle_upay_notify
from utils.upay_pro_client import verify_signature

logger = logging.getLogger(__name__)


async def upay_notify(request: web.Request) -> web.Response:
    if not PAID_AD_ENABLED:
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

    ok, msg = await handle_upay_notify(payload)
    if ok:
        return web.Response(status=200, text="ok")
    return web.Response(status=400, text=msg or "error")

