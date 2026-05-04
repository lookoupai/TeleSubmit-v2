"""
模式选择处理模块
"""
import logging
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton
from telegram.ext import ConversationHandler, CallbackContext

from config.settings import (
    MODE_MEDIA, MODE_DOCUMENT, MODE_MIXED, MODE_TEXT, MODE_ALL,
    TEXT_ONLY_MODE, DEFAULT_SUBMIT_MODE
)
from utils.file_validator import create_file_validator
from models.state import STATE
from database.db_manager import get_db, cleanup_old_data
from utils.blacklist import is_blacklisted
from utils.submit_settings import ensure_snapshot, get_snapshot
from utils import runtime_settings
from ui.keyboards import Keyboards
from handlers.text_handlers import show_text_welcome
from handlers.slot_ad_handlers import try_handle_start_args

logger = logging.getLogger(__name__)


def _active_submit_mode(context: CallbackContext) -> str:
    """
    根据当前入口选择投稿模式：普通投稿与付费广告可独立配置。
    """
    if bool((context.user_data or {}).get("paid_ad")) and runtime_settings.paid_ad_enabled():
        return runtime_settings.paid_ad_submit_mode()
    return runtime_settings.bot_mode()


async def submit(update: Update, context: CallbackContext) -> int:
    """
    处理 /submit 命令，开始投稿流程
    
    Args:
        update: Telegram 更新对象
        context: 回调上下文
        
    Returns:
        int: 下一个会话状态
    """
    logger.info(f"收到 /submit 命令，user_id: {update.effective_user.id}")
    await cleanup_old_data()
    user_id = update.effective_user.id
    if context.user_data.pop("slot_ad_flow", None) is not None:
        logger.info(f"开始投稿前清理残留 slot_ad_flow，user_id: {user_id}")
    
    # 获取用户名信息
    user = update.effective_user
    username = user.username or f"user{user.id}"

    # 初始化投稿会话配置快照（保证同一次投稿流程一致性）
    # 注意：快照内会根据白名单策略应用“放宽项”，并只对本次会话生效（避免会话中规则漂移）
    ensure_snapshot(context, user_id=user_id)
    
    # 检查用户是否在黑名单中
    if is_blacklisted(user_id):
        logger.warning(f"黑名单用户尝试使用机器人，user_id: {user_id}")
        await update.message.reply_text("⚠️ 您已被列入黑名单，无法使用投稿功能。如有疑问，请联系管理员。")
        return ConversationHandler.END
    
    try:
        async with get_db() as conn:
            c = await conn.cursor()
            # 清除旧会话记录
            await c.execute("DELETE FROM submissions WHERE user_id=?", (user_id,))

            # 根据配置决定模式
            bot_mode = _active_submit_mode(context)
            if bot_mode == MODE_TEXT:
                # 仅纯文本模式
                mode = "text"
                logger.info(f"使用纯文本模式，user_id: {user_id}")
                await c.execute(
                    "INSERT INTO submissions (user_id, timestamp, mode, image_id, document_id, username) VALUES (?, ?, ?, ?, ?, ?)",
                    (user_id, datetime.now().timestamp(), mode, "[]", "[]", username)
                )
                await conn.commit()
                await show_text_welcome(update, context)
                logger.info(f"已发送纯文本欢迎信息，切换到TEXT_CONTENT状态，user_id: {user_id}")
                return STATE['TEXT_CONTENT']

            elif bot_mode == MODE_MEDIA:
                mode = "media"
                logger.info(f"使用媒体模式，user_id: {user_id}")
                await c.execute("INSERT INTO submissions (user_id, timestamp, mode, image_id, document_id, username) VALUES (?, ?, ?, ?, ?, ?)",
                          (user_id, datetime.now().timestamp(), mode, "[]", "[]", username))
                await conn.commit()
                await show_media_welcome(update, context)
                logger.info(f"已发送媒体欢迎信息，切换到MEDIA状态，user_id: {user_id}")
                return STATE['MEDIA']

            elif bot_mode == MODE_DOCUMENT:
                mode = "document"
                logger.info(f"使用文档模式，user_id: {user_id}")
                await c.execute("INSERT INTO submissions (user_id, timestamp, mode, image_id, document_id, username) VALUES (?, ?, ?, ?, ?, ?)",
                          (user_id, datetime.now().timestamp(), mode, "[]", "[]", username))
                await conn.commit()
                await show_document_welcome(update, context)
                logger.info(f"已发送文档欢迎信息，切换到DOC状态，user_id: {user_id}")
                return STATE['DOC']

            elif bot_mode == MODE_ALL:
                # 全部模式：文本+媒体+文档
                logger.info(f"使用全部模式（ALL），user_id: {user_id}")
                await c.execute(
                    "INSERT INTO submissions (user_id, timestamp, mode, image_id, document_id, username) VALUES (?, ?, ?, ?, ?, ?)",
                    (user_id, datetime.now().timestamp(), "all", "[]", "[]", username)
                )
                await conn.commit()

                # 显示三选一键盘
                text_button = '📝 纯文本'
                media_button = '🖼 媒体投稿'
                doc_button = '📁 文档投稿'
                keyboard = [[KeyboardButton(text_button), KeyboardButton(media_button), KeyboardButton(doc_button)]]
                markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)

                await update.message.reply_text(
                    "📮 欢迎使用投稿机器人！请选择投稿类型：\n\n"
                    "- 📝 纯文本：直接发送文字内容投稿\n"
                    "  适用场景：发布文字公告、信息分享等\n\n"
                    "- 🖼 媒体投稿：用于提交图片、视频、GIF等媒体文件\n"
                    "  适用场景：直接通过Telegram选择相册中的图片/视频发送\n\n"
                    "- 📁 文档投稿：用于提交压缩包、PDF、DOC等文档文件\n"
                    "  适用场景：通过文件附件方式发送各类资源文件\n\n"
                    "⏱️ 操作超时提醒：如果5分钟内没有操作，会话将自动结束。",
                    reply_markup=markup
                )
                logger.info(f"已发送全部模式选择提示，切换到START_MODE状态，user_id: {user_id}")
                return STATE['START_MODE']

            else:  # 混合模式 (MIXED)
                # 先创建数据库记录
                logger.info(f"使用混合模式，user_id: {user_id}")
                await c.execute("INSERT INTO submissions (user_id, timestamp, mode, image_id, document_id, username) VALUES (?, ?, ?, ?, ?, ?)",
                          (user_id, datetime.now().timestamp(), "mixed", "[]", "[]", username))
                await conn.commit()

                # 显示模式选择键盘
                media_button = '📷 媒体投稿'
                doc_button = '📄 文档投稿'
                keyboard = [[KeyboardButton(media_button), KeyboardButton(doc_button)]]
                markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
                logger.info(f"创建选择键盘，按钮: '{media_button}', '{doc_button}'")

                await update.message.reply_text(
                    "📮 欢迎使用投稿机器人！请选择投稿类型：\n\n"
                    "- 📷 媒体投稿：用于提交图片、视频、GIF等媒体文件\n"
                    "  适用场景：直接通过Telegram选择相册中的图片/视频发送\n"
                    "  注意：媒体模式不支持作为文档附件发送的文件\n\n"
                    "- 📄 文档投稿：用于提交压缩包、PDF、DOC等文档文件\n"
                    "  适用场景：通过文件附件方式发送各类压缩包资源、文档或原始媒体文件\n"
                    "  注意：如果您需要以文件附件形式上传媒体，请选择此模式\n\n"
                    "⏱️ 操作超时提醒：如果5分钟内没有操作，会话将自动结束，需要重新发送 /submit。",
                    reply_markup=markup
                )
                logger.info(f"已发送模式选择提示，切换到START_MODE状态，user_id: {user_id}")
                return STATE['START_MODE']
    except Exception as e:
        logger.error(f"初始化数据错误: {e}", exc_info=True)
        await update.message.reply_text("❌ 初始化失败，请稍后再试")
        return ConversationHandler.END

