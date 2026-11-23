"""
按钮广告位（Slot Ads）与定时发布管理处理器
"""

from __future__ import annotations

import io
import html
import logging
import time
from typing import Optional

import json

from telegram import CopyTextButton, InputFile, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import CallbackContext, ApplicationHandlerStop

from config.settings import (
    ADMIN_IDS,
)
from utils import runtime_settings
from utils.ad_risk_reviewer import review_ad_risk
from utils.qr_code import make_qr_png_bytes
from utils.scheduled_publish_service import (
    compute_next_run_at,
    get_config as get_sched_config,
    get_next_run_at_for_ads,
    update_config_fields,
)
from utils.slot_ad_service import (
    build_channel_keyboard,
    confirm_paid_by_trade_id,
    create_creative,
    create_slot_ad_payment_order,
    disable_expiry_reminder,
    enable_expiry_reminder,
    ensure_can_purchase_or_renew,
    format_epoch,
    format_slot_blocked_message,
    get_slot_order_for_edit,
    get_slot_order_for_user_notice,
    get_active_orders,
    get_plans,
    get_slot_defaults,
    is_admin,
    refresh_last_scheduled_message_keyboard,
    set_slot_default,
    terminate_active_order,
    update_slot_ad_order_creative_by_user,
    user_can_edit_order_today,
    validate_button_style,
    validate_button_text,
    validate_button_url,
    validate_icon_custom_emoji_id,
)

logger = logging.getLogger(__name__)


FLOW_KEY = "slot_ad_flow"

