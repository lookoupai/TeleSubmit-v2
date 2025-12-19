"""
付费广告（/ad）与购买回调处理
"""
import io
import html
import logging
import time
from datetime import datetime
from typing import Optional

from telegram import CopyTextButton, InputFile, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import CallbackContext, ConversationHandler

from utils import runtime_settings
from handlers.mode_selection import submit
from utils.blacklist import is_blacklisted
from utils.qr_code import make_qr_png_bytes
from utils.paid_ad_service import (
    confirm_paid_by_trade_id,
    create_order_for_package,
    get_balance,
    get_packages,
)

logger = logging.getLogger(__name__)


def _as_html_code(value: object) -> str:
    return f"<code>{html.escape(str(value))}</code>"


def _get_selected_pay_type(context: CallbackContext) -> str:
    selected = str((context.user_data or {}).get("paid_ad_pay_type") or "").strip()
    if selected and selected in (runtime_settings.upay_allowed_types() or []):
        return selected
    return runtime_settings.upay_default_type()


def _build_types_keyboard(*, current_type: str) -> InlineKeyboardMarkup:
    types = runtime_settings.upay_allowed_types() or []
    if not types:
        return InlineKeyboardMarkup([[InlineKeyboardButton("暂无可选币种", callback_data="paid_ad_buy_menu")]])

    rows = []
    for t in types:
        label = f"✅ {t}" if t == current_type else str(t)
        rows.append([InlineKeyboardButton(label, callback_data=f"paid_ad_set_type_{t}")])
    rows.append([InlineKeyboardButton("🔙 返回套餐", callback_data="paid_ad_buy_menu")])
    return InlineKeyboardMarkup(rows)

async def ad(update: Update, context: CallbackContext) -> int:
    """
    /ad：进入广告发布流程（跳过 AI/人工审核，但仍保留黑名单等前置校验）
    """
    if not runtime_settings.paid_ad_enabled():
        await update.message.reply_text("❌ 付费广告功能未开启")
        return ConversationHandler.END

    user_id = update.effective_user.id
    if is_blacklisted(user_id):
        await update.message.reply_text("⚠️ 您已被列入黑名单，无法使用广告发布功能。如有疑问，请联系管理员。")
        return ConversationHandler.END

    balance = await get_balance(user_id)
    if balance < 1:
        await update.message.reply_text(
            "📢 广告发布次数不足，请先购买。\n\n"
            "点击下方按钮选择套餐：",
            reply_markup=_build_packages_keyboard(current_type=_get_selected_pay_type(context)),
        )
        return ConversationHandler.END

    context.user_data["paid_ad"] = True
    await update.message.reply_text(
        f"📢 进入广告发布模式：发布成功将扣减 1 次（当前余额 {balance} 次）。\n"
        "该模式会跳过 AI/人工审核。",
    )
    return await submit(update, context)


async def ad_balance(update: Update, context: CallbackContext) -> None:
    if not runtime_settings.paid_ad_enabled():
        await update.message.reply_text("❌ 付费广告功能未开启")
        return
    user_id = update.effective_user.id
    balance = await get_balance(user_id)
    await update.message.reply_text(f"📢 当前广告发布余额：{balance} 次")


def _build_packages_keyboard(*, current_type: str) -> InlineKeyboardMarkup:
    """
    购买套餐键盘（带当前币种展示）。
    """
    packages = get_packages()
    if not packages:
        return InlineKeyboardMarkup([[InlineKeyboardButton("暂无可用套餐", callback_data="paid_ad_noop")]])

    rows = []
    for p in packages:
        rows.append([InlineKeyboardButton(
            f"购买 {p.credits} 次 - {p.amount} {runtime_settings.paid_ad_currency()}",
            callback_data=f"paid_ad_buy_{p.sku_id}",
        )])

    if runtime_settings.upay_allowed_types():
        rows.append([InlineKeyboardButton(f"币种：{current_type}", callback_data="paid_ad_types")])
    return InlineKeyboardMarkup(rows)


