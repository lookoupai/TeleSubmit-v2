"""
审核流程处理模块
处理 AI 审核和重复检测的完整流程
"""
import json
import logging
import time
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ConversationHandler, CallbackContext

from config.settings import (
    AI_REVIEW_NOTIFY_ADMIN_ON_REJECT,
    AI_REVIEW_NOTIFY_ADMIN_ON_DUPLICATE,
    OWNER_ID,
    ADMIN_IDS,
    CHANNEL_ID,
)
from database.db_manager import get_db
from utils.ai_reviewer import get_ai_reviewer, ReviewResult
from utils.duplicate_detector import get_duplicate_detector, DuplicateResult
from utils.feature_extractor import get_feature_extractor
from utils.paid_ad_service import get_balance
from utils.submit_policy import get_effective_policy
from utils import runtime_settings

logger = logging.getLogger(__name__)


def _format_duration(seconds: float) -> str:
    """将秒数格式化为人类可读的等待时长（天/小时/分钟）。"""
    total_seconds = max(0, int(seconds))
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)

    if total_seconds > 0 and days == 0 and hours == 0 and minutes == 0:
        minutes = 1

    parts = []
    if days:
        parts.append(f"{days}天")
    if hours:
        parts.append(f"{hours}小时")
    if minutes or not parts:
        parts.append(f"{minutes}分钟")
    return "".join(parts)


def _build_duplicate_wait_hint(original_submit_time: float, *, window_days: int) -> str:
    """构建重复投稿的等待提示（上次时间/剩余等待/可再次投稿时间）。"""
    if not original_submit_time:
        return ""

    window_days = int(window_days)
    last_dt = datetime.fromtimestamp(original_submit_time)
    available_at_ts = original_submit_time + (window_days * 86400)
    available_dt = datetime.fromtimestamp(available_at_ts)
    remaining_seconds = available_at_ts - time.time()

    return (
        f"上次投稿时间：{last_dt.strftime('%Y-%m-%d %H:%M')}\n"
        f"距离可再次投稿还需：{_format_duration(remaining_seconds)}（到 {available_dt.strftime('%Y-%m-%d %H:%M')} 后）"
    )


async def perform_review(
    update: Update,
    context: CallbackContext,
    submission_data: dict,
    user_info: dict,
    *,
    skip_ai_review: bool = False,
) -> tuple:
    """
    执行完整的审核流程

    Args:
        update: Telegram 更新对象
        context: 回调上下文
        submission_data: 投稿数据
        user_info: 用户信息 {user_id, username, bio}

    Returns:
        tuple: (is_approved, should_continue, message)
            - is_approved: 是否通过审核
            - should_continue: 是否继续发布流程
            - message: 审核消息（用于通知用户）
    """
    user_id = user_info.get('user_id')
    username = user_info.get('username', '')
    user_bio = user_info.get('bio', '')
    policy = get_effective_policy(int(user_id))

    # 构建完整内容用于审核
    content = _build_content_for_review(submission_data)

    # 1. 重复检测
    if bool((policy.get("duplicate_check") or {}).get("enabled", False)):
        dup_result = await _check_duplicate(user_id, username, content, user_bio)
        if dup_result.is_duplicate:
            should_block = (
                dup_result.duplicate_type == "rate_limit"
                or bool((policy.get("duplicate_check") or {}).get("auto_reject", True))
            )
            await _handle_duplicate_result(update, context, dup_result, user_info, policy=policy, blocked=should_block)
            if should_block:
                return (False, False, dup_result.message)

    # 2. AI 审核
    ai_mode = str(((policy.get("ai_review") or {}).get("mode")) or "inherit").strip() or "inherit"
    if ai_mode not in ("inherit", "skip", "run_no_auto_reject", "manual_only"):
        ai_mode = "inherit"

    if ai_mode == "manual_only":
        await _send_to_manual_review(
            update,
            context,
            ReviewResult(
                approved=False,
                confidence=0.0,
                reason="白名单策略：仅人工审核",
                category="待人工审核",
                requires_manual=True,
            ),
            user_info,
            submission_data,
        )
        return (False, False, "您的投稿已提交，正在等待管理员审核。")

    if ai_mode == "skip":
        return (True, True, "")

    allow_auto_reject = ai_mode != "run_no_auto_reject"
    should_run_ai = runtime_settings.ai_review_enabled() and (not skip_ai_review)

    if ai_mode == "run_no_auto_reject" and not should_run_ai:
        # AI 不可用/被跳过时，为保证安全性：转人工审核
        await _send_to_manual_review(
            update,
            context,
            ReviewResult(
                approved=False,
                confidence=0.0,
                reason="白名单策略：AI 不可用，转人工审核",
                category="待人工审核",
                requires_manual=True,
            ),
            user_info,
            submission_data,
        )
        return (False, False, "您的投稿已提交，正在等待管理员审核。")

    if should_run_ai:
        review_result = await _perform_ai_review(submission_data)

        reviewer = get_ai_reviewer()

        if reviewer.should_auto_approve(review_result):
            # 自动通过
            logger.info(f"投稿自动通过: user_id={user_id}, category={review_result.category}")
            return (True, True, "✅ 投稿审核通过！")

        elif reviewer.should_auto_reject(review_result) and allow_auto_reject:
            # 自动拒绝
            await _handle_rejection(update, context, review_result, user_info, submission_data)
            return (False, False, review_result.reason)

        elif reviewer.should_auto_reject(review_result) and not allow_auto_reject:
            # 白名单策略：不允许自动拒绝，转人工审核（方案1）
            await _send_to_manual_review(update, context, review_result, user_info, submission_data)
            return (False, False, "您的投稿已提交，正在等待管理员审核。")

        else:
            # 需要人工审核
            await _send_to_manual_review(update, context, review_result, user_info, submission_data)
            return (False, False, "您的投稿已提交，正在等待管理员审核。")

    # 未启用审核（或跳过 AI 审核），直接通过
    return (True, True, "")