async def _start_order_edit_flow(
    *,
    update: Update,
    context: CallbackContext,
    out_trade_no: str,
    via_query=None,
) -> None:
    """
    开始“编辑订单素材”流程（私聊）。
    """
    out_trade_no = str(out_trade_no or "").strip()
    user_id = update.effective_user.id if update.effective_user else None
    if not out_trade_no or user_id is None:
        if via_query:
            await via_query.answer("❌ 参数无效", show_alert=True)
        return
    if not runtime_settings.slot_ad_enabled():
        if via_query:
            await via_query.answer("❌ 按钮广告位功能未开启", show_alert=True)
        else:
            await update.message.reply_text("❌ 按钮广告位功能未开启")
        return

    order = await get_slot_order_for_edit(out_trade_no)
    if not order:
        msg = "❌ 未找到订单"
        if via_query:
            await via_query.answer(msg, show_alert=True)
        else:
            await update.message.reply_text(msg)
        return

    if int(order.get("buyer_user_id") or 0) != int(user_id):
        msg = "❌ 无权限（仅支持修改自己的订单）"
        if via_query:
            await via_query.answer(msg, show_alert=True)
        else:
            await update.message.reply_text(msg)
        return

    quota = await user_can_edit_order_today(out_trade_no=str(out_trade_no), user_id=int(user_id))
    if not quota.get("ok"):
        limit = quota.get("limit")
        msg = f"⚠️ 今日已达到修改次数上限（{limit} 次/单/天）" if limit else "⚠️ 今日已达到修改次数上限"
        if via_query:
            await via_query.answer(msg, show_alert=True)
        else:
            await update.message.reply_text(msg)
        return

    remaining = quota.get("remaining")
    remaining_text = f"{int(remaining)}" if isinstance(remaining, int) else "不限"
    limit = quota.get("limit")
    limit_text = "不限" if int(limit or 0) <= 0 else str(int(limit))

    context.user_data[FLOW_KEY] = {
        "stage": "edit_text",
        "mode": "edit",
        "out_trade_no": str(out_trade_no),
    }

    current_text = str(order.get("button_text") or "").strip()
    current_url = str(order.get("button_url") or "").strip()
    current_style = str(order.get("button_style") or "").strip()
    current_icon = str(order.get("icon_custom_emoji_id") or "").strip()
    tip = (
        "🛠️ 修改按钮广告内容\n\n"
        f"订单号：{_as_html_code(out_trade_no)}\n"
        f"当前按钮文案：{_as_html_code(current_text)}\n"
        f"当前按钮链接：{_as_html_code(current_url)}\n\n"
        + (f"当前样式：{_as_html_code(current_style)}\n" if current_style else "")
        + (f"当前会员表情ID：{_as_html_code(current_icon)}\n\n" if current_icon else "")
        + f"今日剩余次数：{_as_html_code(remaining_text)} / {_as_html_code(limit_text)}\n\n"
        + "请发送新的按钮文案："
    )

    if via_query and getattr(via_query, "message", None):
        await via_query.message.reply_text(tip, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    elif update.message:
        await update.message.reply_text(tip, parse_mode=ParseMode.HTML, disable_web_page_preview=True)



def _get_args_text(update: Update) -> str:
    if not update.message or not update.message.text:
        return ""
    parts = update.message.text.split(maxsplit=1)
    return parts[1] if len(parts) > 1 else ""


def _require_admin(update: Update) -> bool:
    user_id = update.effective_user.id if update.effective_user else None
    if user_id is None:
        return False
    if int(user_id) not in set(ADMIN_IDS or []):
        return False
    return True


def _as_html_code(value: object) -> str:
    return f"<code>{html.escape(str(value))}</code>"


def _slot_adv_style_enabled() -> bool:
    return bool(runtime_settings.slot_ad_allow_style())


def _slot_adv_icon_enabled() -> bool:
    if not runtime_settings.slot_ad_allow_custom_emoji():
        return False
    return runtime_settings.slot_ad_custom_emoji_mode() != "off"


def _slot_user_can_set_advanced() -> bool:
    if not runtime_settings.slot_ad_user_can_set_advanced():
        return False
    return _slot_adv_style_enabled() or _slot_adv_icon_enabled()


def _next_advanced_stage(prefix: str) -> Optional[str]:
    if _slot_adv_style_enabled():
        return f"{prefix}_style"
    if _slot_adv_icon_enabled():
        return f"{prefix}_icon"
    return None


def _advanced_style_prompt() -> str:
    return "可选：请发送按钮样式（primary/success/danger）。\n如不设置，请发送“无”。"


def _advanced_icon_prompt() -> str:
    return "可选：请发送会员表情 ID（数字字符串）。\n如不设置，请发送“无”。"


def _parse_optional_style_input(raw: str) -> Optional[str]:
    s = str(raw or "").strip()
    if s.lower() in ("", "无", "none", "off", "skip", "默认", "不设置"):
        return None
    return validate_button_style(s)


def _parse_optional_icon_input(raw: str) -> Optional[str]:
    s = str(raw or "").strip()
    if s.lower() in ("", "无", "none", "off", "skip", "默认", "不设置"):
        return None
    return validate_icon_custom_emoji_id(s)


def _build_slot_plan_keyboard(*, slot_id: int, current_type: str) -> InlineKeyboardMarkup:
    plans = get_plans()
    rows = []
    for p in plans:
        rows.append([InlineKeyboardButton(f"{p.days} 天 - {p.amount}", callback_data=f"slot_plan_{slot_id}_{p.days}")])
    if runtime_settings.upay_allowed_types():
        rows.append([InlineKeyboardButton(f"币种：{current_type}", callback_data=f"slot_types_{slot_id}")])
    rows.append([InlineKeyboardButton("取消", callback_data="slot_cancel")])
    return InlineKeyboardMarkup(rows)


def _build_slot_types_keyboard(*, slot_id: int, current_type: str) -> InlineKeyboardMarkup:
    types = runtime_settings.upay_allowed_types() or []
    if not types:
        return InlineKeyboardMarkup([[InlineKeyboardButton("暂无可选币种", callback_data=f"slot_back_plans_{slot_id}")]])
    rows = []
    for t in types:
        label = f"✅ {t}" if t == current_type else str(t)
        rows.append([InlineKeyboardButton(label, callback_data=f"slot_set_type_{slot_id}_{t}")])
    rows.append([InlineKeyboardButton("🔙 返回租期", callback_data=f"slot_back_plans_{slot_id}")])
    return InlineKeyboardMarkup(rows)

def _with_remind_toggle_button(markup: InlineKeyboardMarkup, *, enabled: bool, out_trade_no: str) -> InlineKeyboardMarkup:
    """
    将支付消息的“到期提醒”按钮替换为开/关状态。
    只改按钮，不依赖外部状态，保证幂等。
    """
    rows = [list(r) for r in (markup.inline_keyboard or [])]
    if not rows:
        return markup

    on = InlineKeyboardButton("开启到期前1天提醒（可选）", callback_data=f"slot_remind_on_{out_trade_no}")
    off = InlineKeyboardButton("✅ 已开启到期提醒（点我关闭）", callback_data=f"slot_remind_off_{out_trade_no}")
    target = off if enabled else on

    replaced = False
    for i, row in enumerate(rows):
        if not row:
            continue
        b = row[0]
        cd = getattr(b, "callback_data", None)
        if isinstance(cd, str) and (cd.startswith("slot_remind_on_") or cd.startswith("slot_remind_off_")):
            rows[i] = [target]
            replaced = True
            break

    if not replaced:
        rows.append([target])
    return InlineKeyboardMarkup(rows)

def _without_check_button(markup: InlineKeyboardMarkup, *, out_trade_no: str) -> InlineKeyboardMarkup:
    """
    移除“查单确认”按钮（支付已确认后不再需要）。
    只改按钮，不依赖外部状态，保证幂等。
    """
    rows = [list(r) for r in (markup.inline_keyboard or [])]
    if not rows:
        return markup

    target_cd = f"slot_ad_check_{out_trade_no}"
    new_rows = []
    for row in rows:
        if not row:
            continue
        kept = []
        for b in row:
            cd = getattr(b, "callback_data", None)
            if isinstance(cd, str) and cd == target_cd:
                continue
            kept.append(b)
        if kept:
            new_rows.append(kept)
    return InlineKeyboardMarkup(new_rows)

async def _send_payment_qr_if_possible(
    *,
    context: CallbackContext,
    chat_id: int,
    caption: str,
    pay_address: Optional[str],
    reply_markup: InlineKeyboardMarkup,
) -> None:
    """
    额外发送“收款信息 + 二维码”，失败则降级为纯文字提示。
    """
    if not pay_address:
        return
    try:
        qr_png = make_qr_png_bytes(str(pay_address))
        f = io.BytesIO(qr_png)
        f.name = "payment_qr.png"
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=InputFile(f),
            caption=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )
    except Exception as e:
        logger.warning(f"发送收款二维码失败，将降级为纯文字提示: {e}", exc_info=True)
        await context.bot.send_message(
            chat_id=chat_id,
            text=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )


async def try_handle_start_args(update: Update, context: CallbackContext) -> bool:
    """
    /start 深链入口：/start buy_slot_{id}

    返回 True 表示已消费该 /start，不应继续走默认欢迎消息。
    """
    if not update.message:
        return False
    args = getattr(context, "args", None) or []
    if not args:
        return False

    token = str(args[0] or "").strip()
    if not token.startswith("buy_slot_"):
        return False

    try:
        slot_id = int(token.replace("buy_slot_", "", 1))
    except Exception:
        await update.message.reply_text("❌ 无效的广告位参数")
        return True

    if not runtime_settings.slot_ad_enabled():
        await update.message.reply_text("❌ 按钮广告位功能未开启")
        return True

    active_rows_count = int(runtime_settings.slot_ad_active_rows_count())
    if slot_id <= 0 or slot_id > active_rows_count:
        await update.message.reply_text(f"❌ 该广告位（{int(slot_id)}）当前未启用（启用范围：1..{active_rows_count}）")
        return True

    user_id = update.effective_user.id
    gate = await ensure_can_purchase_or_renew(slot_id=slot_id, user_id=user_id)
    if gate.get("mode") == "blocked":
        await update.message.reply_text(format_slot_blocked_message(slot_id=slot_id, available_at=float(gate["available_at"])))
        return True

    mode = str(gate.get("mode") or "buy")
    renew_start_at = float(gate.get("renew_start_at")) if gate.get("renew_start_at") is not None else None
    context.user_data[FLOW_KEY] = {
        "slot_id": int(slot_id),
        "mode": mode,
        "stage": "choose_plan",
        "renew_start_at": renew_start_at,
        "pay_type": runtime_settings.upay_default_type(),
    }

    if not get_plans():
        await update.message.reply_text("❌ 未配置可购买的租期套餐，请联系管理员")
        context.user_data.pop(FLOW_KEY, None)
        return True

    await update.message.reply_text(
        f"📌 购买广告位：{slot_id}\n\n请选择租期：",
        reply_markup=_build_slot_plan_keyboard(slot_id=slot_id, current_type=runtime_settings.upay_default_type()),
    )
    return True


async def slot_edit_cmd(update: Update, context: CallbackContext) -> None:
    """
    /slot_edit <out_trade_no>
    允许用户在订单有效期内自助修改按钮文案与链接（每日限额）。
    """
    if not update.message:
        return
    out_trade_no = _get_args_text(update).strip()
    if not out_trade_no:
        await update.message.reply_text("用法：/slot_edit <订单号>\n\n提示：订单号形如 SLTxxxxxxxxxxxx。")
        return
    await _start_order_edit_flow(update=update, context=context, out_trade_no=str(out_trade_no), via_query=None)