async def handle_paid_ad_callback(update: Update, context: CallbackContext) -> Optional[int]:
    """
    paid_ad_* 回调统一入口（由 handlers/callback_handlers.py 分发）
    """
    query = update.callback_query
    data = query.data
    user_id = update.effective_user.id

    if not runtime_settings.paid_ad_enabled():
        await query.edit_message_text("❌ 付费广告功能未开启")
        return ConversationHandler.END

    if data == "paid_ad_balance":
        balance = await get_balance(user_id)
        await query.edit_message_text(
            f"📢 当前广告发布余额：{balance} 次",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("购买广告次数", callback_data="paid_ad_buy_menu")]]),
        )
        return None

    if data == "paid_ad_buy_menu":
        current_type = _get_selected_pay_type(context)
        await query.edit_message_text("请选择套餐：", reply_markup=_build_packages_keyboard(current_type=current_type))
        return None

    if data == "paid_ad_types":
        current_type = _get_selected_pay_type(context)
        await query.edit_message_text("请选择收款币种/网络：", reply_markup=_build_types_keyboard(current_type=current_type))
        return None

    if data.startswith("paid_ad_set_type_"):
        t = data.replace("paid_ad_set_type_", "", 1)
        if t not in (runtime_settings.upay_allowed_types() or []):
            await query.answer("❌ 无效币种", show_alert=True)
            return None
        context.user_data["paid_ad_pay_type"] = t
        await query.answer(f"✅ 已切换为 {t}", show_alert=False)
        await query.edit_message_text("请选择套餐：", reply_markup=_build_packages_keyboard(current_type=t))
        return None

    if data == "paid_ad_howto":
        await query.answer("请发送 /ad 进入广告发布流程（发布成功扣减 1 次）。", show_alert=True)
        return None

    if data.startswith("paid_ad_buy_"):
        sku_id = data.replace("paid_ad_buy_", "", 1)
        try:
            order = await create_order_for_package(
                user_id=user_id,
                sku_id=sku_id,
                pay_type=_get_selected_pay_type(context),
            )
        except Exception as e:
            logger.error(f"创建广告购买订单失败: {e}", exc_info=True)
            await query.edit_message_text(f"❌ 创建订单失败：{e}")
            return None

        out_trade_no = order["out_trade_no"]
        payment_url = order.get("payment_url")
        pkg = order["package"]
        trade_id = order.get("trade_id")
        pay_type = order.get("pay_type")
        pay_amount = order.get("pay_amount")
        pay_address = order.get("pay_address")
        expires_at = order.get("expires_at")

        rows = []
        if payment_url:
            rows.append([InlineKeyboardButton("打开支付页", url=str(payment_url))])
        if pay_address:
            rows.append([InlineKeyboardButton("复制收款地址", copy_text=CopyTextButton(str(pay_address)))])
        if pay_amount is not None:
            rows.append([InlineKeyboardButton("复制应付金额", copy_text=CopyTextButton(str(pay_amount)))])
        rows.append([InlineKeyboardButton("我已支付（查单确认）", callback_data=f"paid_ad_check_{out_trade_no}")])
        rows.append([InlineKeyboardButton("查看余额", callback_data="paid_ad_balance")])

        pay_amount_line = None
        if pay_amount is not None:
            pay_amount_line = f"应付金额：{_as_html_code(pay_amount)}（请严格按此金额支付）"
        pay_address_line = None
        if pay_address:
            pay_address_line = f"收款地址：{_as_html_code(pay_address)}"

        await query.edit_message_text(
            "🧾 订单已创建\n\n"
            f"订单号：{out_trade_no}\n"
            f"套餐：{pkg.credits} 次 - {pkg.amount} {runtime_settings.paid_ad_currency()}\n\n"
            + (f"{pay_amount_line}\n" if pay_amount_line else "")
            + (f"{pay_address_line}\n\n" if pay_address_line else "\n")
            + "完成支付后，可点击“我已支付”进行确认入账（回调延迟/丢失时可用）。\n"
            "可使用下方按钮一键复制收款地址/应付金额。",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup(rows),
        )

        # 额外发送“收款信息 + 二维码”，让用户无需打开网页也能完成支付（保留打开支付页按钮兜底）
        chat_id = update.effective_chat.id if update.effective_chat else None
        if chat_id and pay_address and pay_amount:
            expires_text = ""
            if isinstance(expires_at, (int, float)) and expires_at > 0:
                try:
                    expires_text = datetime.fromtimestamp(float(expires_at)).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    expires_text = ""

            remaining_minutes_text = None
            if isinstance(expires_at, (int, float)) and expires_at > 0:
                remaining_seconds = float(expires_at) - time.time()
                if remaining_seconds > 0:
                    remaining_minutes_text = f"有效期：约 {int(remaining_seconds // 60)} 分钟"

            caption_lines = [
                "💳 收款信息",
                f"订单号：{out_trade_no}",
                f"网关单号：{trade_id}" if trade_id else None,
                f"币种/网络：{pay_type}" if pay_type else None,
                f"应付金额：{_as_html_code(pay_amount)}（请严格按此金额支付）",
                f"收款地址：{_as_html_code(pay_address)}",
                f"有效期至：{expires_text}" if expires_text else remaining_minutes_text,
                "建议使用下方按钮一键复制地址/金额；如无法扫码，请点击“打开支付页”。",
            ]
            caption = "\n".join([x for x in caption_lines if x])

            try:
                qr_png = make_qr_png_bytes(pay_address)
                f = io.BytesIO(qr_png)
                f.name = "payment_qr.png"
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=InputFile(f),
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(rows),
                )
            except Exception as e:
                logger.warning(f"发送收款二维码失败，将降级为纯文字提示: {e}", exc_info=True)
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(rows),
                    disable_web_page_preview=True,
                )
        return None

    if data.startswith("paid_ad_check_"):
        out_trade_no = data.replace("paid_ad_check_", "", 1)
        try:
            ok = await confirm_paid_by_trade_id(out_trade_no)
        except Exception as e:
            logger.error(f"查单确认失败: {e}", exc_info=True)
            await query.edit_message_text(f"❌ 查单失败：{e}")
            return None

        if ok:
            balance = await get_balance(user_id)
            await query.edit_message_text(f"✅ 支付确认成功，已入账。\n\n当前余额：{balance} 次")
        else:
            await query.edit_message_text("⏳ 暂未确认到支付成功（可能仍在链上确认或未完成支付）。\n\n请稍后再试。")
        return None

    if data == "paid_ad_noop":
        await query.answer("暂无可用套餐", show_alert=True)
        return None

    await query.edit_message_text("❌ 未知操作")
    return None