async def start(update: Update, context: CallbackContext) -> int:
    """
    处理 /start 命令，显示欢迎信息和可用操作
    
    Args:
        update: Telegram 更新对象
        context: 回调上下文
        
    Returns:
        int: 结束会话
    """
    logger.info(f"收到 /start 命令，user_id: {update.effective_user.id}")
    await cleanup_old_data()
    user_id = update.effective_user.id
    if context.user_data.pop("slot_ad_flow", None) is not None:
        logger.info(f"处理 /start 前清理残留 slot_ad_flow，user_id: {user_id}")
    
    # 获取用户名信息
    user = update.effective_user
    username = user.username or user.first_name or f"user{user.id}"
    
    # 检查用户是否在黑名单中
    if is_blacklisted(user_id):
        logger.warning(f"黑名单用户尝试使用机器人，user_id: {user_id}")
        await update.message.reply_text("⚠️ 您已被列入黑名单，无法使用投稿功能。如有疑问，请联系管理员。")
        return ConversationHandler.END

    # /start 深链（如 buy_slot_x）优先处理
    try:
        handled = await try_handle_start_args(update, context)
        if handled:
            return ConversationHandler.END
    except Exception as e:
        logger.error(f"处理 /start 深链失败: {e}", exc_info=True)
    
    # 显示欢迎信息和可用操作
    welcome_message = f"👋 你好 {username}！欢迎使用投稿机器人！\n\n"
    welcome_message += "🤖 **我能做什么？**\n\n"
    welcome_message += "📮 **投稿功能**\n"
    welcome_message += "• /submit - 开始新投稿\n"
    welcome_message += "  支持媒体投稿（图片/视频）和文档投稿（压缩包/PDF等）\n\n"
    
    welcome_message += "📊 **查询功能**\n"
    welcome_message += "• /search - 搜索历史投稿\n"
    welcome_message += "• /mystats - 查看我的投稿统计\n"
    welcome_message += "• /myposts - 查看我的投稿列表\n\n"
    
    welcome_message += "🔥 **热门排行**\n"
    welcome_message += "• /hot - 查看热门投稿排行榜\n"
    welcome_message += "• /tags - 查看热门标签云\n\n"
    
    welcome_message += "❓ **帮助**\n"
    welcome_message += "• /help - 查看完整帮助信息\n"
    welcome_message += "• /cancel - 取消当前投稿\n\n"
    
    welcome_message += "💡 **快速开始**\n"
    welcome_message += "想要投稿？直接发送 /submit 命令即可开始！"
    
    # 根据身份显示不同菜单
    try:
        reply_markup = Keyboards.main_menu()
    except Exception:
        reply_markup = ReplyKeyboardRemove()
    await update.message.reply_text(
        welcome_message,
        parse_mode='Markdown',
        reply_markup=reply_markup
    )
    logger.info(f"已发送欢迎信息，user_id: {user_id}")
    return ConversationHandler.END