async def handle_slot_callback(update: Update, context: CallbackContext) -> None:
    """
    slot_* 回调入口（由 handlers/callback_handlers.py 分发）
    """
    query = update.callback_query
    data = str(query.data or "")
    user_id = update.effective_user.id

    flow = context.user_data.get(FLOW_KEY) or {}

    if data == "slot_cancel":
        context.user_data.pop(FLOW_KEY, None)
        await query.edit_message_text("已取消")
        return

    if data.startswith("slot_edit_"):
        out_trade_no = data.replace("slot_edit_", "", 1)
        await _start_order_edit_flow(update=update, context=context, out_trade_no=str(out_trade_no), via_query=query)
        return

    if data.startswith("slot_buy_"):
        # 频道中 BOT_USERNAME 未配置时的降级入口：提示用户私聊 /start buy_slot_x
        slot_id = data.replace("slot_buy_", "", 1)
        await query.answer("请私聊机器人完成购买（发送 /start），并确保已与机器人开启对话。", show_alert=True)
        return

    if data.startswith("slot_back_plans_"):
        try:
            slot_id = int(data.replace("slot_back_plans_", "", 1))
        except Exception:
            await query.answer("❌ 无效操作", show_alert=True)
            return
        flow = context.user_data.get(FLOW_KEY) or {}
        current_type = str(flow.get("pay_type") or runtime_settings.upay_default_type())
        await query.edit_message_text(
            f"📌 购买广告位：{slot_id}\n\n请选择租期：",
            reply_markup=_build_slot_plan_keyboard(slot_id=slot_id, current_type=current_type),
        )
        return

    if data.startswith("slot_types_"):
        try:
            slot_id = int(data.replace("slot_types_", "", 1))
        except Exception:
            await query.answer("❌ 无效操作", show_alert=True)
            return
        flow = context.user_data.get(FLOW_KEY) or {}
        current_type = str(flow.get("pay_type") or runtime_settings.upay_default_type())
        await query.edit_message_text(
            "请选择收款币种/网络：",
            reply_markup=_build_slot_types_keyboard(slot_id=slot_id, current_type=current_type),
        )
        return

    if data.startswith("slot_set_type_"):
        rest = data.replace("slot_set_type_", "", 1)
        if "_" not in rest:
            await query.answer("❌ 无效操作", show_alert=True)
            return
        slot_id_str, t = rest.split("_", 1)
        try:
            slot_id = int(slot_id_str)
        except Exception:
            await query.answer("❌ 无效操作", show_alert=True)
            return
        if t not in (runtime_settings.upay_allowed_types() or []):
            await query.answer("❌ 无效币种", show_alert=True)
            return
        flow = context.user_data.get(FLOW_KEY)
        if not isinstance(flow, dict) or int(flow.get("slot_id", 0)) != int(slot_id):
            await query.answer("⚠️ 会话已过期，请重新从购买入口开始。", show_alert=True)
            return
        flow["pay_type"] = t
        context.user_data[FLOW_KEY] = flow
        await query.answer(f"✅ 已切换为 {t}", show_alert=False)
        await query.edit_message_text(
            f"📌 购买广告位：{slot_id}\n\n请选择租期：",
            reply_markup=_build_slot_plan_keyboard(slot_id=slot_id, current_type=t),
        )
        return

    if data.startswith("slot_plan_"):
        try:
            _, _, slot_id_str, days_str = data.split("_", 3)
            slot_id = int(slot_id_str)
            plan_days = int(days_str)
        except Exception:
            await query.answer("❌ 无效操作", show_alert=True)
            return

        if flow.get("stage") != "choose_plan" or int(flow.get("slot_id", 0)) != int(slot_id):
            await query.answer("⚠️ 会话已过期，请重新从购买入口开始。", show_alert=True)
            return

        flow["plan_days"] = int(plan_days)
        flow["stage"] = "text"
        context.user_data[FLOW_KEY] = flow

        await query.edit_message_text("请发送按钮文案（不超过指定长度，不允许换行）：")
        return

    if data.startswith("slot_ad_check_"):
        out_trade_no = data.replace("slot_ad_check_", "", 1)
        order = await get_slot_order_for_user_notice(out_trade_no)
        if not order or order.get("buyer_user_id") != user_id:
            await query.answer("订单不存在或无权访问", show_alert=True)
            return
        try:
            ok = await confirm_paid_by_trade_id(out_trade_no)
        except Exception as e:
            logger.error(f"Slot Ads 查单确认失败: {e}", exc_info=True)
            await query.answer(f"❌ 查单失败：{e}", show_alert=True)
            return
        if ok:
            await query.answer("✅ 支付确认成功，订单已激活（生效时间以规则为准）。", show_alert=True)
            if query.message and query.message.reply_markup:
                try:
                    await query.edit_message_reply_markup(
                        reply_markup=_without_check_button(query.message.reply_markup, out_trade_no=str(out_trade_no))
                    )
                except Exception:
                    pass
        else:
            await query.answer("⏳ 暂未确认到支付成功，请稍后再试。", show_alert=True)
        return

    if data.startswith("slot_remind_on_"):
        out_trade_no = data.replace("slot_remind_on_", "", 1)
        ok = await enable_expiry_reminder(
            out_trade_no=out_trade_no,
            user_id=user_id,
            advance_days=int(runtime_settings.slot_ad_reminder_advance_days()),
        )
        if ok:
            await query.answer("✅ 已开启到期提醒", show_alert=False)
        else:
            await query.answer("❌ 开启失败（可能订单不存在或无权限）", show_alert=True)
        if ok and query.message and query.message.reply_markup:
            await query.edit_message_reply_markup(
                reply_markup=_with_remind_toggle_button(query.message.reply_markup, enabled=True, out_trade_no=out_trade_no)
            )
        return

    if data.startswith("slot_remind_off_"):
        out_trade_no = data.replace("slot_remind_off_", "", 1)
        ok = await disable_expiry_reminder(out_trade_no=out_trade_no, user_id=user_id)
        if ok:
            await query.answer("✅ 已关闭到期提醒", show_alert=False)
        else:
            await query.answer("❌ 关闭失败（可能订单不存在或无权限）", show_alert=True)
        if ok and query.message and query.message.reply_markup:
            await query.edit_message_reply_markup(
                reply_markup=_with_remind_toggle_button(query.message.reply_markup, enabled=False, out_trade_no=out_trade_no)
            )
        return

    await query.answer("❌ 未知操作", show_alert=True)


