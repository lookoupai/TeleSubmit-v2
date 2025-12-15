"""
审核流程处理模块
处理 AI 审核和重复检测的完整流程
"""
import json
import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ConversationHandler, CallbackContext

from config.settings import (
    AI_REVIEW_ENABLED,
    AI_REVIEW_NOTIFY_USER,
    AI_REVIEW_NOTIFY_ADMIN_ON_REJECT,
    AI_REVIEW_NOTIFY_ADMIN_ON_DUPLICATE,
    AI_REVIEW_CHANNEL_TOPIC,
    DUPLICATE_CHECK_ENABLED,
    DUPLICATE_NOTIFY_USER,
    OWNER_ID,
    ADMIN_IDS,
    PAID_AD_ENABLED,
)
from database.db_manager import get_db
from utils.ai_reviewer import get_ai_reviewer, ReviewResult
from utils.duplicate_detector import get_duplicate_detector, DuplicateResult
from utils.feature_extractor import get_feature_extractor
from utils.paid_ad_service import get_balance

logger = logging.getLogger(__name__)


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

    # 构建完整内容用于审核
    content = _build_content_for_review(submission_data)

    # 1. 重复检测
    if DUPLICATE_CHECK_ENABLED:
        dup_result = await _check_duplicate(user_id, username, content, user_bio)
        if dup_result.is_duplicate:
            await _handle_duplicate_result(update, context, dup_result, user_info)
            return (False, False, dup_result.message)

    # 2. AI 审核
    if AI_REVIEW_ENABLED and not skip_ai_review:
        review_result = await _perform_ai_review(submission_data)

        reviewer = get_ai_reviewer()

        if reviewer.should_auto_approve(review_result):
            # 自动通过
            logger.info(f"投稿自动通过: user_id={user_id}, category={review_result.category}")
            return (True, True, "✅ 投稿审核通过！")

        elif reviewer.should_auto_reject(review_result):
            # 自动拒绝
            await _handle_rejection(update, context, review_result, user_info, submission_data)
            return (False, False, review_result.reason)

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
    user_info: dict
):
    """处理重复检测结果"""
    user_id = user_info.get('user_id')
    username = user_info.get('username', '')

    # 通知用户
    if DUPLICATE_NOTIFY_USER:
        if result.duplicate_type == 'rate_limit':
            message = (
                "⚠️ 投稿频率超限\n\n"
                f"{result.message}\n\n"
                "请稍后再试，或联系管理员。"
            )
        else:
            message = (
                "⚠️ 检测到重复投稿\n\n"
                f"{result.message}\n\n"
                "为保证频道内容质量，7 天内相同内容不可重复投稿。\n"
                "如有疑问，请联系管理员。"
            )
        await update.message.reply_text(message)

    # 通知管理员
    if AI_REVIEW_NOTIFY_ADMIN_ON_DUPLICATE and ADMIN_IDS:
        admin_message = (
            "🔔 重复投稿检测通知\n\n"
            f"用户：@{username} (ID: {user_id})\n"
            f"类型：{result.duplicate_type}\n"
            f"相似度：{result.similarity_score:.0%}\n"
            f"详情：{result.message}"
        )
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(chat_id=admin_id, text=admin_message)
            except Exception as e:
                logger.error(f"通知管理员 {admin_id} 失败: {e}")

    logger.info(f"重复投稿被拦截: user_id={user_id}, type={result.duplicate_type}")


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
    if AI_REVIEW_NOTIFY_USER:
        reviewer = get_ai_reviewer()
        if PAID_AD_ENABLED and reviewer.is_off_topic_category(result.category):
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
                f"本频道仅接受与「{AI_REVIEW_CHANNEL_TOPIC}」相关的内容投稿。\n"
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
    review_id = await _save_pending_review(user_id, username, submission_data, result)

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
            ]
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
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=admin_message,
                    reply_markup=markup
                )
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
    """处理审核回调（管理员操作）"""
    query = update.callback_query
    await query.answer()

    data = query.data
    admin_id = query.from_user.id

    # 验证管理员权限
    if admin_id not in ADMIN_IDS:
        await query.edit_message_text("⛔ 权限不足")
        return

    try:
        parts = data.split('_')
        action = parts[1]  # approve/reject/ban
        review_id = int(parts[2])

        # 获取待审核记录
        async with get_db() as conn:
            cursor = await conn.cursor()
            await cursor.execute('''
                SELECT * FROM pending_reviews WHERE id = ?
            ''', (review_id,))
            row = await cursor.fetchone()

            if not row:
                await query.edit_message_text("❌ 审核记录不存在")
                return

            if row['status'] != 'pending':
                await query.edit_message_text("❌ 该投稿已被处理")
                return

            user_id = row['user_id']
            username = row['username']
            submission_data = json.loads(row['submission_data'])

            if action == 'approve':
                # 通过审核
                await cursor.execute('''
                    UPDATE pending_reviews
                    SET status = 'approved', reviewed_at = ?, reviewed_by = ?
                    WHERE id = ?
                ''', (datetime.now().timestamp(), admin_id, review_id))
                await conn.commit()

                # 通知用户
                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text="✅ 您的投稿已通过审核！\n内容即将发布到频道。"
                    )
                except Exception as e:
                    logger.error(f"通知用户 {user_id} 失败: {e}")

                # TODO: 执行发布流程
                await query.edit_message_text(
                    f"✅ 已通过审核\n\n"
                    f"投稿人：@{username}\n"
                    f"审核人：{query.from_user.username or admin_id}"
                )

            elif action == 'reject':
                # 拒绝
                await cursor.execute('''
                    UPDATE pending_reviews
                    SET status = 'rejected', reviewed_at = ?, reviewed_by = ?
                    WHERE id = ?
                ''', (datetime.now().timestamp(), admin_id, review_id))
                await conn.commit()

                # 通知用户
                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text="❌ 您的投稿未通过审核\n\n"
                             f"本频道仅接受与「{AI_REVIEW_CHANNEL_TOPIC}」相关的内容。\n"
                             "如有疑问，请联系管理员。"
                    )
                except Exception as e:
                    logger.error(f"通知用户 {user_id} 失败: {e}")

                await query.edit_message_text(
                    f"❌ 已拒绝\n\n"
                    f"投稿人：@{username}\n"
                    f"审核人：{query.from_user.username or admin_id}"
                )

            elif action == 'ban':
                # 拒绝并拉黑
                await cursor.execute('''
                    UPDATE pending_reviews
                    SET status = 'rejected', reviewed_at = ?, reviewed_by = ?, review_note = 'banned'
                    WHERE id = ?
                ''', (datetime.now().timestamp(), admin_id, review_id))
                await conn.commit()

                # 添加到黑名单
                from utils.blacklist import add_to_blacklist
                add_to_blacklist(user_id, f"投稿审核拒绝并拉黑 by {admin_id}")

                # 通知用户
                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text="⚠️ 您已被加入黑名单\n\n"
                             "由于您的投稿内容不符合频道要求，您已被禁止使用投稿功能。\n"
                             "如有疑问，请联系管理员。"
                    )
                except Exception as e:
                    logger.error(f"通知用户 {user_id} 失败: {e}")

                await query.edit_message_text(
                    f"🚫 已拒绝并拉黑\n\n"
                    f"投稿人：@{username} (ID: {user_id})\n"
                    f"审核人：{query.from_user.username or admin_id}"
                )

    except Exception as e:
        logger.error(f"处理审核回调失败: {e}", exc_info=True)
        await query.edit_message_text(f"❌ 处理失败: {str(e)}")


async def save_fingerprint_after_publish(
    user_id: int,
    username: str,
    submission_data: dict,
    user_bio: str,
    submission_id: int
):
    """发布成功后保存指纹"""
    if not DUPLICATE_CHECK_ENABLED:
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
