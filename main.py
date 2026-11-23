"""
Telegram 投稿机器人主程序
支持媒体和文档投稿
"""
import sys
import json
import signal
import asyncio
import platform
import logging
import os
from datetime import datetime, time as datetime_time
from telegram import Update, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    BaseHandler,
    filters,
    ConversationHandler,
    CallbackContext,
    ApplicationHandlerStop,
    CallbackQueryHandler
)
from dotenv import load_dotenv

# 配置相关导入
from config.settings import (
    TOKEN, TIMEOUT, BOT_MODE, MODE_MEDIA, MODE_DOCUMENT, MODE_MIXED, MODE_TEXT, MODE_ALL,
    RUN_MODE, WEBHOOK_URL, WEBHOOK_PORT, WEBHOOK_PATH, WEBHOOK_SECRET_TOKEN,
    CHANNEL_ID, AI_REVIEW_ENABLED, DUPLICATE_CHECK_ENABLED,
    PAID_AD_ENABLED, SLOT_AD_ENABLED, UPAY_NOTIFY_PATH,
    ADMIN_WEB_ENABLED, ADMIN_WEB_PATH
)
from models.state import STATE

# 数据库相关导入
from database.db_manager import init_db, cleanup_old_data, get_db
from utils.database import (
    get_user_state, 
    delete_user_state, 
    is_blacklisted, 
    initialize_database
)

# 工具函数导入
from utils.logging_config import setup_logging, cleanup_old_logs
from utils.helper_functions import CONFIG
from utils.telegram_capabilities import allowed_updates as telegram_allowed_updates
from utils.telegram_api import TelegramAPI

# 处理程序导入 - 按功能分组
# 基础命令
from handlers import (
    start, help_command, cancel, settings, settings_callback,
    handle_text, collect_extra, handle_image, done_image,
    handle_document, done_document, switch_to_doc_mode
)

# 黑名单管理
from utils.blacklist import manage_blacklist, init_blacklist, blacklist_filter
from handlers.command_handlers import blacklist_add, blacklist_remove, blacklist_list, catch_all, debug, handle_menu_shortcuts

# 投稿处理
from handlers.publish import publish_submission

# 不同投稿模式支持
from handlers.mode_selection import submit, start, select_mode
from handlers.document_handlers import handle_doc, done_doc, prompt_doc
from handlers.media_handlers import handle_media, done_media, skip_media, prompt_media
from handlers.submit_handlers import (
    handle_tag, 
    handle_link, 
    handle_title, 
    handle_note, 
    handle_spoiler,
    skip_optional_link,
    skip_optional_title,
    skip_optional_note
)

# 纯文本投稿处理
from handlers.text_handlers import handle_text_content

# 审核流程处理
from handlers.review_handlers import handle_review_callback

# 付费广告
from handlers.paid_ad_handlers import ad as paid_ad, ad_balance as paid_ad_balance
from handlers.paid_ad_notify import upay_notify
from handlers.admin_web import build_admin_routes
from handlers.slot_ad_handlers import (
    handle_slot_text_input,
    sched_daily,
    sched_delete_prev,
    sched_every_hours,
    sched_off,
    sched_on,
    sched_pin,
    sched_set_text,
    sched_status,
    slot_edit_cmd,
    slot_clear_default_cmd,
    slot_set_default_cmd,
    slot_terminate_cmd,
)

# 错误处理
from handlers.error_handler import error_handler

# 统计和搜索功能
from handlers.stats_handlers import get_hot_posts, get_user_stats, update_post_stats
from handlers.search_handlers import (
    search_posts, 
    get_tag_cloud, 
    get_my_posts, 
    search_by_user, 
    delete_posts_batch,
    handle_search_input
)
# 频道消息监听器
from handlers.channel_listener import (
    handle_channel_message,
    check_deleted_messages_periodic
)
from handlers.index_handlers import (
    rebuild_index_command,
    sync_index_command,
    index_stats_command,
    optimize_index_command
)

# 搜索引擎
from utils.search_engine import init_search_engine
from utils.index_manager import auto_rebuild_index_if_needed
from utils.scheduled_publish_service import scheduled_publish_tick
from utils.fallback_publish_service import fallback_publish_tick
from utils.slot_ad_service import send_due_reminders

# 设置日志
logger = logging.getLogger(__name__)
setup_logging()

# 加载环境变量
load_dotenv()