async def handle_slot_text_input(update: Update, context: CallbackContext) -> None:
    """
    私聊文本输入：承接 /start buy_slot_x 之后的按钮文案与 URL 收集。
    """
    if not update.message or not update.message.text:
        return
    flow = context.user_data.get(FLOW_KEY)
    if not isinstance(flow, dict):
        return

    stage = str(flow.get("stage") or "")
    text = update.message.text.strip()
    user_id = update.effective_user.id

    async def _finish_edit(current_flow: dict) -> None:
        out_trade_no = str(current_flow.get("out_trade_no") or "").strip()
        if not out_trade_no or not current_flow.get("button_text") or not current_flow.get("button_url"):
            await update.message.reply_text("❌ 会话状态异常，请重新从“修改广告内容”入口开始。")
            context.user_data.pop(FLOW_KEY, None)
            raise ApplicationHandlerStop()

        try:
            await update_slot_ad_order_creative_by_user(
                out_trade_no=str(out_trade_no),
                user_id=int(user_id),
                button_text=str(current_flow["button_text"]),
                button_url=str(current_flow["button_url"]),
                button_style=(current_flow.get("button_style") or None),
                icon_custom_emoji_id=(current_flow.get("icon_custom_emoji_id") or None),
            )
        except Exception as e:
            await update.message.reply_text(f"❌ 修改失败：{e}")
            context.user_data.pop(FLOW_KEY, None)
            raise ApplicationHandlerStop()

        context.user_data.pop(FLOW_KEY, None)
        refreshed = False
        try:
            refreshed = await refresh_last_scheduled_message_keyboard(bot=context.bot)
        except Exception as e:
            logger.warning(f"修改素材后更新键盘失败（可忽略，后续定时消息会生效）: {e}", exc_info=True)
            refreshed = False
        await update.message.reply_text(
            "✅ 已更新按钮广告内容。\n"
            + ("✅ 已尝试刷新最近一次定时消息按钮。" if refreshed else "ℹ️ 将在下一次定时消息发送时生效。")
        )
        raise ApplicationHandlerStop()

    async def _finish_create(current_flow: dict) -> None:
        slot_id = int(current_flow["slot_id"])
        plan_days = int(current_flow.get("plan_days") or 0)
        if plan_days <= 0:
            await update.message.reply_text("❌ 租期未选择，请重新从购买入口开始。")
            context.user_data.pop(FLOW_KEY, None)
            raise ApplicationHandlerStop()

        gate = await ensure_can_purchase_or_renew(slot_id=slot_id, user_id=user_id)
        if gate.get("mode") == "blocked":
            await update.message.reply_text(format_slot_blocked_message(slot_id=slot_id, available_at=float(gate["available_at"])))
            context.user_data.pop(FLOW_KEY, None)
            raise ApplicationHandlerStop()

        if current_flow.get("mode") == "renew" and gate.get("mode") != "renew":
            await update.message.reply_text("⚠️ 当前不在续期窗口，请稍后再试。")
            context.user_data.pop(FLOW_KEY, None)
            raise ApplicationHandlerStop()

        review = await review_ad_risk(button_text=str(current_flow["button_text"]), button_url=str(current_flow["button_url"]))
        if not review.passed:
            await update.message.reply_text(f"❌ 风控拒绝：{review.category}\n原因：{review.reason}\n\n请重新从购买入口提交素材。")
            context.user_data.pop(FLOW_KEY, None)
            raise ApplicationHandlerStop()

        creative_id = await create_creative(
            user_id=user_id,
            button_text=str(current_flow["button_text"]),
            button_url=str(current_flow["button_url"]),
            button_style=(current_flow.get("button_style") or None),
            icon_custom_emoji_id=(current_flow.get("icon_custom_emoji_id") or None),
            ai_review=review.to_dict(),
        )

        now = time.time()
        planned_start_at: Optional[float] = None
        if current_flow.get("mode") == "renew":
            planned_start_at = float(current_flow.get("renew_start_at") or 0) or None
        if planned_start_at is None:
            planned_start_at = await get_next_run_at_for_ads(now=now) or now

        try:
            order = await create_slot_ad_payment_order(
                slot_id=slot_id,
                buyer_user_id=user_id,
                creative_id=creative_id,
                plan_days=plan_days,
                planned_start_at=float(planned_start_at),
                pay_type=str(current_flow.get("pay_type") or runtime_settings.upay_default_type()),
            )
        except Exception as e:
            logger.error(f"创建 Slot Ads 支付订单失败: {e}", exc_info=True)
            await update.message.reply_text(f"❌ 创建支付订单失败：{e}")
            context.user_data.pop(FLOW_KEY, None)
            raise ApplicationHandlerStop()

        out_trade_no = order["out_trade_no"]
        trade_id = order.get("trade_id")
        payment_url = order.get("payment_url")
        pay_address = order.get("pay_address")
        pay_amount = order.get("pay_amount")
        pay_type = order.get("pay_type")
        expires_at = order.get("expires_at")

        start_text = format_epoch(order.get("planned_start_at"))
        end_text = format_epoch(order.get("planned_end_at"))

        rows = []
        if payment_url:
            rows.append([InlineKeyboardButton("打开支付页", url=str(payment_url))])
        if pay_address:
            rows.append([InlineKeyboardButton("复制收款地址", copy_text=CopyTextButton(str(pay_address)))])
        if pay_amount is not None:
            rows.append([InlineKeyboardButton("复制应付金额", copy_text=CopyTextButton(str(pay_amount)))])
        rows.append([InlineKeyboardButton("我已支付（查单确认）", callback_data=f"slot_ad_check_{out_trade_no}")])
        rows.append([InlineKeyboardButton("开启到期前1天提醒（可选）", callback_data=f"slot_remind_on_{out_trade_no}")])

        pay_amount_line = f"应付金额：{_as_html_code(pay_amount)}（请严格按此金额支付）" if pay_amount is not None else None
        pay_address_line = f"收款地址：{_as_html_code(pay_address)}" if pay_address else None

        expires_line = None
        if isinstance(expires_at, (int, float)) and float(expires_at) > 0:
            try:
                expires_line = f"订单有效期至：{_as_html_code(format_epoch(float(expires_at)))}"
            except Exception:
                expires_line = None

        await update.message.reply_text(
            "🧾 广告位订单已创建\n\n"
            f"广告位：{_as_html_code(slot_id)}\n"
            f"租期：{_as_html_code(plan_days)} 天\n"
            f"预计生效：{_as_html_code(start_text)}\n"
            f"预计到期：{_as_html_code(end_text)}\n"
            f"订单号：{_as_html_code(out_trade_no)}\n"
            + (f"币种/网络：{_as_html_code(pay_type)}\n" if pay_type else "")
            + (f"{pay_amount_line}\n" if pay_amount_line else "")
            + (f"{pay_address_line}\n" if pay_address_line else "")
            + (f"{expires_line}\n" if expires_line else "")
            + "\n支付成功后系统会自动发送确认消息；如 1-3 分钟未收到，可点击“我已支付”进行查单确认（回调延迟/丢失时可用）。",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=_with_remind_toggle_button(InlineKeyboardMarkup(rows), enabled=False, out_trade_no=str(out_trade_no)),
        )

        from utils.widget_service import send_order_widget
        widget_sent = await send_order_widget(context, "slot_widget", out_trade_no, chat=update.effective_chat)
        chat_id = update.effective_chat.id if update.effective_chat else None
        if not widget_sent and chat_id and pay_address and pay_amount is not None:
            expires_text = None
            if isinstance(expires_at, (int, float)) and float(expires_at) > 0:
                try:
                    expires_text = format_epoch(float(expires_at))
                except Exception:
                    expires_text = None

            remaining_minutes_text = None
            if isinstance(expires_at, (int, float)) and float(expires_at) > 0:
                remaining_seconds = float(expires_at) - time.time()
                if remaining_seconds > 0:
                    remaining_minutes_text = f"有效期：约 {int(remaining_seconds // 60)} 分钟"

            caption_lines = [
                "💳 收款信息",
                f"订单号：{_as_html_code(out_trade_no)}",
                f"网关单号：{_as_html_code(trade_id)}" if trade_id else None,
                f"币种/网络：{_as_html_code(pay_type)}" if pay_type else None,
                f"应付金额：{_as_html_code(pay_amount)}（请严格按此金额支付）",
                f"收款地址：{_as_html_code(pay_address)}",
                f"有效期至：{_as_html_code(expires_text)}" if expires_text else remaining_minutes_text,
                f"广告位：{_as_html_code(slot_id)}（预计生效 {_as_html_code(start_text)}，到期 {_as_html_code(end_text)}）",
                "建议使用下方按钮一键复制地址/金额；如无法扫码，请点击“打开支付页”。",
            ]
            caption = "\n".join([x for x in caption_lines if x])
            reply_markup = _with_remind_toggle_button(InlineKeyboardMarkup(rows), enabled=False, out_trade_no=str(out_trade_no))
            await _send_payment_qr_if_possible(
                context=context,
                chat_id=int(chat_id),
                caption=caption,
                pay_address=str(pay_address),
                reply_markup=reply_markup,
            )

        context.user_data.pop(FLOW_KEY, None)
        raise ApplicationHandlerStop()

    if stage == "edit_text":
        try:
            flow["button_text"] = validate_button_text(text)
        except Exception as e:
            await update.message.reply_text(f"❌ {e}\n\n请重新发送按钮文案：")
            raise ApplicationHandlerStop()
        flow["stage"] = "edit_url"
        context.user_data[FLOW_KEY] = flow
        await update.message.reply_text("请发送新的按钮链接（仅允许 https://）：")
        raise ApplicationHandlerStop()

    if stage == "edit_url":
        try:
            flow["button_url"] = validate_button_url(text)
        except Exception as e:
            await update.message.reply_text(f"❌ {e}\n\n请重新发送链接：")
            raise ApplicationHandlerStop()
        if _slot_user_can_set_advanced():
            next_stage = _next_advanced_stage("edit")
            if next_stage:
                flow["stage"] = next_stage
                context.user_data[FLOW_KEY] = flow
                await update.message.reply_text(_advanced_style_prompt() if next_stage == "edit_style" else _advanced_icon_prompt())
                raise ApplicationHandlerStop()
        await _finish_edit(flow)

    if stage == "edit_style":
        try:
            flow["button_style"] = _parse_optional_style_input(text)
        except Exception as e:
            await update.message.reply_text(f"❌ {e}\n\n请重新发送样式（primary/success/danger）或“无”：")
            raise ApplicationHandlerStop()
        if _slot_adv_icon_enabled():
            flow["stage"] = "edit_icon"
            context.user_data[FLOW_KEY] = flow
            await update.message.reply_text(_advanced_icon_prompt())
            raise ApplicationHandlerStop()
        await _finish_edit(flow)

    if stage == "edit_icon":
        try:
            flow["icon_custom_emoji_id"] = _parse_optional_icon_input(text)
        except Exception as e:
            await update.message.reply_text(f"❌ {e}\n\n请重新发送会员表情 ID（数字）或“无”：")
            raise ApplicationHandlerStop()
        await _finish_edit(flow)

    if stage == "text":
        try:
            flow["button_text"] = validate_button_text(text)
        except Exception as e:
            await update.message.reply_text(f"❌ {e}\n\n请重新发送按钮文案：")
            raise ApplicationHandlerStop()
        flow["stage"] = "url"
        context.user_data[FLOW_KEY] = flow
        await update.message.reply_text("请发送按钮链接（仅允许 https://）：")
        raise ApplicationHandlerStop()

    if stage == "url":
        try:
            flow["button_url"] = validate_button_url(text)
        except Exception as e:
            await update.message.reply_text(f"❌ {e}\n\n请重新发送链接：")
            raise ApplicationHandlerStop()
        if _slot_user_can_set_advanced():
            next_stage = "style" if _slot_adv_style_enabled() else "icon"
            flow["stage"] = next_stage
            context.user_data[FLOW_KEY] = flow
            await update.message.reply_text(_advanced_style_prompt() if next_stage == "style" else _advanced_icon_prompt())
            raise ApplicationHandlerStop()
        await _finish_create(flow)

    if stage == "style":
        try:
            flow["button_style"] = _parse_optional_style_input(text)
        except Exception as e:
            await update.message.reply_text(f"❌ {e}\n\n请重新发送样式（primary/success/danger）或“无”：")
            raise ApplicationHandlerStop()
        if _slot_adv_icon_enabled():
            flow["stage"] = "icon"
            context.user_data[FLOW_KEY] = flow
            await update.message.reply_text(_advanced_icon_prompt())
            raise ApplicationHandlerStop()
        await _finish_create(flow)

    if stage == "icon":
        try:
            flow["icon_custom_emoji_id"] = _parse_optional_icon_input(text)
        except Exception as e:
            await update.message.reply_text(f"❌ {e}\n\n请重新发送会员表情 ID（数字）或“无”：")
            raise ApplicationHandlerStop()
        await _finish_create(flow)


