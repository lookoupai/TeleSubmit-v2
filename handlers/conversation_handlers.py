"""
会话处理器模块
"""
import json
import logging
from datetime import datetime
from telegram import (
    Update,
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaAnimation,
    InputMediaAudio
)
from telegram.ext import ConversationHandler, CallbackContext

from config.settings import CHANNEL_ID
from models.state import STATE
from database.db_manager import get_db
from utils.helper_functions import (
    process_tags, 
    build_caption, 
    validate_state, 
    safe_send
)
from utils.submit_settings import get_snapshot
from handlers.publish import publish_submission

logger = logging.getLogger(__name__)

@validate_state(STATE['MEDIA'])
async def handle_media(update: Update, context: CallbackContext) -> int:
    """
    处理媒体文件上传
    
    Args:
        update: Telegram 更新对象
        context: 回调上下文
        
    Returns:
        int: 当前会话状态
    """
    logger.info(f"处理媒体输入，user_id: {update.effective_user.id}")
    user_id = update.effective_user.id
    new_media = None

    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        new_media = f"photo:{file_id}"
    elif update.message.video:
        file_id = update.message.video.file_id
        new_media = f"video:{file_id}"
    elif update.message.animation:
        file_id = update.message.animation.file_id
        new_media = f"animation:{file_id}"
    elif update.message.audio:
        file_id = update.message.audio.file_id
        new_media = f"audio:{file_id}"
    elif update.message.document:
        mime = update.message.document.mime_type
        if mime == "image/gif":
            file_id = update.message.document.file_id
            new_media = f"animation:{file_id}"
        elif mime.startswith("audio/"):
            file_id = update.message.document.file_id
            new_media = f"audio:{file_id}"
        else:
            await update.message.reply_text("⚠️ 不支持的文件类型，请发送支持的媒体")
            return STATE['MEDIA']
    else:
        await update.message.reply_text("⚠️ 请发送支持的媒体文件")
        return STATE['MEDIA']

    try:
        async with get_db() as conn:
            c = await conn.cursor()
            await c.execute("SELECT image_id FROM submissions WHERE user_id=?", (user_id,))
            row = await c.fetchone()
            media_list = json.loads(row["image_id"]) if row and row["image_id"] else []
            media_list.append(new_media)
            await c.execute("UPDATE submissions SET image_id=? WHERE user_id=?",
                      (json.dumps(media_list), user_id))
        logger.info(f"当前媒体数量：{len(media_list)}")
        await update.message.reply_text(f"✅ 已接收媒体，共计 {len(media_list)} 个。\n继续发送媒体文件，或发送 /done 完成上传。")
    except Exception as e:
        logger.error(f"媒体保存错误: {e}")
        await update.message.reply_text("❌ 媒体保存失败，请稍后再试")
        return ConversationHandler.END
    return STATE['MEDIA']

@validate_state(STATE['MEDIA'])
async def done_media(update: Update, context: CallbackContext) -> int:
    """
    完成媒体上传，进入下一阶段
    
    Args:
        update: Telegram 更新对象
        context: 回调上下文
        
    Returns:
        int: 下一个会话状态
    """
    logger.info(f"媒体上传结束，user_id: {update.effective_user.id}")
    user_id = update.effective_user.id
    try:
        async with get_db() as conn:
            c = await conn.cursor()
            await c.execute("SELECT image_id FROM submissions WHERE user_id=?", (user_id,))
            row = await c.fetchone()
            if not row or not row["image_id"]:
                await update.message.reply_text("⚠️ 请至少发送一个媒体文件")
                return STATE['MEDIA']
    except Exception as e:
        logger.error(f"检索媒体错误: {e}")
        await update.message.reply_text("❌ 内部错误，请稍后再试")
        return ConversationHandler.END
    snapshot = get_snapshot(context)
    allowed_tags = int(snapshot.get("allowed_tags", 30))
    if allowed_tags <= 0:
        try:
            async with get_db() as conn:
                c = await conn.cursor()
                await c.execute("UPDATE submissions SET tags=? WHERE user_id=?", ("", user_id))
        except Exception:
            pass
        await update.message.reply_text(
            "✅ 媒体接收完成。\n\n"
            "📌 当前不收集标签，将进入链接输入（可选）：\n"
            "• 不需要请回复“无”或发送 /skip_optional\n"
            "• 需要请以 http:// 或 https:// 开头"
        )
        return STATE['LINK']

    await update.message.reply_text(f"✅ 媒体接收完成，请发送标签（必选，最多{allowed_tags}个，用逗号分隔，例如：明日方舟，原神）")
    return STATE['TAG']