# 健康检查服务器（用于 Fly.io 部署）
try:
    from health import start_health_server
    HEALTH_SERVER_ENABLED = True
except ImportError:
    HEALTH_SERVER_ENABLED = False
    logger.warning("health.py 未找到，健康检查服务器将不会启动")

# 全局变量
TIMEOUT_SECONDS = int(os.getenv("SESSION_TIMEOUT", "900"))  # 默认15分钟

# 全局关闭事件，用于优雅退出
_shutdown_event: asyncio.Event = None

# 黑名单过滤函数包装器
def check_blacklist(handler_func):
    """黑名单过滤函数包装器"""
    async def wrapper(update, context):
        # 先进行黑名单检查
        if not blacklist_filter(update):
            # 如果在黑名单中，直接返回
            return
        # 不在黑名单中，调用原始处理函数
        return await handler_func(update, context)
    return wrapper

# 会话超时检查函数
async def check_conversation_timeout(update: Update, context: CallbackContext) -> None:
    """
    检查会话是否超时的处理函数
    
    Args:
        update: Telegram 更新对象
        context: 回调上下文
    """
    # 排除频道消息和频道回复
    if update.channel_post or update.edited_channel_post:
        return
    
    # 检查是否是频道或群组中的消息（通过 chat.type 判断）
    if update.message and update.message.chat:
        chat_type = getattr(update.message.chat, 'type', None)
        if chat_type in ('channel', 'supergroup', 'group'):
            # 对于频道和群组，不进行会话超时检查
            return
    
    if not update.effective_user:
        return
    
    user_id = update.effective_user.id
    
    # 对命令消息进行特殊处理 - 命令直接通过，不检查超时
    if update.message and update.message.text and update.message.text.startswith('/'):
        command = update.message.text.split()[0]  # 获取命令部分
        logger.debug(f"跳过命令消息的超时检查: {command}")
        # 关键点：对于命令消息，不进行任何阻止，直接通过
        return
    
    # 检查用户是否在黑名单中
    if is_blacklisted(user_id):
        logger.warning(f"黑名单用户 {user_id} 尝试发送消息")
        await update.message.reply_text("❌ 您已被列入黑名单，无法使用此机器人。")
        return ApplicationHandlerStop()
    
    # 尝试获取用户会话状态
    try:
        user_state = get_user_state(user_id)
        
        # 如果用户没有会话，允许正常流程继续
        if not user_state:
            logger.debug(f"用户 {user_id} 没有活跃会话，不检查超时")
            return
        
        # 检查超时
        import time
        current_time = time.time()
        last_activity = user_state.get("last_activity", 0)
        time_diff = current_time - last_activity
        
        if time_diff > TIMEOUT_SECONDS:
            logger.info(f"用户 {user_id} 会话超时 ({time_diff:.2f}秒 > {TIMEOUT_SECONDS}秒)")
            
            # 删除用户会话数据
            delete_user_state(user_id)
            
            # 向用户发送超时通知
            try:
                await update.message.reply_text(
                    "⏱️ 您的会话已超时。请发送 /start 重新开始。"
                )
            except Exception as e:
                logger.error(f"发送超时通知失败: {e}")
            
            return ApplicationHandlerStop()
        
        logger.debug(f"用户 {user_id} 会话活跃 ({time_diff:.2f}秒 < {TIMEOUT_SECONDS}秒)")
    except Exception as e:
        logger.error(f"检查会话超时时发生错误: {e}")
        # 出错时不阻止消息处理继续，而是让正常流程继续
    
    return

# 添加全局更新记录器
async def log_all_updates(update: Update, context: CallbackContext) -> None:
    """记录所有接收到的更新"""
    if update.message and update.message.text:
        logger.info(f"收到命令: {update.message.text} 来自用户: {update.effective_user.id}")
    return None  # 允许更新继续传递给其他处理器