async def sched_status(update: Update, context: CallbackContext) -> None:
    if not _require_admin(update):
        await update.message.reply_text("⚠️ 无权限")
        return
    cfg = await get_sched_config()
    await update.message.reply_text(
        "📌 定时发布状态\n\n"
        f"启用：{cfg.enabled}\n"
        f"类型：{cfg.schedule_type}\n"
        f"参数：{html.escape(str(cfg.schedule_payload))}\n"
        f"自动置顶：{getattr(cfg, 'auto_pin', False)}\n"
        f"删除上一条：{getattr(cfg, 'delete_prev', False)}\n"
        f"next_run_at：{format_epoch(cfg.next_run_at) if cfg.next_run_at else '未设置'}\n"
        f"last_run_at：{format_epoch(cfg.last_run_at) if cfg.last_run_at else '无'}\n"
        f"正文长度：{len(cfg.message_text or '')}\n"
        f"last_message_id：{cfg.last_message_id or '无'}",
    )


async def sched_on(update: Update, context: CallbackContext) -> None:
    if not _require_admin(update):
        await update.message.reply_text("⚠️ 无权限")
        return
    cfg = await get_sched_config()
    now = time.time()
    next_run_at = compute_next_run_at(now=now, schedule_type=cfg.schedule_type, payload=cfg.schedule_payload, last_run_at=cfg.last_run_at)
    await update_config_fields(enabled=1, next_run_at=float(next_run_at))
    await update.message.reply_text(f"✅ 已开启定时发布\nnext_run_at：{format_epoch(next_run_at)}（服务器时间）")