async def _check_duplicate(
    user_id: int,
    username: str,
    content: str,
    user_bio: str
) -> DuplicateResult:
    """执行重复检测"""
    detector = get_duplicate_detector()
    extractor = get_feature_extractor()

    # 创建指纹
    fingerprint = extractor.create_fingerprint(
        user_id=user_id,
        username=username,
        content=content,
        bio=user_bio
    )

    # 检测重复
    result = await detector.check(fingerprint)

    return result


async def _perform_ai_review(submission_data: dict) -> ReviewResult:
    """执行 AI 审核"""
    reviewer = get_ai_reviewer()
    return await reviewer.review(submission_data)


async def _handle_duplicate_result(
    update: Update,
    context: CallbackContext,
    result: DuplicateResult,
    user_info: dict,
    *,
    policy: dict,
    blocked: bool = True,
):
    """处理重复检测结果"""
    user_id = user_info.get('user_id')
    username = user_info.get('username', '')

    dup_cfg = policy.get("duplicate_check") or {}
    # 通知用户
    if blocked and bool(dup_cfg.get("notify_user", True)):
        if result.duplicate_type == 'rate_limit':
            message = (
                "⚠️ 投稿频率超限\n\n"
                f"{result.message}\n\n"
                "请稍后再试，或联系管理员。"
            )
        else:
            window_days = int(dup_cfg.get("window_days", 7))
            wait_hint = _build_duplicate_wait_hint(result.original_submit_time, window_days=window_days)
            detail = result.message if not wait_hint else f"{result.message}\n\n{wait_hint}"
            message = (
                "⚠️ 检测到重复投稿\n\n"
                f"{detail}\n\n"
                f"为保证频道内容质量，{window_days} 天内相似/相同内容不可重复投稿。\n"
                "如有疑问，请联系管理员。"
            )
        await update.message.reply_text(message)

    # 通知管理员
    if AI_REVIEW_NOTIFY_ADMIN_ON_DUPLICATE and ADMIN_IDS:
        blocked_text = "是" if blocked else "否"
        admin_message = (
            "🔔 重复投稿检测通知\n\n"
            f"用户：@{username} (ID: {user_id})\n"
            f"类型：{result.duplicate_type}\n"
            f"已拦截：{blocked_text}\n"
            f"相似度：{result.similarity_score:.0%}\n"
            f"详情：{result.message}"
        )
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(chat_id=admin_id, text=admin_message)
            except Exception as e:
                logger.error(f"通知管理员 {admin_id} 失败: {e}")

    if blocked:
        logger.info(f"重复投稿被拦截: user_id={user_id}, type={result.duplicate_type}")
    else:
        logger.info(f"重复投稿命中未拦截: user_id={user_id}, type={result.duplicate_type}")


