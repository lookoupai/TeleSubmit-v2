"""
纯文本投稿处理模块
处理纯文本模式的投稿流程
"""
import logging
from datetime import datetime
from telegram import Update, ReplyKeyboardRemove
from telegram.ext import ConversationHandler, CallbackContext

from config.settings import MIN_TEXT_LENGTH, MAX_TEXT_LENGTH
from models.state import STATE
from database.db_manager import get_db

logger = logging.getLogger(__name__)


async def handle_text_content(update: Update, context: CallbackContext) -> int:
    """
    处理纯文本内容输入

    Args:
        update: Telegram 更新对象
        context: 回调上下文

    Returns:
        int: 下一个会话状态
    """
    user_id = update.effective_user.id
    text_content = update.message.text

    logger.info(f"收到纯文本投稿内容，user_id: {user_id}, 长度: {len(text_content)}")

    # 验证内容长度
    if len(text_content) < MIN_TEXT_LENGTH:
        await update.message.reply_text(
            f"⚠️ 投稿内容太短，至少需要 {MIN_TEXT_LENGTH} 个字符。\n"
            f"当前长度：{len(text_content)} 个字符\n\n"
            "请重新输入投稿内容："
        )
        return STATE['TEXT_CONTENT']

    if len(text_content) > MAX_TEXT_LENGTH:
        await update.message.reply_text(
            f"⚠️ 投稿内容超过限制，最多 {MAX_TEXT_LENGTH} 个字符。\n"
            f"当前长度：{len(text_content)} 个字符\n\n"
            "请缩短内容后重新输入："
        )
        return STATE['TEXT_CONTENT']

    try:
        async with get_db() as conn:
            c = await conn.cursor()
            # 保存文本内容
            await c.execute(
                "UPDATE submissions SET text_content=? WHERE user_id=?",
                (text_content, user_id)
            )
            await conn.commit()

        await update.message.reply_text(
            f"✅ 已收到投稿内容（{len(text_content)} 字符）\n\n"
            "📌 请输入标签（必填）：\n"
            "• 最多30个标签，用逗号分隔\n"
            "• 例如：接码,短信验证,虚拟号码\n\n"
            "随时发送 /cancel 取消投稿。"
        )
        return STATE['TAG']

    except Exception as e:
        logger.error(f"保存文本内容失败: {e}", exc_info=True)
        await update.message.reply_text("❌ 保存内容失败，请稍后再试")
        return ConversationHandler.END


async def show_text_welcome(update: Update):
    """
    显示纯文本投稿欢迎信息

    Args:
        update: Telegram 更新对象
    """
    await update.message.reply_text(
        "📝 欢迎使用纯文本投稿功能！\n\n"
        "请按照以下步骤提交：\n\n"
        "1️⃣ 发送投稿内容（必填）：\n"
        f"   - 字数限制：{MIN_TEXT_LENGTH} ~ {MAX_TEXT_LENGTH} 字符\n"
        "   - 请直接发送您的投稿文本\n\n"
        "2️⃣ 发送标签（必填）：\n"
        "   - 最多30个标签，用逗号分隔\n"
        "   - 例如：接码,短信验证,虚拟号码\n\n"
        "3️⃣ 发送链接（可选）：\n"
        "   - 如需附加链接，请确保以 http:// 或 https:// 开头\n"
        "   - 不需要请回复 \"无\" 或发送 /skip_optional\n\n"
        "⏱️ 操作超时提醒：\n"
        "   - 如果5分钟内没有操作，会话将自动结束\n\n"
        "随时发送 /cancel 取消投稿。\n\n"
        "📝 请现在发送您的投稿内容：",
        reply_markup=ReplyKeyboardRemove()
    )