async def sched_off(update: Update, context: CallbackContext) -> None:
    if not _require_admin(update):
        await update.message.reply_text("⚠️ 无权限")
        return
    await update_config_fields(enabled=0)
    await update.message.reply_text("✅ 已关闭定时发布")


async def sched_set_text(update: Update, context: CallbackContext) -> None:
    if not _require_admin(update):
        await update.message.reply_text("⚠️ 无权限")
        return
    text = _get_args_text(update)
    await update_config_fields(message_text=str(text))
    await update.message.reply_text(f"✅ 已更新定时消息正文（长度 {len(text)}）")


async def sched_daily(update: Update, context: CallbackContext) -> None:
    if not _require_admin(update):
        await update.message.reply_text("⚠️ 无权限")
        return
    arg = _get_args_text(update).strip()
    if not arg:
        await update.message.reply_text("用法：/sched_daily HH:MM")
        return
    now = time.time()
    payload = {"time": arg}
    try:
        next_run_at = compute_next_run_at(now=now, schedule_type="daily_at", payload=payload, last_run_at=None)
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")
        return
    await update_config_fields(
        schedule_type="daily_at",
        schedule_payload=json.dumps(payload, ensure_ascii=False),
        next_run_at=float(next_run_at),
    )
    await update.message.reply_text(f"✅ 已设置 daily_at={arg}\nnext_run_at：{format_epoch(next_run_at)}（服务器时间）")


