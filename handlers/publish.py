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
from utils.operation_service import claim_operation, get_operation, set_operation_state
from utils.publish_service import publication_scope

logger = logging.getLogger(__name__)


async def publish_to_channel(context: CallbackContext, post_data: dict):
    """
    兼容旧测试/旧导入路径的简化发布函数。
    """
    text = post_data.get("content") or post_data.get("text") or post_data.get("text_content") or ""
    sent_message = await context.bot.send_message(chat_id=CHANNEL_ID, text=text)

    db = get_db()
    if hasattr(db, "add_post"):
        await db.add_post({
            **post_data,
            "message_id": getattr(sent_message, "message_id", None),
        })

    return sent_message


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
    rich_content=None,
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
        from utils.content_normalizer import search_description
        from ui.rich_messages import TEMPLATE_VERSION

        source_json = rich_content.source().source_json() if rich_content else None
        post_id = None
        async with get_db() as conn:
            cursor = await conn.cursor()
            await cursor.execute("""
                INSERT INTO published_posts
                (message_id, user_id, username, title, tags, link, note,
                 content_type, file_ids, caption, filename, publish_time,
                 last_update, related_message_ids, text_content,
                 rating_subject_id, rating_avg, rating_votes, source_content_json, renderer, template_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    user_id=excluded.user_id, username=excluded.username, title=excluded.title,
                    tags=excluded.tags, link=excluded.link, note=excluded.note, caption=excluded.caption,
                    content_type=excluded.content_type, file_ids=excluded.file_ids, filename=excluded.filename,
                    related_message_ids=excluded.related_message_ids, text_content=excluded.text_content,
                    rating_subject_id=COALESCE(excluded.rating_subject_id, published_posts.rating_subject_id),
                    rating_avg=CASE WHEN excluded.rating_subject_id IS NOT NULL THEN excluded.rating_avg ELSE published_posts.rating_avg END,
                    rating_votes=CASE WHEN excluded.rating_subject_id IS NOT NULL THEN excluded.rating_votes ELSE published_posts.rating_votes END,
                    source_content_json=COALESCE(excluded.source_content_json, published_posts.source_content_json),
                    renderer=COALESCE(excluded.renderer, published_posts.renderer),
                    template_version=COALESCE(excluded.template_version, published_posts.template_version)
                WHERE excluded.renderer = 'rich'
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
                source_json,
                "rich" if rich_content else None,
                TEMPLATE_VERSION if rich_content else None,
            ))
            stored = await (await conn.execute(
                "SELECT source_content_json FROM published_posts WHERE message_id=?", (message_id,)
            )).fetchone()
            source_json = stored[0] if stored else source_json
            post_id = message_id  # message_id is the INTEGER PRIMARY KEY (rowid)
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
                description=search_description(source_json, "\n".join(v for v in (note, text_content) if v)),
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
            return False

        return True
    except Exception as e:
        logger.error(f"保存帖子信息到数据库失败: {e}")
        return False

async def repair_publication_record(operation_id):
    """Rebuild metadata and index from an attested receipt without sending anything."""
    operation = await get_operation(operation_id)
    if not operation or operation["state"] != "succeeded":
        return False
    ids = json.loads(operation["result_json"]).get("message_ids", [])
    if not ids:
        return False
    payload = json.loads(operation["payload_json"])
    data = {
        "title": "",
        "note": "",
        "tags": "",
        "link": "",
        "username": str(operation["owner_user_id"]),
        **payload.get("submission", payload),
    }
    media = json.loads(data.get("image_id") or "[]")
    docs = json.loads(data.get("document_id") or "[]")
    settings = payload.get("settings", data.get("_settings", {}))
    rich_content = None
    if json.loads(operation["result_json"]).get("renderer") == "rich":
        from ui.rich_messages import PostContent
        rich_content = PostContent.from_submission(
            data, docs, show_submitter=settings.get("show_submitter", True),
            prefix=payload.get("publication_prefix", ""),
        )
    return await save_published_post(
        operation["owner_user_id"],
        ids[0],
        data,
        media,
        docs,
        ids,
        data.get("text_content"),
        show_submitter=settings.get("show_submitter", True),
        rich_content=rich_content,
    )


async def show_submission_confirmation(update, context):
    from ui.keyboards import Keyboards

    user_id = update.effective_user.id
    async with get_db() as conn:
        row = await (
            await conn.execute("SELECT * FROM submissions WHERE user_id=?", (user_id,))
        ).fetchone()
    if not row:
        await context.bot.send_message(
            chat_id=user_id, text="投稿会话已结束，请重新发送 /submit。"
        )
        return ConversationHandler.END
    text = row["text_content"] or row["title"] or row["note"] or "媒体/文档投稿"
    await context.bot.send_message(
        chat_id=user_id,
        text="请确认本次投稿：\n\n" + text[:3000],
        reply_markup=Keyboards.submission_confirm(row["session_id"], row["revision"]),
    )
    return 13


async def publish_submission(update, context):
    user_id = update.effective_user.id
    async with get_db() as conn:
        row = await (
            await conn.execute("SELECT * FROM submissions WHERE user_id=?", (user_id,))
        ).fetchone()
    if not row:
        await context.bot.send_message(
            chat_id=user_id, text="投稿会话已结束，请重新发送 /submit。"
        )
        return ConversationHandler.END
    data = dict(row)
    query = getattr(update, "callback_query", None)
    if (
        query
        and query.data != f"submit_confirm_{data['session_id']}_{data['revision']}"
    ):
        await query.answer("该预览已失效，请重新确认当前投稿。", show_alert=True)
        return 13
    payload = {
        "submission": data,
        "settings": get_snapshot(context),
        "paid_ad": bool(context.user_data.get("paid_ad")),
        "_destination": CHANNEL_ID,
    }
    operation = await claim_operation(
        f"submit:{data['session_id']}:{data['revision']}:{CHANNEL_ID}", user_id, payload
    )
    if not operation:
        await context.bot.send_message(
            chat_id=user_id,
            text="本次投稿已有处理记录，请勿重复发布。管理员可查询处理结果。",
        )
        return ConversationHandler.END
    operation_id = operation["id"]
    context.user_data.pop("pending_review_id", None)
    try:
        if query:
            try:
                await query.answer("正在处理投稿")
                await query.edit_message_text("正在审核并发布，请稍候。")
            except Exception:
                pass
        async with publication_scope(operation_id):
            await _publish_submission_impl(
                update,
                context,
                data,
                operation_id,
                settings=payload["settings"],
                paid_ad=payload["paid_ad"],
            )
        current = await get_operation(operation_id)
        if current["state"] == "processing":
            await set_operation_state(operation_id, "failed")
    except Exception as exc:
        current = await get_operation(operation_id)
        if current["state"] == "processing":
            await set_operation_state(
                operation_id, "failed", error_code=type(exc).__name__
            )
        elif current["state"] == "sending":
            await set_operation_state(
                operation_id, "partial", error_code=type(exc).__name__
            )
        current = await get_operation(operation_id)
        message = (
            "内容已发送，后续处理暂未完成，请联系管理员。"
            if current["state"] == "succeeded"
            else (
                "发送结果尚待核对，请勿重新投稿。"
                if current["state"] in ("unknown", "partial")
                else "本次发布未完成，请联系管理员处理。"
            )
        )
        await context.bot.send_message(
            chat_id=user_id, text=f"{message}\n处理编号：{operation_id}"
        )
        logger.error("投稿操作 %s 中断: %s", operation_id, type(exc).__name__)
    finally:
        current = await get_operation(operation_id)
        if current["state"] == "failed":
            await refund_one_credit(user_id, operation_id=operation_id)
        if current["state"] in ("succeeded", "review_pending", "rejected"):
            async with get_db() as conn:
                await conn.execute(
                    "DELETE FROM submissions WHERE user_id=? AND session_id=? AND revision=?",
                    (user_id, data["session_id"], data["revision"]),
                )
    return ConversationHandler.END


async def _publish_submission_impl(update: Update, context: CallbackContext, data, operation_id, *, settings=None, paid_ad=None) -> int:
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
    reply_text = lambda text, **kwargs: context.bot.send_message(chat_id=user_id, text=text, **kwargs)
    snapshot = settings if settings is not None else get_snapshot(context)
    show_submitter = bool(snapshot.get("show_submitter", runtime_settings.bot_show_submitter()))
    notify_owner = bool(snapshot.get("notify_owner", runtime_settings.bot_notify_owner()))
    rating_enabled = bool(snapshot.get("rating_enabled", runtime_settings.rating_enabled()))
    try:
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
            await reply_text("❌ 未检测到任何上传文件或文本内容，请重新发送 /start")
            return ConversationHandler.END

        is_paid_ad = (bool(context.user_data.get("paid_ad")) if paid_ad is None else paid_ad) and runtime_settings.paid_ad_enabled()
        billing_id = context.user_data.get("billing_operation_id", operation_id)

        # === 审核流程：重复检测和 AI 审核 ===
        if not context.user_data.get("publication_review_completed") and (runtime_settings.duplicate_check_enabled() or runtime_settings.ai_review_enabled()):
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
            from types import SimpleNamespace
            review_update = SimpleNamespace(effective_user=update.effective_user, message=SimpleNamespace(reply_text=reply_text))
            is_approved, should_continue, review_message = await perform_review(
                review_update,
                context,
                submission_data,
                user_info,
                skip_ai_review=is_paid_ad,
            )

            if not should_continue:
                await set_operation_state(operation_id, "review_pending" if context.user_data.pop("pending_review_id", None) else "rejected")
                # 审核未通过，终止发布流程
                logger.info(f"投稿审核未通过: user_id={user_id}, message={review_message}")
                # 清理用户会话数据
                try:
                    async with get_db() as conn:
                        c = await conn.cursor()
                        await c.execute("DELETE FROM submissions WHERE user_id=? AND session_id=? AND revision=?", (user_id,data["session_id"],data["revision"]))
                        await conn.commit()
                    logger.info(f"已删除用户 {user_id} 的投稿记录")
                except Exception as e:
                    logger.error(f"清理投稿数据失败: {e}")
                return ConversationHandler.END

        async with get_db() as conn:
            current=await (await conn.execute("SELECT payload_json FROM bot_operations WHERE id=?",(operation_id,))).fetchone()
            saved_payload=json.loads(current[0])
            saved_payload["review_completed"]=True
            await conn.execute("UPDATE bot_operations SET payload_json=? WHERE id=?",(json.dumps(saved_payload,ensure_ascii=False),operation_id))

        # 安全处理spoiler字段，防止None值导致AttributeError
        spoiler_value = data["spoiler"] if "spoiler" in data.keys() and data["spoiler"] else "false"
        spoiler_flag = spoiler_value.lower() == "true"

        # 付费广告：预扣 1 次，失败则退回
        if is_paid_ad:
            reserved = await reserve_one_credit(user_id, operation_id=billing_id)
            if not reserved:
                context.user_data.pop("paid_ad", None)
                await reply_text(
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

        from handlers.rich_content import add_post_view_button, send_resource, source_for_publication
        rich_content = source_for_publication(
            context, snapshot, data, media_list, doc_list, spoiler_flag,
            show_submitter=show_submitter, prefix=ad_prefix,
        )
        if rich_content:
            async with get_db() as conn:
                await conn.execute(
                    "UPDATE bot_operations SET payload_json=json_set(payload_json, '$.publication_prefix', ?) WHERE id=?",
                    (ad_prefix, operation_id),
                )
            sent_message = await send_resource(context, rich_content)
            all_message_ids.append(sent_message.message_id)
            if getattr(sent_message, "renderer", None) != "rich":
                rich_content = None
        # 处理纯文本投稿
        elif text_content and not media_list and not doc_list:
            sent_message = await handle_text_publish(context, text_content, caption, spoiler_flag, prefix=ad_prefix)
            if sent_message:
                all_message_ids.append(sent_message.message_id)
        # 处理媒体文件
        elif media_list:
            sent_message, all_message_ids = await handle_media_publish(context, media_list, caption_for_media, spoiler_flag)
        
        # 处理文档文件
        if doc_list and (not sent_message or media_list):
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
                await refund_one_credit(user_id, operation_id=operation_id)
                context.user_data.pop("paid_ad", None)
            await reply_text("❌ 内容发送失败，请稍后再试")
            return ConversationHandler.END
            
        operation = await get_operation(operation_id)
        all_message_ids = json.loads(operation["result_json"]).get("message_ids", all_message_ids)
        await set_operation_state(operation_id, "succeeded")

        # 生成投稿链接
        if CHANNEL_ID.startswith('@'):
            channel_username = CHANNEL_ID.lstrip('@')
            submission_link = f"https://t.me/{channel_username}/{sent_message.message_id}"
        else:
            submission_link = "频道无公开链接"

        try:
            if is_paid_ad:
                context.user_data.pop("paid_ad", None)
                await reply_text(
                    f"✅ 广告已成功发布到频道（已扣减 1 次）\n点击以下链接查看：\n{submission_link}"
                )
            else:
                await reply_text(
                    f"🎉 投稿已成功发布到频道！\n点击以下链接查看投稿：\n{submission_link}"
                )
        except Exception as exc:
            logger.warning("发布回执通知未送达: %s", type(exc).__name__)

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
            rich_content=rich_content,
        )

        # 为频道消息附加评分键盘
        if rich_content:
            from ui.keyboards import Keyboards
            markup = Keyboards.rating_keyboard(rating_subject_id, rating_avg, rating_votes) if rating_subject_id is not None else None
            await add_post_view_button(context, sent_message.message_id, markup)
        elif rating_enabled and rating_subject_info and rating_subject_id is not None:
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
                        await reply_text(
                            "⚠️ 投稿已发布，但无法通知管理员。请直接联系管理员。"
                        )
            except Exception as e:
                logger.error(f"处理通知过程中发生错误: 错误类型: {type(e)}, 详细信息: {str(e)}")
                logger.error("异常追踪: ", exc_info=True)
        else:
            logger.info(f"不发送通知: notify_owner={notify_owner}, OWNER_ID={OWNER_ID}")
        
    except Exception:
        raise
    return ConversationHandler.END

async def handle_media_publish(context, media_list, caption, spoiler_flag):
    """Send deterministic groups; record every receipt and stop at the first failure."""
    from utils.publish_service import send_part

    all_ids = []
    first = None
    if caption and len(caption) > 850:
        first = await send_part(
            context.bot.send_message,
            chat_id=CHANNEL_ID,
            text=caption,
            parse_mode="HTML",
        )
        all_ids.append(first.message_id)
        caption = None
    # Audio and visual files cannot share a Telegram album; animations are sent singly.
    batches = []
    for item in media_list:
        kind, file_id = item.split(":", 1)
        if kind not in ("photo", "video", "animation", "audio"):
            raise ValueError("Unsupported media type")
        category = (
            "audio"
            if kind == "audio"
            else "animation" if kind == "animation" else "visual"
        )
        if (
            not batches
            or category == "animation"
            or batches[-1][0] != category
            or len(batches[-1][1]) >= 10
        ):
            batches.append((category, []))
        batches[-1][1].append((kind, file_id))
    for category, batch in batches:
        reply_id = first.message_id if first else None
        if len(batch) == 1:
            kind, file_id = batch[0]
            kwargs = {
                "chat_id": CHANNEL_ID,
                kind: file_id,
                "caption": caption if not all_ids else None,
                "parse_mode": "HTML" if caption and not all_ids else None,
                "reply_to_message_id": reply_id,
            }
            if kind in ("photo", "video", "animation"):
                kwargs["has_spoiler"] = spoiler_flag
            result = await send_part(getattr(context.bot, "send_" + kind), **kwargs)
            messages = [result]
        else:
            media = []
            for index, (kind, file_id) in enumerate(batch):
                cls = {
                    "photo": InputMediaPhoto,
                    "video": InputMediaVideo,
                    "audio": InputMediaAudio,
                }[kind]
                kwargs = {
                    "media": file_id,
                    "caption": caption if not all_ids and index == 0 else None,
                    "parse_mode": (
                        "HTML" if caption and not all_ids and index == 0 else None
                    ),
                }
                if kind in ("photo", "video"):
                    kwargs["has_spoiler"] = spoiler_flag
                media.append(cls(**kwargs))
            messages = await send_part(
                context.bot.send_media_group,
                chat_id=CHANNEL_ID,
                media=media,
                reply_to_message_id=reply_id,
            )
        first = first or messages[0]
        all_ids.extend(m.message_id for m in messages)
    return first, all_ids


async def handle_text_publish(context, text_content, caption, spoiler_flag, prefix=""):
    from utils.publish_service import send_part
    import html

    # User text is literal text, never arbitrary Telegram HTML.
    text = html.escape(text_content)
    if spoiler_flag:
        text = f"<tg-spoiler>{text}</tg-spoiler>"
    full_text = "\n\n".join(p for p in (prefix, text, caption) if p)
    return await send_part(
        context.bot.send_message, chat_id=CHANNEL_ID, text=full_text, parse_mode="HTML"
    )


async def handle_document_publish(
    context, doc_list, caption=None, reply_to_message_id=None
):
    from utils.publish_service import send_part

    first = None
    if caption and len(caption) > 850:
        first = await send_part(
            context.bot.send_message,
            chat_id=CHANNEL_ID,
            text=caption,
            parse_mode="HTML",
            reply_to_message_id=reply_to_message_id,
        )
        reply_to_message_id = first.message_id
        caption = None
    for start in range(0, len(doc_list), 10):
        batch = doc_list[start : start + 10]
        file_ids = [item.split(":", 2)[1] if ":" in item else item for item in batch]
        if len(batch) == 1:
            message = await send_part(
                context.bot.send_document,
                chat_id=CHANNEL_ID,
                document=file_ids[0],
                caption=caption if start == 0 else None,
                parse_mode="HTML" if caption and start == 0 else None,
                reply_to_message_id=reply_to_message_id,
            )
        else:
            group = [
                InputMediaDocument(
                    media=f,
                    caption=caption if start == 0 and i == len(batch) - 1 else None,
                    parse_mode=(
                        "HTML"
                        if caption and start == 0 and i == len(batch) - 1
                        else None
                    ),
                )
                for i, f in enumerate(file_ids)
            ]
            results = await send_part(
                context.bot.send_media_group,
                chat_id=CHANNEL_ID,
                media=group,
                reply_to_message_id=reply_to_message_id,
            )
            message = results[0]
        first = first or message
        reply_to_message_id = first.message_id
    return first