@validate_state(STATE['TAG'])
async def handle_tag(update: Update, context: CallbackContext) -> int:
    """
    处理标签输入
    
    Args:
        update: Telegram 更新对象
        context: 回调上下文
        
    Returns:
        int: 下一个会话状态
    """
    logger.info(f"处理标签输入，user_id: {update.effective_user.id}")
    user_id = update.effective_user.id
    raw_tags = update.message.text.strip()
    snapshot = get_snapshot(context)
    allowed_tags = int(snapshot.get("allowed_tags", 30))
    if allowed_tags <= 0:
        processed_tags = ""
    else:
        success, processed_tags = process_tags(raw_tags, allowed_tags)
        if not success or not processed_tags:
            await update.message.reply_text(f"❌ 标签格式错误，请重新输入（最多{allowed_tags}个，用逗号分隔）")
            return STATE['TAG']
    try:
        async with get_db() as conn:
            c = await conn.cursor()
            await c.execute("UPDATE submissions SET tags=? WHERE user_id=?",
                      (processed_tags, user_id))
        logger.info(f"标签保存成功，user_id: {user_id}")
    except Exception as e:
        logger.error(f"标签保存错误: {e}")
        await update.message.reply_text("❌ 标签保存失败，请稍后再试")
        return ConversationHandler.END
    if allowed_tags <= 0:
        await update.message.reply_text(
            "✅ 当前不收集标签，已忽略标签输入。\n\n"
            "请发送链接（可选，不需要请回复 “无” 或发送 /skip_optional 跳过后面的所有可选项 。需填写请以 http:// 或 https:// 开头）"
        )
    else:
        await update.message.reply_text(
            "✅ 标签已保存，请发送链接（可选，不需要请回复 “无” 或发送 /skip_optional 跳过后面的所有可选项 。需填写请以 http:// 或 https:// 开头）"
        )
    return STATE['LINK']

@validate_state(STATE['LINK'])
async def handle_link(update: Update, context: CallbackContext) -> int:
    """
    处理链接输入
    
    Args:
        update: Telegram 更新对象
        context: 回调上下文
        
    Returns:
        int: 下一个会话状态
    """
    logger.info(f"处理链接输入，user_id: {update.effective_user.id}")
    user_id = update.effective_user.id
    link = update.message.text.strip()
    if link.lower() == "无":
        link = ""
    elif not link.startswith(('http://', 'https://')):
        await update.message.reply_text("⚠️ 链接格式不正确，请以 http:// 或 https:// 开头，或回复“无”跳过")
        return STATE['LINK']
    try:
        async with get_db() as conn:
            c = await conn.cursor()
            await c.execute("UPDATE submissions SET link=? WHERE user_id=?",
                      (link, user_id))
        logger.info(f"链接保存成功，user_id: {user_id}")
    except Exception as e:
        logger.error(f"链接保存错误: {e}")
        await update.message.reply_text("❌ 链接保存失败，请稍后再试")
        return ConversationHandler.END
    await update.message.reply_text("✅ 链接已保存，请发送标题（可选，不需要请回复 “无” 或发送 /skip_optional 跳过后面的所有可选项）")
    return STATE['TITLE']

@validate_state(STATE['TITLE'])
async def handle_title(update: Update, context: CallbackContext) -> int:
    """
    处理标题输入
    
    Args:
        update: Telegram 更新对象
        context: 回调上下文
        
    Returns:
        int: 下一个会话状态
    """
    logger.info(f"处理标题输入，user_id: {update.effective_user.id}")
    user_id = update.effective_user.id
    title = update.message.text.strip()
    title_to_store = "" if title.lower() == "无" else title[:100]
    try:
        async with get_db() as conn:
            c = await conn.cursor()
            await c.execute("UPDATE submissions SET title=? WHERE user_id=?",
                      (title_to_store, user_id))
        logger.info(f"标题保存成功，user_id: {user_id}")
    except Exception as e:
        logger.error(f"标题保存错误: {e}")
        await update.message.reply_text("❌ 标题保存失败，请稍后再试")
        return ConversationHandler.END
    await update.message.reply_text("✅ 标题已保存，请发送简介（可选，不需要请回复 “无” 或发送 /skip_optional 跳过后面的所有可选项）")
    return STATE['NOTE']

@validate_state(STATE['NOTE'])
async def handle_note(update: Update, context: CallbackContext) -> int:
    """
    处理简介输入
    
    Args:
        update: Telegram 更新对象
        context: 回调上下文
        
    Returns:
        int: 下一个会话状态
    """
    logger.info(f"处理简介输入，user_id: {update.effective_user.id}")
    user_id = update.effective_user.id
    note = update.message.text.strip()
    note_to_store = "" if note.lower() == "无" else note[:600]
    try:
        async with get_db() as conn:
            c = await conn.cursor()
            await c.execute("UPDATE submissions SET note=? WHERE user_id=?",
                      (note_to_store, user_id))
        logger.info(f"简介保存成功，user_id: {user_id}")
    except Exception as e:
        logger.error(f"简介保存错误: {e}")
        await update.message.reply_text("❌ 简介保存失败，请稍后再试")
        return ConversationHandler.END
    await update.message.reply_text("✅ 简介已保存，请问是否将所有媒体设为剧透（点击查看）？回复 “否” 或 “是”")
    return STATE['SPOILER']

