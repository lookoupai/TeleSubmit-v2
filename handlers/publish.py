"""
投稿发布模块
"""
import json
import logging
import asyncio
from datetime import datetime
from telegram import (
    Update,
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaAnimation,
    InputMediaAudio,
    InputMediaDocument,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import ConversationHandler, CallbackContext

from config.settings import (
    CHANNEL_ID,
    NET_TIMEOUT,
    OWNER_ID,
)
from database.db_manager import get_db, cleanup_old_data
from utils.helper_functions import build_caption, safe_send
from utils.submit_settings import get_snapshot
from utils.search_engine import get_search_engine, PostDocument
from handlers.review_handlers import perform_review, save_fingerprint_after_publish
from utils.rating_service import get_rating_service
from utils.paid_ad_service import reserve_one_credit, refund_one_credit
from utils import runtime_settings

logger = logging.getLogger(__name__)

async def save_published_post(
    user_id,
    message_id,
    data,
    media_list,
    doc_list,
    all_message_ids=None,
    text_content=None,
    rating_subject_id=None,
    rating_avg=None,
    rating_votes=None,
    show_submitter=None,
):
    """
    保存已发布的帖子信息到数据库和搜索索引

    Args:
        user_id: 用户ID
        message_id: 频道主消息ID
        data: 投稿数据（sqlite3.Row对象）
        media_list: 媒体列表
        doc_list: 文档列表
        all_message_ids: 所有相关消息ID列表（用于多组媒体的热度统计）
        text_content: 纯文本投稿内容
    """
    try:
        # 确定内容类型
        if text_content and not media_list and not doc_list:
            content_type = 'text'
        elif media_list and doc_list:
            content_type = 'mixed'
        elif media_list:
            content_type = 'media'
        else:
            content_type = 'document'

        # 获取文件ID列表
        file_ids = json.dumps(media_list if media_list else (doc_list if doc_list else []))
        
        # 提取标签（从tags字段）- 兼容 sqlite3.Row 对象
        tags = data['tags'] if 'tags' in data.keys() else ''
        
        # 构建说明
        caption = build_caption(data, show_submitter=show_submitter)
        
        # 提取信息 - 兼容 sqlite3.Row 对象
        title = data['title'] if data['title'] else ''
        note = data['note'] if data['note'] else ''
        link = data['link'] if data['link'] else ''
        username = data['username'] if 'username' in data.keys() and data['username'] else f'user{user_id}'
        publish_time = datetime.now()
        
        # 提取文件名（从文档列表中）
        filename = ''
        if doc_list:
            filenames = []
            for doc_item in doc_list:
                # 新格式：document:file_id:filename
                parts = doc_item.split(':', 2)
                if len(parts) >= 3:
                    filenames.append(parts[2])
                elif len(parts) == 2:
                    # 兼容旧格式 document:file_id
                    filenames.append('未知文件')
            filename = ' | '.join(filenames) if filenames else ''
        
        # 处理相关消息ID（用于多组媒体热度统计）
        related_ids_json = None
        if all_message_ids and len(all_message_ids) > 1:
            # 只保存除主消息外的其他消息ID
            related_ids = [mid for mid in all_message_ids if mid != message_id]
            if related_ids:
                related_ids_json = json.dumps(related_ids)
                logger.info(f"记录{len(related_ids)}个关联消息ID: {related_ids}")
        
        # 评分快照（允许为空，避免破坏现有逻辑）
        rating_subject_id = rating_subject_id if rating_subject_id is not None else None
        rating_avg = float(rating_avg) if rating_avg is not None else 0.0
        rating_votes = int(rating_votes) if rating_votes is not None else 0

        # 保存到数据库并获取 post_id
        post_id = None
        async with get_db() as conn:
            cursor = await conn.cursor()
            await cursor.execute("""
                INSERT INTO published_posts
                (message_id, user_id, username, title, tags, link, note,
                 content_type, file_ids, caption, filename, publish_time,
                 last_update, related_message_ids, text_content,
                 rating_subject_id, rating_avg, rating_votes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                message_id,
                user_id,
                username,
                title,
                tags,
                link,
                note,
                content_type,
                file_ids,
                caption,
                filename,
                publish_time.timestamp(),
                publish_time.timestamp(),
                related_ids_json,
                text_content,
                rating_subject_id,
                rating_avg,
                rating_votes,
            ))
            post_id = cursor.lastrowid  # 获取插入的行ID
            await conn.commit()
            logger.info(f"已保存帖子 {message_id} (post_id: {post_id}) 到published_posts表（内容类型: {content_type}）")
        
        # 添加到搜索索引
        try:
            search_engine = get_search_engine()
            
            # 构建搜索文档
            # 将 note 作为 description
            post_doc = PostDocument(
                message_id=message_id,
                post_id=post_id,  # 传入数据库ID
                title=title,
                description=note,  # 使用note作为描述
                tags=tags,
                filename=filename,  # 文件名
                link=link,
                user_id=user_id,
                username=username,
                publish_time=publish_time,
                views=0,
                heat_score=0
            )
            
            # 添加到索引
            search_engine.add_post(post_doc)
            logger.info(f"已添加帖子 {message_id} (post_id: {post_id}) 到搜索索引（文件名: {filename}）")
            
        except Exception as e:
            logger.error(f"添加到搜索索引失败: {e}", exc_info=True)
            # 继续执行，不影响发布流程
            
    except Exception as e:
        logger.error(f"保存帖子信息到数据库失败: {e}")

async def publish_submission(update: Update, context: CallbackContext) -> int:
    """
    发布投稿到频道
    
    处理逻辑:
    1. 仅媒体模式: 将媒体发送到频道
    2. 仅文档模式或文档优先模式: 
       - 若同时有媒体和文档，则以媒体为主贴，文档组合作为回复
       - 若仅有文档，则以文档进行组合发送（说明文本放在最后一条）
    
    Args:
        update: Telegram 更新对象
        context: 回调上下文
        
    Returns:
        int: 会话结束状态
    """
    user_id = update.effective_user.id
    snapshot = get_snapshot(context)
    show_submitter = bool(snapshot.get("show_submitter", runtime_settings.bot_show_submitter()))
    notify_owner = bool(snapshot.get("notify_owner", runtime_settings.bot_notify_owner()))
    rating_enabled = bool(snapshot.get("rating_enabled", runtime_settings.rating_enabled()))
    try:
        async with get_db() as conn:
            c = await conn.cursor()
            await c.execute("SELECT * FROM submissions WHERE user_id=?", (user_id,))
            data = await c.fetchone()
        
        if not data:
            await update.message.reply_text("❌ 数据异常，请重新发送 /start")
            return ConversationHandler.END

        caption = build_caption(data, show_submitter=show_submitter)
        
        # 解析媒体和文档数据，增强型错误处理
        media_list = []
        doc_list = []
        
        try:
            if data["image_id"]:
                media_list = json.loads(data["image_id"])
        except (json.JSONDecodeError, TypeError):
            logger.warning(f"解析媒体数据失败，user_id: {user_id}")
            media_list = []
            
        try:
            if data["document_id"]:
                doc_list = json.loads(data["document_id"])
        except (json.JSONDecodeError, TypeError):
            logger.warning(f"解析文档数据失败，user_id: {user_id}")
            doc_list = []
        
        # 获取纯文本内容
        text_content = data["text_content"] if "text_content" in data.keys() and data["text_content"] else None

        if not media_list and not doc_list and not text_content:
            await update.message.reply_text("❌ 未检测到任何上传文件或文本内容，请重新发送 /start")
            return ConversationHandler.END

        is_paid_ad = bool(context.user_data.get("paid_ad")) and runtime_settings.paid_ad_enabled()

        # === 审核流程：重复检测和 AI 审核 ===
        if runtime_settings.duplicate_check_enabled() or runtime_settings.ai_review_enabled():
            # 构建投稿数据用于审核（同时保存完整发布所需字段，供人工审核通过后发布）
            submission_data = {
                'text_content': text_content,
                'title': data['title'] if data['title'] else '',
                'note': data['note'] if data['note'] else '',
                'tags': data['tags'] if 'tags' in data.keys() else '',
                'link': data['link'] if data['link'] else '',
                'image_id': data['image_id'] if 'image_id' in data.keys() else '[]',
                'document_id': data['document_id'] if 'document_id' in data.keys() else '[]',
                'spoiler': data['spoiler'] if 'spoiler' in data.keys() and data['spoiler'] else 'false',
                'mode': data['mode'] if 'mode' in data.keys() else '',
            }

            # 获取用户信息
            user = update.effective_user
            username = data['username'] if 'username' in data.keys() and data['username'] else ''

            # 尝试获取用户 bio（用于重复检测）
            user_bio = ''
            try:
                chat = await context.bot.get_chat(user_id)
                user_bio = chat.bio or ''
            except Exception as e:
                logger.debug(f"获取用户 bio 失败: {e}")

            user_info = {
                'user_id': user_id,
                'username': username or user.username or '',
                'bio': user_bio
            }

            # 执行审核（包含重复检测和 AI 审核）
            is_approved, should_continue, review_message = await perform_review(
                update,
                context,
                submission_data,
                user_info,
                skip_ai_review=is_paid_ad,
            )

            if not should_continue:
                # 审核未通过，终止发布流程
                logger.info(f"投稿审核未通过: user_id={user_id}, message={review_message}")
                # 清理用户会话数据
                try:
                    async with get_db() as conn:
                        c = await conn.cursor()
                        await c.execute("DELETE FROM submissions WHERE user_id=?", (user_id,))
                        await conn.commit()
                    logger.info(f"已删除用户 {user_id} 的投稿记录")
                except Exception as e:
                    logger.error(f"清理投稿数据失败: {e}")
                return ConversationHandler.END

        # 安全处理spoiler字段，防止None值导致AttributeError
        spoiler_value = data["spoiler"] if "spoiler" in data.keys() and data["spoiler"] else "false"
        spoiler_flag = spoiler_value.lower() == "true"

        # 付费广告：预扣 1 次，失败则退回
        if is_paid_ad:
            reserved = await reserve_one_credit(user_id)
            if not reserved:
                context.user_data.pop("paid_ad", None)
                await update.message.reply_text(
                    "❌ 广告发布次数不足，请先购买。\n\n"
                    "点击“购买广告次数”选择套餐，或使用 /ad_balance 查看余额。",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("购买广告次数", callback_data="paid_ad_buy_menu")]]),
                )
                return ConversationHandler.END

        sent_message = None
        all_message_ids = []  # 用于记录所有发送的消息ID

        # 评分实体（可选）
        rating_subject_info = None
        if rating_enabled:
            try:
                rating_service = get_rating_service()
                rating_subject_info = await rating_service.get_or_create_subject_from_submission(
                    submission_row=data,
                    user_id=user_id,
                    source_chat_id=None,
                )
            except Exception as e:
                logger.error(f"解析评分实体时出错: {e}", exc_info=True)

        # 广告前缀：媒体/文档/混合放在 caption 顶部；纯文本放在消息顶部
        ad_prefix = (runtime_settings.paid_ad_publish_prefix() or "📢 广告").strip() if is_paid_ad else ""
        caption_for_media = caption
        if is_paid_ad and not (text_content and not media_list and not doc_list):
            caption_for_media = f"{ad_prefix}\n\n{caption}" if caption else ad_prefix

        # 处理纯文本投稿
        if text_content and not media_list and not doc_list:
            sent_message = await handle_text_publish(context, text_content, caption, spoiler_flag, prefix=ad_prefix)
            if sent_message:
                all_message_ids.append(sent_message.message_id)
        # 处理媒体文件
        elif media_list:
            sent_message, all_message_ids = await handle_media_publish(context, media_list, caption_for_media, spoiler_flag)
        
        # 处理文档文件
        if doc_list:
            if sent_message:
                # 如果已经发送了媒体，则文档作为回复
                doc_msg = await handle_document_publish(
                    context, 
                    doc_list, 
                    None,  # 不需要重复发送说明，回复到主贴即可
                    sent_message.message_id
                )
                if doc_msg:
                    all_message_ids.append(doc_msg.message_id)
            else:
                # 如果只有文档，直接发送
                sent_message = await handle_document_publish(context, doc_list, caption_for_media)
                if sent_message:
                    all_message_ids.append(sent_message.message_id)
        
        # 处理结果
        if not sent_message:
            if is_paid_ad:
                await refund_one_credit(user_id)
                context.user_data.pop("paid_ad", None)
            await update.message.reply_text("❌ 内容发送失败，请稍后再试")
            return ConversationHandler.END
            
        # 生成投稿链接
        if CHANNEL_ID.startswith('@'):
            channel_username = CHANNEL_ID.lstrip('@')
            submission_link = f"https://t.me/{channel_username}/{sent_message.message_id}"
        else:
            submission_link = "频道无公开链接"

        if is_paid_ad:
            context.user_data.pop("paid_ad", None)
            await update.message.reply_text(
                f"✅ 广告已成功发布到频道（已扣减 1 次）\n点击以下链接查看：\n{submission_link}"
            )
        else:
            await update.message.reply_text(
                f"🎉 投稿已成功发布到频道！\n点击以下链接查看投稿：\n{submission_link}"
            )
        
        # 保存已发布的帖子信息到数据库（用于热度统计和搜索）
        rating_subject_id = None
        rating_avg = None
        rating_votes = None
        if rating_subject_info:
            rating_subject_id = rating_subject_info.get("subject_id")
            rating_avg = rating_subject_info.get("avg_score", 0.0)
            rating_votes = rating_subject_info.get("vote_count", 0)

        await save_published_post(
            user_id,
            sent_message.message_id,
            data,
            media_list,
            doc_list,
            all_message_ids,
            text_content,
            rating_subject_id=rating_subject_id,
            rating_avg=rating_avg,
            rating_votes=rating_votes,
            show_submitter=show_submitter,
        )

        # 为频道消息附加评分键盘
        if rating_enabled and rating_subject_info and rating_subject_id is not None:
            try:
                rating_service = get_rating_service()
                await rating_service.attach_rating_keyboard(
                    context=context,
                    message_id=sent_message.message_id,
                    subject_id=rating_subject_id,
                    avg_score=rating_avg or 0.0,
                    vote_count=rating_votes or 0,
                )
            except Exception as e:
                logger.error(f"为消息附加评分键盘失败: {e}", exc_info=True)

        # 保存投稿指纹（用于重复检测）
        if runtime_settings.duplicate_check_enabled():
            try:
                # 构建投稿数据
                submission_data = {
                    'text_content': text_content,
                    'title': data['title'] if data['title'] else '',
                    'note': data['note'] if data['note'] else '',
                    'tags': data['tags'] if 'tags' in data.keys() else '',
                    'link': data['link'] if data['link'] else ''
                }

                # 获取用户名
                user = update.effective_user
                username = data['username'] if 'username' in data.keys() and data['username'] else ''

                # 尝试获取用户 bio
                user_bio = ''
                try:
                    chat = await context.bot.get_chat(user_id)
                    user_bio = chat.bio or ''
                except Exception:
                    pass

                await save_fingerprint_after_publish(
                    user_id=user_id,
                    username=username or user.username or '',
                    submission_data=submission_data,
                    user_bio=user_bio,
                    submission_id=sent_message.message_id
                )
                logger.info(f"已保存投稿指纹: user_id={user_id}, message_id={sent_message.message_id}")
            except Exception as e:
                logger.error(f"保存投稿指纹失败: {e}")

        # 向所有者发送投稿通知
        if notify_owner and OWNER_ID:
            # 记录详细的调试信息
            logger.info(f"准备发送通知: notify_owner={notify_owner}, OWNER_ID={OWNER_ID}, 类型={type(OWNER_ID)}")
            
            # 获取用户名信息
            username = None
            try:
                username = data["username"] if "username" in data else f"user{user_id}"
            except (KeyError, TypeError):
                username = f"user{user_id}"
                
            # 获取用户名信息，优先使用真实用户名
            user = update.effective_user
            real_username = user.username or username
            
            # 安全处理可能缺失的数据字段
            try:
                mode = data["mode"] if "mode" in data else "未知"
                media_count = len(json.loads(data["image_id"])) if "image_id" in data and data["image_id"] else 0
                doc_count = len(json.loads(data["document_id"])) if "document_id" in data and data["document_id"] else 0
                tag_text = data["tag"] if "tag" in data else "无"
                title_text = data["title"] if "title" in data else "无"
                spoiler_text = "是" if "spoiler" in data and data["spoiler"] == "true" else "否"
            except Exception as e:
                logger.error(f"数据处理错误: {e}")
                # 设置默认值
                mode = "未知"
                media_count = 0
                doc_count = 0
                tag_text = "无"
                title_text = "无"
                spoiler_text = "否"
            
            # 构建纯文本通知消息（不使用任何Markdown，确保最大兼容性）
            notification_text = (
                f"📨 新投稿通知\n\n"
                f"👤 投稿人信息:\n"
                f"  • ID: {user_id}\n"
                f"  • 用户名: {('@' + real_username) if user.username else real_username}\n"
                f"  • 昵称: {user.first_name}{f' {user.last_name}' if user.last_name else ''}\n\n"
                
                f"🔗 查看投稿: {submission_link}\n\n"
                
                f"⚙️ 管理操作:\n"
                f"封禁此用户: /blacklist_add {user_id} 违规内容\n"
                f"查看黑名单: /blacklist_list"
            )
            
            try:
                # OWNER_ID 已经在配置中转换为整数类型，直接使用
                logger.info(f"准备发送通知到所有者: {OWNER_ID}")
                
                # 记录通知消息内容
                logger.info(f"通知消息长度: {len(notification_text)}, 使用纯文本格式")
                
                # 简化尝试逻辑 - 直接使用纯文本，不尝试任何格式化
                try:
                    message = await context.bot.send_message(
                        chat_id=OWNER_ID,
                        text=notification_text
                    )
                    logger.info(f"通知发送成功！消息ID: {message.message_id}")
                except Exception as e:
                    logger.error(f"发送通知失败: {e}")
                    # 尝试使用更简化的消息
                    try:
                        simple_msg = f"📨 新投稿通知 - 用户 {real_username} (ID: {user_id}) 发布了新投稿\n链接: {submission_link}\n\n封禁命令: /blacklist_add {user_id} 违规内容"
                        await context.bot.send_message(
                            chat_id=OWNER_ID,
                            text=simple_msg
                        )
                        logger.info("使用简化消息成功发送通知")
                    except Exception as e2:
                        logger.error(f"发送简化通知也失败: {e2}")
                        # 通知用户有问题
                        await update.message.reply_text(
                            "⚠️ 投稿已发布，但无法通知管理员。请直接联系管理员。"
                        )
            except Exception as e:
                logger.error(f"处理通知过程中发生错误: 错误类型: {type(e)}, 详细信息: {str(e)}")
                logger.error("异常追踪: ", exc_info=True)
        else:
            logger.info(f"不发送通知: notify_owner={notify_owner}, OWNER_ID={OWNER_ID}")
        
    except Exception as e:
        logger.error(f"发布投稿失败: {e}")
        await update.message.reply_text(f"❌ 发布失败，请联系管理员。错误信息：{str(e)}")
    finally:
        # 清理用户会话数据
        try:
            async with get_db() as conn:
                c = await conn.cursor()
                await c.execute("DELETE FROM submissions WHERE user_id=?", (user_id,))
            logger.info(f"已删除用户 {user_id} 的投稿记录")
        except Exception as e:
            logger.error(f"删除数据错误: {e}")
        
        # 清理过期数据
        await cleanup_old_data()
    
    return ConversationHandler.END

async def handle_media_publish(context, media_list, caption, spoiler_flag):
    """
    处理媒体发布
    
    Args:
        context: 回调上下文
        media_list: 媒体列表
        caption: 说明文本
        spoiler_flag: 是否剧透标志
        
    Returns:
        tuple: (主消息对象, 所有消息ID列表) 或 (None, [])
    """
    # 检查caption长度，如果过长先单独发送
    caption_message = None
    
    # 强制检查caption长度，保证媒体组发送的可靠性
    # 不管SHOW_SUBMITTER如何设置，当caption超过850字符时都单独发送
    # 使用较小的阈值（850而不是1000）来确保足够的安全边际
    if caption and len(caption) > 850:
        logger.info(f"Caption过长 ({len(caption)} 字符)，单独发送caption")
        try:
            caption_message = await safe_send(
                context.bot.send_message,
                chat_id=CHANNEL_ID,
                text=caption,
                parse_mode='HTML'
            )
            # 媒体组将不再包含caption
            caption = None
        except Exception as e:
            logger.error(f"发送长caption失败: {e}")
            # 继续尝试发送媒体，但不带caption

    # 单个媒体处理
    if len(media_list) == 1:
        typ, file_id = media_list[0].split(":", 1)
        try:
            # 如果已经单独发送了caption，则不再添加到媒体
            media_caption = None if caption_message else caption
            
            if typ == "photo":
                sent_message = await safe_send(
                    context.bot.send_photo,
                    chat_id=CHANNEL_ID,
                    photo=file_id,
                    caption=media_caption,
                    parse_mode='HTML' if media_caption else None,
                    has_spoiler=spoiler_flag,
                    reply_to_message_id=caption_message.message_id if caption_message else None
                )
            elif typ == "video":
                sent_message = await safe_send(
                    context.bot.send_video,
                    chat_id=CHANNEL_ID,
                    video=file_id,
                    caption=media_caption,
                    parse_mode='HTML' if media_caption else None,
                    has_spoiler=spoiler_flag,
                    reply_to_message_id=caption_message.message_id if caption_message else None
                )
            elif typ == "animation":
                sent_message = await safe_send(
                    context.bot.send_animation,
                    chat_id=CHANNEL_ID,
                    animation=file_id,
                    caption=media_caption,
                    parse_mode='HTML' if media_caption else None,
                    has_spoiler=spoiler_flag,
                    reply_to_message_id=caption_message.message_id if caption_message else None
                )
            elif typ == "audio":
                sent_message = await safe_send(
                    context.bot.send_audio,
                    chat_id=CHANNEL_ID,
                    audio=file_id,
                    caption=media_caption,
                    parse_mode='HTML' if media_caption else None,
                    reply_to_message_id=caption_message.message_id if caption_message else None
                )
            
            # 收集所有消息ID
            main_msg = caption_message or sent_message
            all_ids = []
            if caption_message:
                all_ids.append(caption_message.message_id)
            if sent_message:
                all_ids.append(sent_message.message_id)
            return (main_msg, all_ids)
        except Exception as e:
            logger.error(f"发送单条媒体失败: {e}")
            if caption_message:
                return (caption_message, [caption_message.message_id])
            return (None, [])
    
    # 多个媒体处理 - 将媒体分组，每组最多10个
    else:
        try:
            all_sent_messages = []
            success_groups = 0
            total_groups = (len(media_list) + 9) // 10  # 向上取整计算总组数
            first_message = caption_message  # 如果单独发送了caption，用它作为第一条消息
            
            # 将媒体列表分成每组最多10个项目
            for chunk_index in range(0, len(media_list), 10):
                media_chunk = media_list[chunk_index:chunk_index + 10]
                media_group = []
                
                group_number = chunk_index // 10 + 1
                logger.info(f"处理第{group_number}组媒体，共{len(media_chunk)}个项目 (总共{total_groups}组)")
                
                for i, m in enumerate(media_chunk):
                    typ, file_id = m.split(":", 1)
                    # 只在第一组的第一个媒体添加说明（如果caption不为None且没有单独发送）
                    # 强制设置简短的caption，即使SHOW_SUBMITTER=True也能可靠发送
                    use_caption = caption if (chunk_index == 0 and i == 0 and caption is not None and not caption_message) else None
                    use_parse_mode = 'HTML' if use_caption else None
                    
                    if typ == "photo":
                        media_group.append(InputMediaPhoto(
                            media=file_id,
                            caption=use_caption,
                            parse_mode=use_parse_mode,
                            has_spoiler=spoiler_flag
                        ))
                    elif typ == "video":
                        media_group.append(InputMediaVideo(
                            media=file_id,
                            caption=use_caption,
                            parse_mode=use_parse_mode,
                            has_spoiler=spoiler_flag
                        ))
                    elif typ == "animation":
                        media_group.append(InputMediaAnimation(
                            media=file_id,
                            caption=use_caption,
                            parse_mode=use_parse_mode,
                            has_spoiler=spoiler_flag
                        ))
                    elif typ == "audio":
                        media_group.append(InputMediaAudio(
                            media=file_id,
                            caption=use_caption,
                            parse_mode=use_parse_mode
                        ))
                
                # 发送当前组，增加超时参数
                extended_timeout = 60  # 更长的超时时间，避免误判为超时
                if first_message is None:
                    logger.info(f"发送第{group_number}组媒体（首组），{len(media_group)}个媒体项目")
                    # 第一组直接发送
                    try:
                        sent_messages = await asyncio.wait_for(
                            context.bot.send_media_group(
                                chat_id=CHANNEL_ID,
                                media=media_group
                            ),
                            timeout=extended_timeout
                        )
                        
                        if sent_messages and len(sent_messages) > 0:
                            all_sent_messages.extend(sent_messages)
                            first_message = sent_messages[0]  # 保存第一条消息，用于回复
                            logger.info(f"第{group_number}组媒体发送成功，message_id={first_message.message_id}")
                            success_groups += 1
                        else:
                            logger.error(f"第{group_number}组媒体发送返回空结果")
                    except asyncio.TimeoutError:
                        logger.warning(f"第{group_number}组媒体发送超时，但可能已成功发送")
                        # 即使超时，尝试继续后续组的发送
                        # 等待3秒，让Telegram服务器有时间处理
                        await asyncio.sleep(3)
                    except Exception as e:
                        logger.error(f"第{group_number}组媒体发送失败: {e}")
                        
                        # 如果是网络相关错误，休眠更长时间后继续
                        if any(keyword in str(e).lower() for keyword in ["network", "connection", "timeout"]):
                            await asyncio.sleep(5)
                else:
                    logger.info(f"发送第{group_number}组媒体（回复组），{len(media_group)}个媒体项目，回复到message_id={first_message.message_id}")
                    # 后续组作为回复发送到第一条消息
                    try:
                        sent_messages = await asyncio.wait_for(
                            context.bot.send_media_group(
                                chat_id=CHANNEL_ID,
                                media=media_group,
                                reply_to_message_id=first_message.message_id
                            ),
                            timeout=extended_timeout
                        )
                        
                        if sent_messages and len(sent_messages) > 0:
                            all_sent_messages.extend(sent_messages)
                            logger.info(f"第{group_number}组媒体发送成功，第一条message_id={sent_messages[0].message_id}")
                            success_groups += 1
                        else:
                            logger.error(f"第{group_number}组媒体发送返回空结果")
                    except asyncio.TimeoutError:
                        logger.warning(f"第{group_number}组媒体发送超时，但可能已成功发送")
                        # 即使超时，尝试继续后续组的发送
                        # 等待3秒，让Telegram服务器有时间处理
                        await asyncio.sleep(3)
                    except Exception as e:
                        logger.error(f"第{group_number}组媒体发送失败: {e}")
                        
                        # 如果是网络相关错误，休眠更长时间后继续
                        if any(keyword in str(e).lower() for keyword in ["network", "connection", "timeout"]):
                            await asyncio.sleep(5)
                
                # 添加更长的延迟，避免API限制
                # 每组之间等待2秒，给Telegram API更多处理时间
                await asyncio.sleep(2)
            
            # 计算实际处理的媒体数量并记录结果
            total_media_estimate = success_groups * 10
            if success_groups < total_groups and len(all_sent_messages) == 0:
                logger.warning(f"媒体发送部分超时，预计已发送约{total_media_estimate}个媒体项目（可能不准确）")
            else:
                logger.info(f"所有媒体发送完成，{success_groups}/{total_groups}组成功，共{len(all_sent_messages)}个媒体项目成功记录")
            
            # 收集所有消息ID
            all_message_ids = []
            if caption_message:
                all_message_ids.append(caption_message.message_id)
            all_message_ids.extend([msg.message_id for msg in all_sent_messages])
            
            # 返回主消息和所有消息ID
            main_msg = first_message if first_message else (all_sent_messages[0] if all_sent_messages else None)
            return (main_msg, all_message_ids)
        except Exception as e:
            logger.error(f"发送媒体组失败: {e}")
            if caption_message:
                return (caption_message, [caption_message.message_id])
            return (None, [])

async def handle_text_publish(context, text_content, caption, spoiler_flag, prefix: str = ""):
    """
    处理纯文本投稿发布

    Args:
        context: 回调上下文
        text_content: 纯文本投稿内容
        caption: 额外说明（标签、链接等）
        spoiler_flag: 是否标记为剧透

    Returns:
        发送的消息对象或None
    """
    try:
        # 组合完整的消息内容
        # 纯文本模式：text_content 是正文，caption 包含标签等元信息
        if caption:
            # 在剧透模式下，将正文用剧透标签包裹
            if spoiler_flag:
                full_text = f"<tg-spoiler>{text_content}</tg-spoiler>\n\n{caption}"
            else:
                full_text = f"{text_content}\n\n{caption}"
        else:
            if spoiler_flag:
                full_text = f"<tg-spoiler>{text_content}</tg-spoiler>"
            else:
                full_text = text_content

        if prefix:
            full_text = f"{prefix}\n\n{full_text}"

        sent_message = await safe_send(
            context.bot.send_message,
            chat_id=CHANNEL_ID,
            text=full_text,
            parse_mode='HTML'
        )
        logger.info(f"纯文本投稿发送成功，message_id={sent_message.message_id}")
        return sent_message
    except Exception as e:
        logger.error(f"发送纯文本投稿失败: {e}")
        return None


async def handle_document_publish(context, doc_list, caption=None, reply_to_message_id=None):
    """
    处理文档发布
    
    Args:
        context: 回调上下文
        doc_list: 文档列表
        caption: 说明文本，如果为None则不添加说明
        reply_to_message_id: 回复的消息ID，如果为None则创建新消息
        
    Returns:
        发送的消息对象或None
    """
    if len(doc_list) == 1 and caption is not None:
        # 单个文档处理
        parts = doc_list[0].split(":", 2)
        file_id = parts[1] if len(parts) >= 2 else parts[0]
        try:
            return await safe_send(
                context.bot.send_document,
                chat_id=CHANNEL_ID,
                document=file_id,
                caption=caption,
                parse_mode='HTML',
                reply_to_message_id=reply_to_message_id
            )
        except Exception as e:
            logger.error(f"发送单个文档失败: {e}")
            return None
    else:
        # 多个文档处理，使用文档组
        try:
            doc_media_group = []
            for i, doc_item in enumerate(doc_list):
                # 新格式：document:file_id:filename 或 旧格式：document:file_id
                parts = doc_item.split(":", 2)
                file_id = parts[1] if len(parts) >= 2 else parts[0]
                # 只在最后一个文档添加说明，且caption不为None
                caption_to_use = caption if (i == len(doc_list) - 1 and caption is not None) else None
                doc_media_group.append(InputMediaDocument(
                    media=file_id,
                    caption=caption_to_use,
                    parse_mode='HTML' if caption_to_use else None
                ))
            
            sent_docs = await safe_send(
                context.bot.send_media_group,
                chat_id=CHANNEL_ID,
                media=doc_media_group,
                reply_to_message_id=reply_to_message_id
            )
            
            if sent_docs and len(sent_docs) > 0:
                return sent_docs[0]
            return None
        except Exception as e:
            logger.error(f"发送文档组失败: {e}")
            return None