async def sched_every_hours(update: Update, context: CallbackContext) -> None:
    if not _require_admin(update):
        await update.message.reply_text("⚠️ 无权限")
        return
    arg = _get_args_text(update).strip()
    if not arg:
        await update.message.reply_text("用法：/sched_every_hours N")
        return
    try:
        hours = int(arg)
    except Exception:
        await update.message.reply_text("❌ N 必须是整数")
        return
    if hours <= 0:
        await update.message.reply_text("❌ N 必须 > 0")
        return
    now = time.time()
    payload = {"hours": hours}
    next_run_at = compute_next_run_at(now=now, schedule_type="every_n_hours", payload=payload, last_run_at=now)
    await update_config_fields(
        schedule_type="every_n_hours",
        schedule_payload=json.dumps(payload, ensure_ascii=False),
        next_run_at=float(next_run_at),
    )
    await update.message.reply_text(f"✅ 已设置 every_n_hours={hours}\nnext_run_at：{format_epoch(next_run_at)}")


def _parse_on_off_arg(value: str) -> Optional[bool]:
    s = (value or "").strip().lower()
    if s in {"1", "on", "true", "yes", "y", "开启"}:
        return True
    if s in {"0", "off", "false", "no", "n", "关闭"}:
        return False
    return None


async def sched_pin(update: Update, context: CallbackContext) -> None:
    if not _require_admin(update):
        await update.message.reply_text("⚠️ 无权限")
        return
    arg = _get_args_text(update).strip()
    enabled = _parse_on_off_arg(arg)
    if enabled is None:
        await update.message.reply_text("用法：/sched_pin 1|0（发出后是否自动置顶）")
        return
    await update_config_fields(auto_pin=1 if enabled else 0)
    await update.message.reply_text(f"✅ 已{'开启' if enabled else '关闭'}：发出后自动置顶")


async def sched_delete_prev(update: Update, context: CallbackContext) -> None:
    if not _require_admin(update):
        await update.message.reply_text("⚠️ 无权限")
        return
    arg = _get_args_text(update).strip()
    enabled = _parse_on_off_arg(arg)
    if enabled is None:
        await update.message.reply_text("用法：/sched_delete_prev 1|0（发出后是否删除上一条定时消息）")
        return
    await update_config_fields(delete_prev=1 if enabled else 0)
    await update.message.reply_text(f"✅ 已{'开启' if enabled else '关闭'}：发出后删除上一条定时消息")


async def slot_set_default_cmd(update: Update, context: CallbackContext) -> None:
    if not _require_admin(update):
        await update.message.reply_text("⚠️ 无权限")
        return
    arg = _get_args_text(update).strip()
    parts = arg.split(maxsplit=2)
    if len(parts) < 3:
        await update.message.reply_text("用法：/slot_set_default <slot_id> <text> <url>")
        return
    try:
        slot_id = int(parts[0])
    except Exception:
        await update.message.reply_text("❌ slot_id 必须是整数")
        return
    text = parts[1].strip()
    url = parts[2].strip()
    try:
        text = validate_button_text(text)
        url = validate_button_url(url)
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")
        return
    await set_slot_default(slot_id, text, url)
    await update.message.reply_text(f"✅ 已设置 slot {slot_id} 默认按钮")


async def slot_clear_default_cmd(update: Update, context: CallbackContext) -> None:
    if not _require_admin(update):
        await update.message.reply_text("⚠️ 无权限")
        return
    arg = _get_args_text(update).strip()
    if not arg:
        await update.message.reply_text("用法：/slot_clear_default <slot_id>")
        return
    try:
        slot_id = int(arg)
    except Exception:
        await update.message.reply_text("❌ slot_id 必须是整数")
        return
    await set_slot_default(slot_id, None, None)
    await update.message.reply_text(f"✅ 已清空 slot {slot_id} 默认按钮")


async def slot_terminate_cmd(update: Update, context: CallbackContext) -> None:
    if not _require_admin(update):
        await update.message.reply_text("⚠️ 无权限")
        return
    arg = _get_args_text(update).strip()
    if not arg:
        await update.message.reply_text("用法：/slot_terminate <slot_id> [reason]")
        return
    parts = arg.split(maxsplit=1)
    try:
        slot_id = int(parts[0])
    except Exception:
        await update.message.reply_text("❌ slot_id 必须是整数")
        return
    reason = parts[1] if len(parts) > 1 else "违规内容"

    ok = await terminate_active_order(slot_id=slot_id, reason=reason)
    if not ok:
        await update.message.reply_text("ℹ️ 该广告位当前没有生效广告")
        return

    # 立刻更新“最近一次定时消息”的键盘（不改正文）
    sched = await get_sched_config()
    if sched.last_message_chat_id and sched.last_message_id:
        try:
            slot_defaults = await get_slot_defaults()
            active = await get_active_orders(now=time.time())
            keyboard = build_channel_keyboard(slot_defaults=slot_defaults, active_orders=active)
            await context.bot.edit_message_reply_markup(
                chat_id=int(sched.last_message_chat_id),
                message_id=int(sched.last_message_id),
                reply_markup=keyboard,
            )
        except Exception as e:
            logger.warning(f"终止后更新键盘失败（可忽略，后续定时消息会生效）: {e}", exc_info=True)

    await update.message.reply_text(f"✅ 已终止 slot {slot_id} 的当前广告（不退款）")