async def setup_bot_commands(application):
    """
    设置机器人命令菜单（左侧斜杠按钮）
    """
    commands = [
        BotCommand("start", "🚀 启动机器人"),
        BotCommand("submit", "📝 发起投稿"),
        BotCommand("ad", "📢 发布广告（扣次数）"),
        BotCommand("ad_balance", "💳 查看广告余额"),
        BotCommand("slot_edit", "🛠️ 修改按钮广告内容"),
        BotCommand("search", "🔍 搜索投稿内容"),
        BotCommand("tags", "🏷️ 查看标签云"),
        BotCommand("myposts", "📋 查看我的投稿"),
        BotCommand("mystats", "📊 查看个人统计"),
        BotCommand("hot", "🔥 查看热门投稿"),
        BotCommand("help", "❓ 查看帮助信息"),
        BotCommand("cancel", "❌ 取消当前操作"),
        BotCommand("settings", "⚙️ 机器人设置"),
    ]
    
    try:
        await application.bot.set_my_commands(commands)
        logger.info(f"成功设置 {len(commands)} 个命令菜单项")
    except Exception as e:
        logger.error(f"设置命令菜单失败: {e}", exc_info=True)


async def main():
    """
    主函数 - 设置并启动机器人
    """
    logger.info(f"启动TeleSubmit机器人。版本: {CONFIG.get('VERSION', '0.1.0')}")
    logger.info(f"会话超时时间: {TIMEOUT_SECONDS}秒")
    
    # 启动健康检查服务器（仅在 Polling 模式下）
    # Webhook 模式会使用 telegram 的内置服务器
    if HEALTH_SERVER_ENABLED and RUN_MODE == 'POLLING':
        try:
            start_health_server(port=8080)
            logger.info("健康检查服务器已启动（Polling 模式）")
        except Exception as e:
            logger.warning(f"启动健康检查服务器失败: {e}")
    
    # 初始化数据库
    await init_db()
    from utils.operation_service import recover_interrupted_operations
    await recover_interrupted_operations()
    # 载入运行时配置快照（广告/AI 等热更新项）
    try:
        from utils.runtime_settings import refresh as refresh_runtime_settings
        await refresh_runtime_settings()
    except Exception as e:
        logger.warning(f"运行时配置加载失败（可忽略，将使用静态配置）: {e}")
    # 初始化投稿限制策略（白名单/档位）
    try:
        from utils.submit_policy import init_submit_policy
        await init_submit_policy()
    except Exception as e:
        logger.warning(f"投稿策略初始化失败（可忽略，将仅使用全局默认）: {e}")
    # 初始化用户会话数据库
    initialize_database()
    # 初始化黑名单
    await init_blacklist()
    
    # 初始化搜索引擎
    logger.info("正在初始化搜索引擎...")
    try:
        from config.settings import SEARCH_INDEX_DIR, SEARCH_ENABLED
        if SEARCH_ENABLED:
            # 初始化搜索引擎（内置兼容性检查和自动重建）
            from utils.search_engine import get_search_engine
            search_engine = init_search_engine(index_dir=SEARCH_INDEX_DIR, from_scratch=False)
            logger.info(f"搜索引擎初始化完成，索引目录: {SEARCH_INDEX_DIR}")
            
            # 检查是否需要重新索引
            if hasattr(search_engine, '_needs_reindex') and search_engine._needs_reindex:
                logger.warning("检测到索引已重建，需要重新索引所有帖子")
                logger.info("正在从数据库重新索引...")
                try:
                    result = await auto_rebuild_index_if_needed()
                    if result.get("success") or (result.get("result") and result["result"].get("success")):
                        logger.info(f"✅ 索引重建成功！")
                        # 清除重建标记
                        search_engine._needs_reindex = False
                    else:
                        logger.warning(f"⚠️ 索引重建未完全成功，搜索功能可能受限")
                except Exception as rebuild_err:
                    logger.error(f"自动重建索引失败: {rebuild_err}", exc_info=True)
                    logger.warning("搜索功能可能不可用，请手动执行 /rebuild_index")
            else:
                # 正常的索引检查和同步
                logger.info("正在检查搜索索引...")
                try:
                    result = await auto_rebuild_index_if_needed()
                    if result["action"] == "sync":
                        sync_result = result["result"]
                        if sync_result["success"]:
                            logger.info(f"✅ 索引已自动同步: 添加 {sync_result['added']} 个, 删除 {sync_result['removed']} 个")
                        else:
                            logger.warning(f"⚠️ 索引同步部分失败: {sync_result.get('errors', [])}")
                    elif result["action"] == "rebuild":
                        rebuild_result = result["result"]
                        if rebuild_result["success"]:
                            logger.info(f"✅ 索引已自动重建: 成功 {rebuild_result['added']} 个, 失败 {rebuild_result['failed']} 个 (原因: {result.get('reason', '未知')})")
                        else:
                            logger.warning(f"⚠️ 索引重建失败: {rebuild_result.get('errors', [])}")
                    elif result["action"] == "none":
                        logger.info(f"✅ {result['reason']}")
                    else:
                        logger.warning(f"⚠️ 索引检查失败: {result.get('reason', '未知原因')}")
                except Exception as idx_err:
                    logger.error(f"索引检查失败: {idx_err}", exc_info=True)
                    logger.warning("将继续运行，但索引可能不准确")
        else:
            logger.info("搜索功能已禁用")
    except Exception as e:
        logger.error(f"搜索引擎初始化失败: {e}", exc_info=True)
        logger.warning("将继续运行，但搜索功能可能不可用")
    
    # 创建和启动应用程序
    token = TOKEN
    if not token:
        logger.error("未设置TELEGRAM_BOT_TOKEN环境变量")
        sys.exit(1)
        
    # 创建Application实例
    application = Application.builder().token(token).build()
    
    # 设置应用程序
    setup_application(application)
    
    # 初始化应用程序
    logger.info(f"机器人正在启动，运行模式: {RUN_MODE}")
    await application.initialize()
    application.bot_data["telegram_api"] = TelegramAPI(token)
    await application.start()
    
    # 设置命令菜单
    await setup_bot_commands(application)
    
    # 根据运行模式选择启动方式
    webhook_server = None
    
    if RUN_MODE == 'WEBHOOK':
        # Webhook 模式
        logger.info("📡 启动 Webhook 模式...")
        
        # 验证 Webhook URL
        if not WEBHOOK_URL:
            logger.error("❌ Webhook 模式需要设置 WEBHOOK_URL")
            sys.exit(1)
        
        # 导入 Webhook 服务器模块
        from utils.webhook_server import WebhookServer, setup_webhook
        
        # 生成或使用 Secret Token
        import secrets
        secret_token = WEBHOOK_SECRET_TOKEN or secrets.token_urlsafe(32)
        if not WEBHOOK_SECRET_TOKEN:
            logger.info(f"已自动生成 Secret Token: {secret_token}")
        
        # 创建并启动 Webhook 服务器（包含健康检查）
        extra_routes = []
        if PAID_AD_ENABLED or SLOT_AD_ENABLED:
            extra_routes.append(("POST", UPAY_NOTIFY_PATH, upay_notify))
        if ADMIN_WEB_ENABLED:
            extra_routes.extend(build_admin_routes())
        webhook_server = WebhookServer(
            application=application,
            port=WEBHOOK_PORT,
            path=WEBHOOK_PATH,
            secret_token=secret_token,
            extra_routes=extra_routes,
        )
        await webhook_server.start()
        
        # 设置 Telegram Webhook
        success = await setup_webhook(
            application=application,
            webhook_url=WEBHOOK_URL,
            webhook_path=WEBHOOK_PATH,
            secret_token=secret_token
        )
        
        if not success:
            logger.error("❌ Webhook 设置失败")
            await webhook_server.stop()
            sys.exit(1)
        
        logger.info(f"✅ Webhook 模式已启动")
        logger.info(f"   监听地址: 0.0.0.0:{WEBHOOK_PORT}{WEBHOOK_PATH}")
        logger.info(f"   外部地址: {WEBHOOK_URL}{WEBHOOK_PATH}")
        logger.info(f"   健康检查: http://0.0.0.0:{WEBHOOK_PORT}/health")
        logger.info(f"   Secret Token: {'已设置' if WEBHOOK_SECRET_TOKEN else f'{secret_token[:16]}...'}")
        
    else:
        # Polling 模式（默认）
        logger.info("🔄 启动 Polling 模式...")
        # 明确指定需要接收的更新类型，包括频道消息
        allowed_updates = telegram_allowed_updates()
        await application.updater.start_polling(allowed_updates=allowed_updates)
        logger.info("✅ Polling 模式已启动")
    
    # 创建关闭事件
    global _shutdown_event
    _shutdown_event = asyncio.Event()

    # 添加事件处理器以便优雅关闭
    loop = asyncio.get_running_loop()
    stop_signals = (signal.SIGINT, signal.SIGTERM, signal.SIGABRT)
    for s in stop_signals:
        loop.add_signal_handler(
            s, lambda s=s: asyncio.create_task(shutdown(application, s, webhook_server))
        )

    logger.info("机器人运行中，使用 Ctrl+C 停止")

    # 等待关闭事件
    await _shutdown_event.wait()

    logger.info("机器人已停止")