async def select_mode(update: Update, context: CallbackContext) -> int:
    """
    处理用户模式选择

    Args:
        update: Telegram 更新对象
        context: 回调上下文

    Returns:
        int: 下一个会话状态
    """
    user_id = update.effective_user.id
    text = update.message.text

    # 增加调试日志
    logger.info(f"处理模式选择，用户输入: '{text}'，user_id: {user_id}")

    try:
        async with get_db() as conn:
            c = await conn.cursor()

            # 使用更灵活的匹配方式
            if "纯文本" in text or "📝" in text:
                # 选择纯文本投稿模式
                logger.info(f"用户选择纯文本模式，user_id: {user_id}")
                await c.execute("UPDATE submissions SET mode=?, image_id=?, document_id=? WHERE user_id=?",
                                ("text", "[]", "[]", user_id))
                await conn.commit()
                await update.message.reply_text("✅ 已选择纯文本投稿模式", reply_markup=ReplyKeyboardRemove())
                await show_text_welcome(update, context)
                return STATE['TEXT_CONTENT']

            elif "媒体" in text or "📷" in text or "🖼" in text:
                # 选择媒体投稿模式
                logger.info(f"用户选择媒体模式，user_id: {user_id}")
                await c.execute("UPDATE submissions SET mode=?, image_id=?, document_id=? WHERE user_id=?",
                                ("media", "[]", "[]", user_id))
                await conn.commit()
                await update.message.reply_text("✅ 已选择媒体投稿模式", reply_markup=ReplyKeyboardRemove())
                await show_media_welcome(update, context)
                return STATE['MEDIA']

            elif "文档" in text or "📄" in text or "📁" in text:
                # 选择文档投稿模式
                logger.info(f"用户选择文档模式，user_id: {user_id}")
                await c.execute("UPDATE submissions SET mode=?, image_id=?, document_id=? WHERE user_id=?",
                                ("document", "[]", "[]", user_id))
                await conn.commit()
                await update.message.reply_text("✅ 已选择文档投稿模式", reply_markup=ReplyKeyboardRemove())
                await show_document_welcome(update, context)
                return STATE['DOC']

            else:
                # 无效选择，根据当前模式显示不同键盘
                logger.warning(f"无效的模式选择: '{text}'，user_id: {user_id}")

                if _active_submit_mode(context) == MODE_ALL:
                    text_button = '📝 纯文本'
                    media_button = '🖼 媒体投稿'
                    doc_button = '📁 文档投稿'
                    keyboard = [[KeyboardButton(text_button), KeyboardButton(media_button), KeyboardButton(doc_button)]]
                else:
                    media_button = '📷 媒体投稿'
                    doc_button = '📄 文档投稿'
                    keyboard = [[KeyboardButton(media_button), KeyboardButton(doc_button)]]

                markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
                await update.message.reply_text(
                    "⚠️ 请选择有效的投稿类型：",
                    reply_markup=markup
                )
                return STATE['START_MODE']
    except Exception as e:
        logger.error(f"模式选择错误: {e}", exc_info=True)
        await update.message.reply_text("❌ 模式选择失败，请稍后再试", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

async def show_media_welcome(update, context: CallbackContext):
    """
    显示媒体投稿欢迎信息
    
    Args:
        update: Telegram 更新对象
    """
    snapshot = get_snapshot(context)
    allowed_tags = int(snapshot.get("allowed_tags", 30))
    max_media_media_mode = int(snapshot.get("max_media_media_mode", 50))
    require_one = bool(snapshot.get("media_mode_require_one", True))
    media_required_text = "必选" if require_one else "可选"
    tags_step = (
        "2️⃣ 标签：\n"
        "   - 当前不收集标签，将自动跳过\n\n"
        if allowed_tags <= 0
        else
        "2️⃣ 发送标签（必选）：\n"
        f"   - 最多{allowed_tags}个标签，用逗号分隔（例如：明日方舟，原神）。\n\n"
    )
    await update.message.reply_text(
        "📮 欢迎使用媒体投稿功能！请按照以下步骤提交：\n\n"
        f"1️⃣ 发送媒体文件（{media_required_text}）：\n"
        f"   - 支持图片、视频、GIF、音频等，最多上传{max_media_media_mode}个文件。\n"
        "   - 📱 请直接发送媒体（非文件附件形式）：\n"
        "     • 从相册选择后直接发送\n"
        "     • 直接发送视频/GIF\n"
        "   - ⚠️ 不支持以文件附件方式发送的媒体文件\n"
        "   - ⚠️ 如需以文件附件形式上传媒体，请使用文档投稿模式\n"
        "   - 上传完毕后，请发送 /done_media。\n\n"
        f"{tags_step}"
        "3️⃣ 发送链接（可选）：\n"
        "   - 如需附加链接，请确保以 http:// 或 https:// 开头；不需要请回复 \"无\" 或发送 /skip_optional 跳过后面的所有可选项。\n\n"
        "4️⃣ 发送标题（可选）：\n"
        "   - 如不需要标题，请回复 \"无\" 或发送 /skip_optional 跳过后面的所有可选项。\n\n"
        "5️⃣ 发送简介（可选）：\n"
        "   - 如不需要简介，请回复 \"无\" 或发送 /skip_optional 跳过后面的所有可选项。\n\n"
        "6️⃣ 是否将所有媒体设为剧透（点击查看）？\n"
        "   - 请回复 \"否\" 或 \"是\"。\n\n"
        "⏱️ 操作超时提醒：\n"
        "   - 如果5分钟内没有操作，会话将自动结束，需要重新发送 /start。\n\n"
        "随时发送 /cancel 取消投稿。"
    )

async def show_document_welcome(update, context: CallbackContext):
    """
    显示文档投稿欢迎信息
    
    Args:
        update: Telegram 更新对象
    """
    snapshot = get_snapshot(context)
    allowed_file_types = str(snapshot.get("allowed_file_types") or "*")
    allowed_tags = int(snapshot.get("allowed_tags", 30))
    max_docs = int(snapshot.get("max_docs", 10))
    max_media_default = int(snapshot.get("max_media_default", 10))
    file_validator = create_file_validator(allowed_file_types)
    allowed_types_desc = file_validator.get_allowed_types_description()
    tags_step = (
        "3️⃣ 标签：\n"
        "   - 当前不收集标签，将自动跳过\n\n"
        if allowed_tags <= 0
        else
        "3️⃣ 发送标签（必选）：\n"
        f"   - 最多{allowed_tags}个标签，用逗号分隔（例如：教程，资料，软件）。\n\n"
    )
    await update.message.reply_text(
        "📮 欢迎使用文档投稿功能！请按照以下步骤提交：\n\n"
        "1️⃣ 发送文档文件（必选）：\n"
        f"   - 至少上传1个文件，最多上传{max_docs}个文件。\n"
        "   - 📎 请以文件附件形式发送：\n"
        "     • 点击聊天输入框旁的📎图标\n"
        "     • 选择文件或文档\n"
        f"   - ✅ 允许的文件类型：\n{allowed_types_desc}\n"
        "   - 上传完毕后，请发送 /done_doc。\n\n"
        "2️⃣ 发送媒体文件（可选）：\n"
        f"   - 支持图片、视频、GIF、音频等，最多上传{max_media_default}个文件。\n"
        "   - 📱 请直接发送媒体（非文件附件形式）：\n"
        "     • 从相册选择后直接发送\n"
        "     • 直接发送视频/GIF\n"
        "   - 上传完毕后，请发送 /done_media，或发送 /skip_media 跳过此步骤。\n\n"
        f"{tags_step}"
        "4️⃣ 发送链接（可选）：\n"
        "   - 如需附加链接，请确保以 http:// 或 https:// 开头；不需要请回复 \"无\" 或发送 /skip_optional 跳过后面的所有可选项。\n\n"
        "5️⃣ 发送标题（可选）：\n"
        "   - 如不需要标题，请回复 \"无\" 或发送 /skip_optional 跳过后面的所有可选项。\n\n"
        "6️⃣ 发送简介（可选）：\n"
        "   - 如不需要简介，请回复 \"无\" 或发送 /skip_optional 跳过后面的所有可选项。\n\n"
        "7️⃣ 是否将内容设为剧透（点击查看）？\n"
        "   - 请回复 \"否\" 或 \"是\"。\n\n"
        "⏱️ 操作超时提醒：\n"
        "   - 如果5分钟内没有操作，会话将自动结束，需要重新发送 /start。\n\n"
        "随时发送 /cancel 取消投稿。"
    )