async def _handle_rejection(
    update: Update,
    context: CallbackContext,
    result: ReviewResult,
    user_info: dict,
    submission_data: dict
):
    """处理自动拒绝"""
    user_id = user_info.get('user_id')
    username = user_info.get('username', '')

    # 通知用户
    if runtime_settings.ai_review_notify_user():
        reviewer = get_ai_reviewer()
        if runtime_settings.paid_ad_enabled() and reviewer.is_off_topic_category(result.category):
            balance = await get_balance(user_id)
            keyboard = [
                [
                    InlineKeyboardButton("购买广告次数", callback_data="paid_ad_buy_menu"),
                    InlineKeyboardButton("查看余额", callback_data="paid_ad_balance"),
                ],
                [
                    InlineKeyboardButton("广告发布 /ad", callback_data="paid_ad_howto"),
                ],
            ]
            message = (
                "❌ 投稿未通过审核：主题无关\n\n"
                f"原因：{result.reason}\n\n"
                "若需发布广告，可购买广告发布次数（可批量购买，随时使用）。\n"
                f"当前余额：{balance} 次\n\n"
                "使用 /ad 发布广告（每次发布扣 1 次）。"
            )
            await update.message.reply_text(message, reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            message = (
                "❌ 投稿未通过审核\n\n"
                f"原因：{result.reason}\n\n"
                f"本频道仅接受与「{runtime_settings.ai_review_channel_topic()}」相关的内容投稿。\n"
                "如有疑问，请联系管理员。"
            )
            await update.message.reply_text(message)

    # 通知管理员
    if AI_REVIEW_NOTIFY_ADMIN_ON_REJECT and ADMIN_IDS:
        content_preview = _get_content_preview(submission_data)
        admin_message = (
            "🔔 投稿自动拒绝通知\n\n"
            f"用户：@{username} (ID: {user_id})\n"
            f"分类：{result.category}\n"
            f"置信度：{result.confidence:.0%}\n"
            f"原因：{result.reason}\n\n"
            f"内容预览：\n{content_preview}"
        )
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(chat_id=admin_id, text=admin_message)
            except Exception as e:
                logger.error(f"通知管理员 {admin_id} 失败: {e}")

    logger.info(f"投稿被自动拒绝: user_id={user_id}, category={result.category}")


async def _send_to_manual_review(
    update: Update,
    context: CallbackContext,
    result: ReviewResult,
    user_info: dict,
    submission_data: dict
):
    """发送到人工审核队列"""
    user_id = user_info.get('user_id')
    username = user_info.get('username', '')

    # 保存到待审核队列
    from utils.submit_settings import get_snapshot
    submission_data = {**submission_data, "_settings": get_snapshot(context), "_destination": CHANNEL_ID}
    review_id = await _save_pending_review(user_id, username, submission_data, result)
    if not review_id:
        raise RuntimeError("Could not save review")
    context.user_data["pending_review_id"] = review_id

    # 通知用户
    await update.message.reply_text(
        "📋 您的投稿已提交审核\n\n"
        "管理员将尽快审核您的投稿，请耐心等待。\n"
        "审核结果将通过机器人通知您。"
    )

    # 通知管理员
    if ADMIN_IDS:
        content_preview = _get_content_preview(submission_data)
        keyboard = [
            [
                InlineKeyboardButton("✅ 通过", callback_data=f"review_approve_{review_id}"),
                InlineKeyboardButton("❌ 拒绝", callback_data=f"review_reject_{review_id}")
            ],
            [
                InlineKeyboardButton("🚫 拒绝并拉黑", callback_data=f"review_ban_{review_id}")
            ],
            [InlineKeyboardButton("查看完整正文", callback_data=f"review_full_{review_id}")],
        ]
        markup = InlineKeyboardMarkup(keyboard)

        admin_message = (
            "🔔 新投稿待审核\n\n"
            f"投稿人：@{username} (ID: {user_id})\n"
            f"投稿时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"内容：\n{content_preview}\n\n"
            f"标签：{submission_data.get('tags', '无')}\n"
            f"链接：{submission_data.get('link', '无')}\n\n"
            f"AI 审核结果：\n"
            f"• 置信度：{result.confidence:.0%}\n"
            f"• 分类：{result.category}\n"
            f"• 原因：{result.reason}"
        )

        for admin_id in ADMIN_IDS:
            try:
                from handlers.rich_content import FALLBACK_ERRORS
                from ui.rich_messages import review_blocks
                from utils.telegram_api import TelegramAPIError
                from utils.telegram_capabilities import feature_enabled
                api = getattr(context, "bot_data", {}).get("telegram_api")
                renderer = "plain"
                message_id = None
                if api and feature_enabled("rich_review", admin_id):
                    try:
                        sent = await api.call("sendRichMessage", {
                            "chat_id": admin_id, "reply_markup": markup.to_dict(),
                            "rich_message": review_blocks(submission_data, user_id, username, result.to_dict()),
                        })
                        message_id = sent["message_id"]
                        renderer = "rich"
                    except TelegramAPIError as exc:
                        if exc.category not in FALLBACK_ERRORS:
                            raise
                if message_id is None:
                    message = await context.bot.send_message(
                        chat_id=admin_id, text=admin_message, reply_markup=markup,
                    )
                    message_id = message.message_id
                from utils.message_views import remember_view
                await remember_view("review", str(review_id), admin_id, admin_id, message_id, renderer=renderer)
            except Exception as e:
                logger.error(f"通知管理员 {admin_id} 失败: {e}")

    logger.info(f"投稿已发送到人工审核: user_id={user_id}, review_id={review_id}")


async def _save_pending_review(
    user_id: int,
    username: str,
    submission_data: dict,
    review_result: ReviewResult
) -> int:
    """保存待审核投稿"""
    try:
        async with get_db() as conn:
            cursor = await conn.cursor()
            await cursor.execute('''
                INSERT INTO pending_reviews
                (user_id, username, submission_data, ai_review_result, status)
                VALUES (?, ?, ?, ?, 'pending')
            ''', (
                user_id,
                username,
                json.dumps(submission_data, ensure_ascii=False),
                json.dumps(review_result.to_dict(), ensure_ascii=False)
            ))
            await conn.commit()
            return cursor.lastrowid
    except Exception as e:
        logger.error(f"保存待审核投稿失败: {e}")
        return 0


async def handle_review_callback(update: Update, context: CallbackContext):
    from utils.operation_service import claim_review, get_operation, set_operation_state
    from utils.publish_service import publication_scope
    from utils.message_views import refresh_review_views
    from handlers.rich_content import edit_review_content
    from ui.rich_messages import review_blocks
    from utils.callback_context import MessageRef
    from utils.content_normalizer import telegram_field

    query = update.callback_query
    is_rich = isinstance(telegram_field(getattr(query, "message", None), "rich_message"), dict)

    async def edit_card(text, markup=None, rich=None):
        if is_rich:
            return await edit_review_content(
                context, MessageRef.from_query(query), renderer="rich",
                text=text, rich=rich, markup=markup,
            )
        return await query.edit_message_text(text, **({"reply_markup": markup} if markup else {}))

    admin_id = query.from_user.id
    if admin_id not in ADMIN_IDS:
        await query.answer("权限不足", show_alert=True)
        return
    try:
        prefix, action, id_text = query.data.split("_", 2)
        review_id = int(id_text)
        if action not in ("approve", "reject", "ban", "banok", "back", "full") or not 0 < review_id < 2**63:
            raise ValueError("Invalid review action")
    except (ValueError, AttributeError):
        await query.answer("审核操作无效", show_alert=True)
        return
    await query.answer("正在处理")
    async with get_db() as conn:
        row = await (
            await conn.execute("SELECT * FROM pending_reviews WHERE id=?", (review_id,))
        ).fetchone()
    if not row:
        await edit_card("审核记录不存在。")
        return
    if action == "full":
        from handlers.rich_content import split_plain_text
        from ui.rich_messages import PostContent

        content = PostContent.from_submission(json.loads(row["submission_data"]), [], show_submitter=False)
        ai = json.loads(row["ai_review_result"] or "{}")
        paragraphs = [*content.lines(), f"AI 审核：{ai.get('category') or '无'}", ai.get("reason") or ""]
        for chunk in split_plain_text("\n\n".join(filter(None, paragraphs))):
            await context.bot.send_message(chat_id=admin_id, text=chunk, disable_web_page_preview=True)
        return
    if row["status"] != "pending":
        await edit_card("该投稿已有审核结果，请勿重复处理。")
        return
    if action in ("ban", "back"):
        if action == "ban":
            text = f"确认拒绝稿件 #{review_id} 并禁止投稿人 {row['user_id']} 再次投稿？"
            buttons = [
                [
                    InlineKeyboardButton(
                        "确认拒绝并拉黑", callback_data=f"review_banok_{review_id}"
                    ),
                    InlineKeyboardButton(
                        "返回审核", callback_data=f"review_back_{review_id}"
                    ),
                ]
            ]
        else:
            text = f"待审核 #{review_id}\n\n" + _get_content_preview(
                json.loads(row["submission_data"])
            )
            buttons = [
                [
                    InlineKeyboardButton(
                        "通过", callback_data=f"review_approve_{review_id}"
                    ),
                    InlineKeyboardButton(
                        "拒绝", callback_data=f"review_reject_{review_id}"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "拒绝并拉黑…", callback_data=f"review_ban_{review_id}"
                    )
                ],
                [InlineKeyboardButton("查看完整正文", callback_data=f"review_full_{review_id}")],
            ]
        rich = review_blocks(
            json.loads(row["submission_data"]), row["user_id"], row["username"],
            json.loads(row["ai_review_result"] or "{}"),
        ) if is_rich and action == "back" else None
        await edit_card(text, InlineKeyboardMarkup(buttons), rich)
        return
    action = "ban" if action == "banok" else action
    claimed = await claim_review(review_id, action, admin_id)
    if not claimed:
        await edit_card("该投稿已由其他管理员处理。")
        return
    user_id = claimed["user_id"]
    operation_id = claimed["operation_id"]
    try:
        if action == "approve":
            await refresh_review_views(context, review_id, "审核已通过，正在发布。")
            async with publication_scope(operation_id):
                ok, error = await _publish_approved_submission(
                    context,
                    user_id,
                    claimed["username"],
                    json.loads(claimed["submission_data"]),
                )
            if not ok:
                raise RuntimeError("Publication incomplete")
            await set_operation_state(operation_id, "succeeded")
            text = f"已通过审核并发布。\n审核人：{admin_id}\n处理编号：{operation_id}"
        elif action == "reject":
            text = f"已拒绝。\n审核人：{admin_id}"
            await context.bot.send_message(
                chat_id=user_id, text="您的投稿未通过审核。如有疑问，请联系管理员。"
            )
        else:
            from utils.blacklist import add_to_blacklist

            try:
                added = await add_to_blacklist(user_id, f"审核拒绝并拉黑 by {admin_id}")
                if added is False:
                    raise RuntimeError("Blacklist update failed")
                text = f"已拒绝并拉黑。\n审核人：{admin_id}"
                async with get_db() as conn:
                    await conn.execute(
                        "UPDATE pending_reviews SET review_note='banned' WHERE id=?",
                        (review_id,),
                    )
            except Exception:
                text = f"已拒绝，拉黑未完成，请重试黑名单管理操作。\n审核人：{admin_id}"
    except Exception as exc:
        if operation_id:
            op = await get_operation(operation_id)
            if op["state"] == "processing":
                await set_operation_state(
                    operation_id, "failed", error_code=type(exc).__name__
                )
            elif op["state"] == "sending":
                await set_operation_state(
                    operation_id, "partial", error_code=type(exc).__name__
                )
            text = (
                f"审核已通过，发布结果需核对。请勿重复发布。\n处理编号：{operation_id}"
            )
        else:
            text = f"审核结果已保存，通知未送达。\n审核人：{admin_id}"
        logger.error("审核 %s 后续处理失败: %s", review_id, type(exc).__name__)
    await refresh_review_views(context, review_id, text)
    try:
        await edit_card(text)
    except Exception:
        pass


async def _publish_approved_submission(
    context: CallbackContext,
    user_id: int,
    username: str,
    submission_data: dict,
) -> tuple:
    """
    人工审核通过后，从 pending_reviews 中保存的完整 submission_data 执行发布。

    Returns:
        (success: bool, error_message: str)
    """
    from config.settings import CHANNEL_ID, OWNER_ID
    CHANNEL_ID = submission_data.get("_destination") or CHANNEL_ID
    from handlers.publish import (
        handle_media_publish,
        handle_text_publish,
        handle_document_publish,
        save_published_post,
    )
    from utils.helper_functions import build_caption
    from handlers.rich_content import add_post_view_button, send_resource, source_for_publication

    # 从 submission_data 还原发布所需的各字段
    text_content = submission_data.get('text_content') or None
    media_list = []
    doc_list = []
    try:
        raw_image = submission_data.get('image_id', '[]')
        if isinstance(raw_image, str):
            media_list = json.loads(raw_image)
        elif isinstance(raw_image, list):
            media_list = raw_image
    except (json.JSONDecodeError, TypeError):
        media_list = []

    try:
        raw_doc = submission_data.get('document_id', '[]')
        if isinstance(raw_doc, str):
            doc_list = json.loads(raw_doc)
        elif isinstance(raw_doc, list):
            doc_list = raw_doc
    except (json.JSONDecodeError, TypeError):
        doc_list = []

    if not media_list and not doc_list and not text_content:
        return (False, "投稿内容为空（无媒体、文档或文本）")

    # 构造一个类 dict 对象供 build_caption 使用
    caption_data = {
        'link': submission_data.get('link', ''),
        'title': submission_data.get('title', ''),
        'note': submission_data.get('note', ''),
        'tags': submission_data.get('tags', ''),
        'spoiler': submission_data.get('spoiler', 'false'),
        'user_id': user_id,
        'username': username,
    }

    show_submitter = submission_data.get("_settings", {}).get("show_submitter", runtime_settings.bot_show_submitter())
    caption = build_caption(caption_data, show_submitter=show_submitter)

    spoiler_value = submission_data.get('spoiler', 'false') or 'false'
    spoiler_flag = spoiler_value.lower() == 'true'

    sent_message = None
    all_message_ids = []

    snapshot = submission_data.get("_settings", {})
    rich_content = source_for_publication(
        context, snapshot, {**caption_data, "text_content": text_content},
        media_list, doc_list, spoiler_flag, show_submitter=show_submitter,
    )
    if rich_content:
        sent_message = await send_resource(context, rich_content, chat_id=CHANNEL_ID)
        all_message_ids.append(sent_message.message_id)
        if getattr(sent_message, "renderer", None) != "rich":
            rich_content = None
    # 发布纯文本
    elif text_content and not media_list and not doc_list:
        sent_message = await handle_text_publish(context, text_content, caption, spoiler_flag)
        if sent_message:
            all_message_ids.append(sent_message.message_id)

    # 发布媒体
    elif media_list:
        sent_message, all_message_ids = await handle_media_publish(context, media_list, caption, spoiler_flag)

    # 发布文档
    if doc_list and (not sent_message or media_list):
        if sent_message:
            doc_msg = await handle_document_publish(context, doc_list, None, sent_message.message_id)
            if doc_msg:
                all_message_ids.append(doc_msg.message_id)
        else:
            sent_message = await handle_document_publish(context, doc_list, caption)
            if sent_message:
                all_message_ids.append(sent_message.message_id)

    if not sent_message:
        return (False, "所有发送方式均失败")

    from utils.publish_service import current_operation_id
    from utils.operation_service import get_operation
    operation_id = current_operation_id()
    if operation_id:
        operation = await get_operation(operation_id)
        all_message_ids = json.loads(operation["result_json"]).get("message_ids", all_message_ids)

    # 保存到 published_posts
    try:
        rating = {}
        if rich_content and snapshot.get("rating_enabled"):
            from utils.rating_service import get_rating_service
            rating = await get_rating_service().get_or_create_subject_from_submission(
                {**caption_data, "text_content": text_content}, user_id=user_id,
            ) or {}
        await save_published_post(
            user_id,
            sent_message.message_id,
            caption_data,
            media_list,
            doc_list,
            all_message_ids,
            text_content,
            rating_subject_id=rating.get("subject_id"),
            rating_avg=rating.get("avg_score"),
            rating_votes=rating.get("vote_count"),
            show_submitter=show_submitter,
            rich_content=rich_content,
        )
        if rich_content:
            from ui.keyboards import Keyboards
            markup = Keyboards.rating_keyboard(rating["subject_id"], rating["avg_score"], rating["vote_count"]) if rating else None
            await add_post_view_button(context, sent_message.message_id, markup, chat_id=CHANNEL_ID)
    except Exception as e:
        logger.error(f"人工审核发布后保存记录失败: {e}", exc_info=True)

    # 保存投稿指纹
    if runtime_settings.duplicate_check_enabled():
        try:
            user_bio = ''
            try:
                chat = await context.bot.get_chat(user_id)
                user_bio = chat.bio or ''
            except Exception:
                pass
            await save_fingerprint_after_publish(
                user_id=user_id,
                username=username,
                submission_data=submission_data,
                user_bio=user_bio,
                submission_id=sent_message.message_id,
            )
        except Exception as e:
            logger.error(f"人工审核发布后保存指纹失败: {e}")

    # 生成投稿链接并通知用户
    try:
        if CHANNEL_ID.startswith('@'):
            channel_username = CHANNEL_ID.lstrip('@')
            submission_link = f"https://t.me/{channel_username}/{sent_message.message_id}"
        else:
            submission_link = "频道无公开链接"
        await context.bot.send_message(
            chat_id=user_id,
            text=f"🎉 投稿已成功发布到频道！\n点击以下链接查看投稿：\n{submission_link}",
        )
    except Exception as e:
        logger.error(f"人工审核发布后通知用户链接失败: {e}")

    # 通知所有者
    notify_owner = runtime_settings.bot_notify_owner()
    if notify_owner and OWNER_ID:
        try:
            if CHANNEL_ID.startswith('@'):
                channel_username = CHANNEL_ID.lstrip('@')
                submission_link = f"https://t.me/{channel_username}/{sent_message.message_id}"
            else:
                submission_link = "频道无公开链接"
            notification_text = (
                f"📨 新投稿通知（人工审核通过）\n\n"
                f"👤 投稿人：@{username} (ID: {user_id})\n"
                f"🔗 查看投稿: {submission_link}\n\n"
                f"⚙️ 管理操作:\n"
                f"封禁此用户: /blacklist_add {user_id} 违规内容"
            )
            await context.bot.send_message(chat_id=OWNER_ID, text=notification_text)
        except Exception as e:
            logger.error(f"人工审核发布后通知所有者失败: {e}")

    logger.info(f"人工审核通过后发布成功: user_id={user_id}, message_id={sent_message.message_id}")
    return (True, "")


async def save_fingerprint_after_publish(
    user_id: int,
    username: str,
    submission_data: dict,
    user_bio: str,
    submission_id: int
):
    """发布成功后保存指纹"""
    if not runtime_settings.duplicate_check_enabled():
        return

    try:
        content = _build_content_for_review(submission_data)
        extractor = get_feature_extractor()
        detector = get_duplicate_detector()

        fingerprint = extractor.create_fingerprint(
            user_id=user_id,
            username=username,
            content=content,
            bio=user_bio
        )

        await detector.save_fingerprint(
            fingerprint,
            status='approved',
            submission_id=submission_id
        )

    except Exception as e:
        logger.error(f"保存指纹失败: {e}")


def _build_content_for_review(submission_data: dict) -> str:
    """构建用于审核的内容字符串"""
    parts = []

    if submission_data.get('text_content'):
        parts.append(submission_data['text_content'])
    if submission_data.get('title'):
        parts.append(submission_data['title'])
    if submission_data.get('note'):
        parts.append(submission_data['note'])
    if submission_data.get('tags'):
        parts.append(submission_data['tags'])
    if submission_data.get('link'):
        parts.append(submission_data['link'])

    return '\n'.join(parts)


def _get_content_preview(submission_data: dict, max_length: int = 200) -> str:
    """获取内容预览"""
    content = submission_data.get('text_content', '') or submission_data.get('note', '')
    if len(content) > max_length:
        return content[:max_length] + "..."
    return content or "(无文本内容)"