@validate_state(STATE['SPOILER'])
async def handle_spoiler(update: Update, context: CallbackContext) -> int:
    """
    处理剧透设置
    
    Args:
        update: Telegram 更新对象
        context: 回调上下文
        
    Returns:
        int: 下一个会话状态或结束状态
    """
    logger.info(f"处理剧透选择，user_id: {update.effective_user.id}")
    user_id = update.effective_user.id
    answer = update.message.text.strip()
    # 用户回复"是"则设为剧透，否则为非剧透
    spoiler_flag = True if answer == "是" else False
    try:
        async with get_db() as conn:
            c = await conn.cursor()
            await c.execute("UPDATE submissions SET spoiler=? WHERE user_id=?",
                      ("true" if spoiler_flag else "false", user_id))
        logger.info(f"剧透选择保存成功，user_id: {user_id}，spoiler: {spoiler_flag}")
    except Exception as e:
        logger.error(f"剧透保存错误: {e}")
        await update.message.reply_text("❌ 剧透选择保存失败，请稍后再试")
        return ConversationHandler.END
    await update.message.reply_text("✅ 剧透选择已保存，正在发布投稿……")
    return await publish_submission(update, context)

# 跳过可选项的处理函数
async def skip_optional_link(update: Update, context: CallbackContext) -> int:
    """
    跳过链接及后续所有可选项（包括剧透设置）

    Args:
        update: Telegram 更新对象
        context: 回调上下文

    Returns:
        int: 下一个会话状态
    """
    logger.info(f"跳过链接、标题、简介、剧透，user_id: {update.effective_user.id}")
    user_id = update.effective_user.id
    try:
        async with get_db() as conn:
            c = await conn.cursor()
            # 链接、标题、简介、剧透均设置为默认值
            await c.execute("UPDATE submissions SET link=?, title=?, note=?, spoiler=? WHERE user_id=?",
                      ("", "", "", "false", user_id))
    except Exception as e:
        logger.error(f"/skip_optional 执行错误: {e}")
        await update.message.reply_text("❌ 跳过可选项失败，请稍后再试")
        return ConversationHandler.END
    await update.message.reply_text("✅ 已跳过所有可选项，正在发布投稿……")
    return await publish_submission(update, context)

async def skip_optional_title(update: Update, context: CallbackContext) -> int:
    """
    跳过标题及后续所有可选项（包括剧透设置）

    Args:
        update: Telegram 更新对象
        context: 回调上下文

    Returns:
        int: 下一个会话状态
    """
    logger.info(f"跳过标题、简介、剧透，user_id: {update.effective_user.id}")
    user_id = update.effective_user.id
    try:
        async with get_db() as conn:
            c = await conn.cursor()
            await c.execute("UPDATE submissions SET title=?, note=?, spoiler=? WHERE user_id=?",
                      ("", "", "false", user_id))
    except Exception as e:
        logger.error(f"/skip_optional 执行错误: {e}")
        await update.message.reply_text("❌ 跳过可选项失败，请稍后再试")
        return ConversationHandler.END
    await update.message.reply_text("✅ 已跳过所有可选项，正在发布投稿……")
    return await publish_submission(update, context)

async def skip_optional_note(update: Update, context: CallbackContext) -> int:
    """
    跳过简介及剧透设置

    Args:
        update: Telegram 更新对象
        context: 回调上下文

    Returns:
        int: 下一个会话状态
    """
    logger.info(f"跳过简介、剧透，user_id: {update.effective_user.id}")
    user_id = update.effective_user.id
    try:
        async with get_db() as conn:
            c = await conn.cursor()
            await c.execute("UPDATE submissions SET note=?, spoiler=? WHERE user_id=?",
                      ("", "false", user_id))
    except Exception as e:
        logger.error(f"/skip_optional 执行错误: {e}")
        await update.message.reply_text("❌ 跳过可选项失败，请稍后再试")
        return ConversationHandler.END
    await update.message.reply_text("✅ 已跳过所有可选项，正在发布投稿……")
    return await publish_submission(update, context)

async def prompt_media(update: Update, context: CallbackContext) -> int:
    """
    提示用户发送媒体文件
    
    Args:
        update: Telegram 更新对象
        context: 回调上下文
        
    Returns:
        int: 当前会话状态
    """
    await update.message.reply_text("请发送支持的媒体文件，或发送 /done 完成上传")
    return STATE['MEDIA']