async def shutdown(application, sig, webhook_server=None):
    """
    优雅地关闭机器人

    Args:
        application: telegram.ext.Application 实例
        sig: 信号类型
        webhook_server: Webhook 服务器实例（可选）
    """
    global _shutdown_event

    logger.info(f"收到信号 {sig.name}，正在关闭...")

    try:
        # 如果是 Webhook 模式，停止 webhook 服务器并删除 webhook
        if webhook_server:
            try:
                logger.info("正在停止 Webhook 服务器...")
                await webhook_server.stop()
                logger.info("Webhook 服务器已停止")
            except Exception as e:
                logger.warning(f"停止 Webhook 服务器失败: {e}")

            try:
                logger.info("正在删除 Telegram Webhook...")
                await application.bot.delete_webhook(drop_pending_updates=False)
                logger.info("Telegram Webhook 已删除")
            except Exception as e:
                logger.warning(f"删除 Webhook 失败: {e}")

        # 关闭机器人更新器
        if application.updater.running:
            await application.updater.stop()
        await application.stop()
        await application.shutdown()
        adapter = application.bot_data.get("telegram_api")
        if adapter:
            await adapter.close()

        logger.info("应用程序关闭完成")

    except Exception as e:
        logger.error(f"关闭过程中发生错误: {e}")

    finally:
        # 触发关闭事件，让 main() 函数正常退出
        if _shutdown_event:
            _shutdown_event.set()


def setup_application(application):
    """
    初始化和配置应用程序
    """
    # 首先设置全局记录器为最高优先级
    application.add_handler(MessageHandler(filters.ALL, log_all_updates), group=-999)
    
    # 添加黑名单管理命令和调试命令（设置为最高优先级，不可被其他处理器拦截）
    try:
        logger.info("注册高优先级命令处理器...")
        application.add_handler(CommandHandler('debug', debug), group=-998)
        application.add_handler(CommandHandler('blacklist_add', blacklist_add), group=-998)
        application.add_handler(CommandHandler('blacklist_remove', blacklist_remove), group=-998)
        application.add_handler(CommandHandler('blacklist_list', blacklist_list), group=-998)
        # 不再注册高优先级的cancel命令，只在ConversationHandler的fallbacks中注册
        # application.add_handler(CommandHandler('cancel', cancel), group=-998)  # 注释掉这行
        logger.info("高优先级命令处理器注册完成")
    except Exception as e:
        logger.error(f"注册高优先级命令处理器失败: {e}", exc_info=True)
    
    # 注册错误处理
    application.add_error_handler(error_handler)
    
    # 注册基本命令处理器
    from handlers.telegram_ui_admin import ui_settings, publication_status, publication_resolve, publication_retry, publication_repair
    from handlers.widget_handlers import OrderWidgetHandler, handle_widget_button
    application.add_handler(OrderWidgetHandler(), group=-997)
    application.add_handler(CommandHandler("telegram_ui", ui_settings))
    application.add_handler(CommandHandler("publish_status", publication_status))
    application.add_handler(CommandHandler("publish_resolve", publication_resolve))
    application.add_handler(CommandHandler("publish_retry", publication_retry))
    application.add_handler(CommandHandler("publish_repair", publication_repair))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CommandHandler("settings", settings))
    application.add_handler(CommandHandler("ad_balance", paid_ad_balance))
    application.add_handler(CommandHandler("blacklist", manage_blacklist), group=1)

    # Slot Ads / 定时发布（管理员命令 + 私聊输入承接）
    application.add_handler(CommandHandler("sched_status", sched_status))
    application.add_handler(CommandHandler("sched_on", sched_on))
    application.add_handler(CommandHandler("sched_off", sched_off))
    application.add_handler(CommandHandler("sched_pin", sched_pin))
    application.add_handler(CommandHandler("sched_delete_prev", sched_delete_prev))
    application.add_handler(CommandHandler("sched_set_text", sched_set_text))
    application.add_handler(CommandHandler("sched_daily", sched_daily))
    application.add_handler(CommandHandler("sched_every_hours", sched_every_hours))
    application.add_handler(CommandHandler("slot_set_default", slot_set_default_cmd))
    application.add_handler(CommandHandler("slot_clear_default", slot_clear_default_cmd))
    application.add_handler(CommandHandler("slot_terminate", slot_terminate_cmd))
    application.add_handler(CommandHandler("slot_edit", slot_edit_cmd))

    application.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, handle_slot_text_input),
        group=1,
    )
    
    # 注册统计和搜索命令处理器
    application.add_handler(CommandHandler("hot", get_hot_posts))
    application.add_handler(CommandHandler("mystats", get_user_stats))
    application.add_handler(CommandHandler("search", search_posts))
    application.add_handler(CommandHandler("tags", get_tag_cloud))
    application.add_handler(CommandHandler("myposts", get_my_posts))
    application.add_handler(CommandHandler("searchuser", search_by_user))
    application.add_handler(CommandHandler("delete_posts", delete_posts_batch))
    
    # 注册索引管理命令处理器（仅管理员）
    application.add_handler(CommandHandler("rebuild_index", rebuild_index_command))
    application.add_handler(CommandHandler("sync_index", sync_index_command))
    application.add_handler(CommandHandler("index_stats", index_stats_command))
    application.add_handler(CommandHandler("optimize_index", optimize_index_command))
    
    # 注册会话超时检查处理器
    application.add_handler(MessageHandler(filters.ALL, check_conversation_timeout), group=0)
    
    # 注册频道消息监听器（监听频道新消息，自动记录到数据库）
    try:
        logger.info("注册频道消息监听器...")
        # 频道消息不会触发普通的 MessageHandler，需要使用 BaseHandler 捕获所有更新
        # 然后在处理器内部检查 update.channel_post 或 update.edited_channel_post
        # 创建一个自定义的 BaseHandler 来捕获所有更新
        class ChannelPostHandler(BaseHandler):
            """自定义处理器，用于捕获频道消息"""
            def __init__(self, callback):
                super().__init__(callback)
            
            def check_update(self, update):
                """检查更新是否包含频道消息"""
                return update.channel_post is not None or update.edited_channel_post is not None
        
        application.add_handler(ChannelPostHandler(handle_channel_message), group=2)
        logger.info("频道消息监听器注册完成")
    except Exception as e:
        logger.error(f"注册频道消息监听器失败: {e}", exc_info=True)
    
    try:
        # 添加独立的 /start 命令处理器（只显示欢迎信息）
        logger.info("注册 /start 命令处理器...")
        application.add_handler(CommandHandler("start", start), group=1)
        
        # 添加会话处理器
        logger.info("注册会话处理器...")
        from handlers.callback_handlers import handle_callback_query
        conv_handler = ConversationHandler(
            entry_points=[
                CommandHandler("submit", submit),
                CommandHandler("ad", paid_ad),
                # 添加底部菜单按钮"开始投稿"作为 entry_point
                MessageHandler(filters.Regex(r".*开始投稿$"), submit)
            ],
            states={
                # 模式选择状态
                STATE.get('START_MODE', 0): [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, select_mode)
                ],
                
                # 文档和媒体处理状态 - 优先处理skip_media命令
                STATE.get('MEDIA', 2): [
                    CommandHandler('done_media', done_media),
                    CommandHandler('skip_media', skip_media),
                    MessageHandler(filters.PHOTO | filters.VIDEO | filters.ANIMATION | filters.AUDIO |
                                 filters.Document.Category("animation") | filters.Document.AUDIO, 
                                 handle_media),
                    # 在媒体状态下也检查文档类型
                    MessageHandler(filters.Document.ALL, handle_media),
                    # 添加媒体模式切换回调
                    CallbackQueryHandler(switch_to_doc_mode, pattern="^switch_to_doc$"),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, prompt_media)
                ],
                STATE.get('DOC', 1): [
                    CommandHandler('done_doc', done_doc),
                    MessageHandler(filters.Document.ALL, handle_doc),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, prompt_doc)
                ],
                
                # 其他状态
                STATE.get('TEXT', 10): [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)],
                STATE.get('IMAGE', 11): [
                    MessageHandler(filters.PHOTO | filters.CAPTION, handle_image),
                    CommandHandler("done_img", done_image)
                ],
                STATE.get('EXTRA', 12): [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_extra)],
                STATE.get('PUBLISH', 13): [
                    CallbackQueryHandler(handle_callback_query, pattern="^submit_"),
                    CallbackQueryHandler(cancel, pattern="^cancel$")
                ],
                
                # 投稿处理状态
                STATE.get('TAG', 4): [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_tag)],
                STATE.get('LINK', 5): [
                    CommandHandler('skip_optional', skip_optional_link),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link)
                ],
                STATE.get('TITLE', 6): [
                    CommandHandler('skip_optional', skip_optional_title),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, handle_title)
                ],
                STATE.get('NOTE', 7): [
                    CommandHandler('skip_optional', skip_optional_note),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, handle_note)
                ],
                STATE.get('SPOILER', 8): [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_spoiler)],

                # 纯文本投稿状态
                STATE.get('TEXT_CONTENT', 14): [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_content)]
            },
            fallbacks=[CommandHandler("cancel", cancel)],
            name="submission_conversation",
            persistent=False,
            allow_reentry=True,
        )
        
        application.add_handler(conv_handler, group=2)
        logger.info("会话处理器注册完成")
    except Exception as e:
        logger.error(f"注册会话处理器失败: {e}", exc_info=True)
    
    # 添加回调查询处理器（统一处理所有回调）
    from handlers.callback_handlers import handle_callback_query
    from handlers.rating_handlers import handle_rating_callback
    # 添加审核回调处理器（在通用处理器之前，以便优先匹配）
    application.add_handler(CallbackQueryHandler(handle_review_callback, pattern="^review_"), group=3)
    application.add_handler(CallbackQueryHandler(handle_rating_callback, pattern=r"^rating_\d+_\d+$", block=False), group=3)
    application.add_handler(CallbackQueryHandler(handle_widget_button, pattern=r"^widget_(?:check_|balance$)", block=False), group=3)
    application.add_handler(CallbackQueryHandler(handle_callback_query), group=3)
    
    # 添加周期性清理任务
    try:
        logger.info("设置定期任务...")
        job_queue = application.job_queue
        from handlers.rating_handlers import refresh_rating_keyboards
        from utils.widget_service import widget_refresh_tick
        job_queue.run_repeating(refresh_rating_keyboards, interval=1, first=1)
        job_queue.run_repeating(widget_refresh_tick, interval=2, first=2, name="widget_refresh")
        job_queue.run_repeating(
            lambda context: asyncio.create_task(cleanup_old_data()), 
            interval=300, 
            first=10
        )
        
        # 添加周期性清理日志任务
        def clean_logs_job(context):
            """定期清理日志文件"""
            logger.info("执行定期日志清理任务")
            cleanup_old_logs("logs")
            
        # 每天凌晨3点执行一次日志清理
        job_queue.run_daily(clean_logs_job, time=datetime_time(hour=3, minute=0))
        
        # 添加帖子统计数据更新任务（默认每2小时执行一次，降低峰值）
        job_queue.run_repeating(update_post_stats, interval=7200, first=60)
        
        # 添加定期检查已删除消息的任务（每30分钟检查一次）
        job_queue.run_repeating(
            check_deleted_messages_periodic,
            interval=1800,  # 30分钟
            first=300  # 启动后5分钟开始第一次检查
        )

        # 定时发布：每 30 秒 tick 一次（配置在 DB，可热更新）
        job_queue.run_repeating(scheduled_publish_tick, interval=30, first=15)

        # 兜底定时发布：每 30 秒 tick 一次（当天无投稿才发，可热更新）
        job_queue.run_repeating(fallback_publish_tick, interval=30, first=20)

        # Slot Ads 到期提醒：每 60 秒检查一次（默认关闭，用户 opt-in）
        job_queue.run_repeating(send_due_reminders, interval=60, first=30)
        logger.info("定期任务设置完成（包括统计数据更新和删除消息检查）")
    except Exception as e:
        logger.error(f"设置定期任务失败: {e}", exc_info=True)
    
    # 将底部菜单文本映射到命令（在最低优先级前处理）
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu_shortcuts), group=998)
    # 不再捕获任意文本的“取消”，以免误触
    # 搜索模式下的自然语言输入处理（在更低优先级，避免干扰其他文本处理）
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_search_input), group=999)
    # 添加未处理消息的捕获处理器 (最低优先级组)
    application.add_handler(MessageHandler(filters.ALL, catch_all), group=1000)
    
    logger.info("应用程序设置完成")


if __name__ == "__main__":
    try:
        # 根据系统设置正确的事件循环策略
        if platform.system() == "Windows":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        
        # 确保使用新的事件循环
        asyncio.set_event_loop(asyncio.new_event_loop())
        
        # 启动主函数
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("程序中断，正在退出...")
    except Exception as e:
        logger.error(f"发生异常: {e}", exc_info=True)
        sys.exit(1)
