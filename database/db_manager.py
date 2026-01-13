"""
数据库管理模块
"""
import logging
from datetime import datetime
from contextlib import asynccontextmanager
import aiosqlite

from config.settings import DB_PATH, TIMEOUT, DB_CACHE_KB, SLOT_AD_MAX_ROWS

logger = logging.getLogger(__name__)

@asynccontextmanager
async def get_db():
    """
    数据库连接上下文管理器

    Yields:
        aiosqlite.Connection: 数据库连接对象
    """
    # 设置 30 秒超时，避免 database is locked 错误
    conn = await aiosqlite.connect(DB_PATH, timeout=30.0)
    conn.row_factory = aiosqlite.Row
    # 优化 SQLite 运行参数，降低 I/O 延迟
    try:
        await conn.execute("PRAGMA journal_mode=WAL;")
        await conn.execute("PRAGMA synchronous=NORMAL;")
        await conn.execute("PRAGMA temp_store=MEMORY;")
        # 通过负值设置 KB 为单位的 page cache 大小（默认为 4MB，可通过 DB_CACHE_KB 配置）
        await conn.execute(f"PRAGMA cache_size={-int(DB_CACHE_KB)};")
        # 设置 busy_timeout，让 SQLite 在锁冲突时等待而非立即失败
        await conn.execute("PRAGMA busy_timeout=30000;")
    except Exception:
        pass
    try:
        yield conn
        await conn.commit()
    except Exception as e:
        await conn.rollback()
        raise e
    finally:
        await conn.close()

async def init_db():
    """
    初始化数据库
    """
    try:
        async with get_db() as conn:
            # 临时投稿数据表
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS submissions (
                    user_id INTEGER PRIMARY KEY,
                    timestamp REAL,
                    mode TEXT,
                    image_id TEXT,
                    document_id TEXT,
                    tags TEXT,
                    link TEXT,
                    title TEXT,
                    note TEXT,
                    spoiler TEXT,
                    username TEXT,
                    text_content TEXT
                )
            ''')

            # 添加 text_content 字段（如果表已存在但没有该字段）
            try:
                await conn.execute('ALTER TABLE submissions ADD COLUMN text_content TEXT')
                logger.info("已添加 text_content 字段到 submissions 表")
            except Exception:
                pass

            # 已发布帖子表（用于热度统计、搜索和评分）
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS published_posts (
                    message_id INTEGER PRIMARY KEY,
                    user_id INTEGER,
                    username TEXT,
                    title TEXT,
                    tags TEXT,
                    link TEXT,
                    note TEXT,
                    content_type TEXT,
                    file_ids TEXT,
                    caption TEXT,
                    filename TEXT,
                    publish_time REAL,
                    views INTEGER DEFAULT 0,
                    forwards INTEGER DEFAULT 0,
                    reactions INTEGER DEFAULT 0,
                    heat_score REAL DEFAULT 0,
                    last_update REAL,
                    related_message_ids TEXT,
                    is_deleted INTEGER DEFAULT 0,
                    text_content TEXT,
                    rating_subject_id INTEGER,
                    rating_avg REAL DEFAULT 0.0,
                    rating_votes INTEGER DEFAULT 0
                )
            ''')

            # 添加 is_deleted 字段（如果表已存在但没有该字段）
            try:
                await conn.execute('ALTER TABLE published_posts ADD COLUMN is_deleted INTEGER DEFAULT 0')
                logger.info("已添加 is_deleted 字段到 published_posts 表")
            except Exception:
                # 字段已存在，忽略错误
                pass

            # 添加 text_content 字段到 published_posts
            try:
                await conn.execute('ALTER TABLE published_posts ADD COLUMN text_content TEXT')
                logger.info("已添加 text_content 字段到 published_posts 表")
            except Exception:
                pass

            # 添加评分相关字段到 published_posts
            try:
                await conn.execute('ALTER TABLE published_posts ADD COLUMN rating_subject_id INTEGER')
                logger.info("已添加 rating_subject_id 字段到 published_posts 表")
            except Exception:
                pass

            try:
                await conn.execute('ALTER TABLE published_posts ADD COLUMN rating_avg REAL DEFAULT 0.0')
                logger.info("已添加 rating_avg 字段到 published_posts 表")
            except Exception:
                pass

            try:
                await conn.execute('ALTER TABLE published_posts ADD COLUMN rating_votes INTEGER DEFAULT 0')
                logger.info("已添加 rating_votes 字段到 published_posts 表")
            except Exception:
                pass

            # 创建索引以提升查询性能
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_heat_score ON published_posts(heat_score DESC)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_publish_time ON published_posts(publish_time DESC)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_user_id ON published_posts(user_id)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_tags ON published_posts(tags)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_is_deleted ON published_posts(is_deleted)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_rating_subject_id ON published_posts(rating_subject_id)')

            # ============================================
            # 评分实体表
            # ============================================
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS rating_subjects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subject_type TEXT NOT NULL,
                    subject_key TEXT NOT NULL,
                    display_name TEXT,
                    score_sum INTEGER DEFAULT 0,
                    vote_count INTEGER DEFAULT 0,
                    avg_score REAL DEFAULT 0.0,
                    created_at REAL DEFAULT (strftime('%s', 'now')),
                    updated_at REAL DEFAULT (strftime('%s', 'now'))
                )
            ''')

            await conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_rating_subject_unique ON rating_subjects(subject_type, subject_key)')

            # ============================================
            # 评分标识表
            # ============================================
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS rating_subject_identifiers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subject_id INTEGER NOT NULL,
                    identifier_type TEXT NOT NULL,
                    identifier_value TEXT NOT NULL,
                    created_at REAL DEFAULT (strftime('%s', 'now')),
                    FOREIGN KEY (subject_id) REFERENCES rating_subjects(id)
                )
            ''')

            await conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_rating_identifier_unique ON rating_subject_identifiers(identifier_type, identifier_value)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_rating_identifier_subject ON rating_subject_identifiers(subject_id)')

            # ============================================
            # 评分投票表
            # ============================================
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS rating_votes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subject_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    score INTEGER NOT NULL,
                    created_at REAL DEFAULT (strftime('%s', 'now')),
                    updated_at REAL DEFAULT (strftime('%s', 'now')),
                    FOREIGN KEY (subject_id) REFERENCES rating_subjects(id)
                )
            ''')

            await conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_rating_vote_unique ON rating_votes(subject_id, user_id)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_rating_vote_subject ON rating_votes(subject_id)')

            # ============================================
            # 投稿指纹表（用于重复检测）
            # ============================================
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS submission_fingerprints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    urls TEXT,
                    tg_usernames TEXT,
                    tg_links TEXT,
                    phone_numbers TEXT,
                    emails TEXT,
                    bio_features TEXT,
                    content_hash TEXT,
                    content_length INTEGER,
                    submit_time REAL NOT NULL,
                    submission_id INTEGER,
                    status TEXT DEFAULT 'pending',
                    fingerprint_version INTEGER DEFAULT 1,
                    created_at REAL DEFAULT (strftime('%s', 'now'))
                )
            ''')

            # 指纹表索引
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_fp_user_id ON submission_fingerprints(user_id)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_fp_submit_time ON submission_fingerprints(submit_time)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_fp_content_hash ON submission_fingerprints(content_hash)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_fp_status ON submission_fingerprints(status)')

            # ============================================
            # 特征索引表（用于快速查找重复特征）
            # ============================================
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS fingerprint_features (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fingerprint_id INTEGER NOT NULL,
                    feature_type TEXT NOT NULL,
                    feature_value TEXT NOT NULL,
                    created_at REAL DEFAULT (strftime('%s', 'now')),
                    FOREIGN KEY (fingerprint_id) REFERENCES submission_fingerprints(id)
                )
            ''')

            await conn.execute('CREATE INDEX IF NOT EXISTS idx_ff_type_value ON fingerprint_features(feature_type, feature_value)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_ff_fingerprint ON fingerprint_features(fingerprint_id)')

            # ============================================
            # AI 审核缓存表
            # ============================================
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS ai_review_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content_hash TEXT NOT NULL UNIQUE,
                    approved INTEGER,
                    confidence REAL,
                    reason TEXT,
                    category TEXT,
                    requires_manual INTEGER,
                    created_at REAL DEFAULT (strftime('%s', 'now')),
                    expires_at REAL
                )
            ''')

            await conn.execute('CREATE INDEX IF NOT EXISTS idx_arc_content_hash ON ai_review_cache(content_hash)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_arc_expires ON ai_review_cache(expires_at)')

            # ============================================
            # 待审核投稿队列表
            # ============================================
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS pending_reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    submission_data TEXT NOT NULL,
                    ai_review_result TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at REAL DEFAULT (strftime('%s', 'now')),
                    reviewed_at REAL,
                    reviewed_by INTEGER,
                    review_note TEXT
                )
            ''')

            await conn.execute('CREATE INDEX IF NOT EXISTS idx_pr_status ON pending_reviews(status)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_pr_user_id ON pending_reviews(user_id)')

            # ============================================
            # 付费广告次数（UPAY_PRO）相关表
            # ============================================
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS user_ad_credits (
                    user_id INTEGER PRIMARY KEY,
                    balance INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL
                )
            ''')

            await conn.execute('''
                CREATE TABLE IF NOT EXISTS ad_orders (
                    out_trade_no TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    sku_id TEXT NOT NULL,
                    credits INTEGER NOT NULL,
                    amount TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    status TEXT NOT NULL,
                    upay_trade_id TEXT,
                    payment_url TEXT,
                    expires_at REAL,
                    created_at REAL NOT NULL,
                    paid_at REAL
                )
            ''')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_ad_orders_user_id ON ad_orders(user_id)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_ad_orders_status ON ad_orders(status)')

            await conn.execute('''
                CREATE TABLE IF NOT EXISTS ad_credit_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    out_trade_no TEXT UNIQUE,
                    user_id INTEGER NOT NULL,
                    delta INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
            ''')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_ad_ledger_user_id ON ad_credit_ledger(user_id)')

            # ============================================
            # 定时发布（Scheduled Publish）配置
            # ============================================
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS scheduled_publish_config (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    enabled INTEGER NOT NULL DEFAULT 0,
                    schedule_type TEXT NOT NULL DEFAULT 'daily_at',
                    schedule_payload TEXT NOT NULL DEFAULT '{}',
                    message_text TEXT NOT NULL DEFAULT '',
                    auto_pin INTEGER NOT NULL DEFAULT 0,
                    delete_prev INTEGER NOT NULL DEFAULT 0,
                    next_run_at REAL,
                    last_run_at REAL,
                    last_message_chat_id INTEGER,
                    last_message_id INTEGER,
                    updated_at REAL
                )
            """)
            # 兼容迁移：为旧库补齐新字段
            try:
                await conn.execute("ALTER TABLE scheduled_publish_config ADD COLUMN auto_pin INTEGER NOT NULL DEFAULT 0")
                logger.info("已添加 auto_pin 字段到 scheduled_publish_config 表")
            except Exception:
                pass
            try:
                await conn.execute("ALTER TABLE scheduled_publish_config ADD COLUMN delete_prev INTEGER NOT NULL DEFAULT 0")
                logger.info("已添加 delete_prev 字段到 scheduled_publish_config 表")
            except Exception:
                pass
            await conn.execute("""
                INSERT OR IGNORE INTO scheduled_publish_config(id, enabled, schedule_type, schedule_payload, message_text, updated_at)
                VALUES (1, 0, 'daily_at', '{}', '', strftime('%s', 'now'))
            """)

            # ============================================
            # 兜底定时发布（Fallback Publish）配置 & 消息池
            # ============================================
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS fallback_publish_config (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    enabled INTEGER NOT NULL DEFAULT 0,
                    schedule_type TEXT NOT NULL DEFAULT 'daily_at',
                    schedule_payload TEXT NOT NULL DEFAULT '{}',
                    header_text TEXT NOT NULL DEFAULT '',
                    footer_text TEXT NOT NULL DEFAULT '',
                    next_run_at REAL,
                    last_run_at REAL,
                    cycle_id INTEGER NOT NULL DEFAULT 1,
                    miss_tolerance_seconds INTEGER NOT NULL DEFAULT 300,
                    updated_at REAL
                )
            """)
            # 兼容迁移：为旧库补齐新字段
            try:
                await conn.execute("ALTER TABLE fallback_publish_config ADD COLUMN header_text TEXT NOT NULL DEFAULT ''")
                logger.info("已添加 header_text 字段到 fallback_publish_config 表")
            except Exception:
                pass
            try:
                await conn.execute("ALTER TABLE fallback_publish_config ADD COLUMN footer_text TEXT NOT NULL DEFAULT ''")
                logger.info("已添加 footer_text 字段到 fallback_publish_config 表")
            except Exception:
                pass
            await conn.execute("""
                INSERT OR IGNORE INTO fallback_publish_config(
                    id, enabled, schedule_type, schedule_payload,
                    cycle_id, miss_tolerance_seconds, updated_at
                )
                VALUES (1, 0, 'daily_at', '{}', 1, 300, strftime('%s', 'now'))
            """)

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS fallback_message_pool (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    display_name TEXT,
                    platform_domain TEXT,
                    platform_tg_username TEXT,
                    rating_subject_id INTEGER,
                    message_text TEXT NOT NULL,
                    used_cycle_id INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL
                )
            """)
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_fallback_pool_enabled ON fallback_message_pool(enabled)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_fallback_pool_used_cycle_id ON fallback_message_pool(used_cycle_id)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_fallback_pool_rating_subject_id ON fallback_message_pool(rating_subject_id)")

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS fallback_publish_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_key TEXT NOT NULL UNIQUE,
                    scheduled_at REAL NOT NULL,
                    status TEXT NOT NULL,
                    published_posts_count INTEGER NOT NULL DEFAULT 0,
                    picked_pool_id INTEGER,
                    sent_message_chat_id INTEGER,
                    sent_message_id INTEGER,
                    error TEXT,
                    created_at REAL NOT NULL
                )
            """)
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_fallback_runs_scheduled_at ON fallback_publish_runs(scheduled_at DESC)")

            # ============================================
            # 运行时配置（热更新 key-value）
            # ============================================
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS runtime_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at REAL
                )
            """)

            # ============================================
            # 按钮广告位（Slot Ads）
            # ============================================
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS ad_slots (
                    slot_id INTEGER PRIMARY KEY,
                    default_text TEXT,
                    default_url TEXT,
                    default_buttons_json TEXT,
                    sell_enabled INTEGER NOT NULL DEFAULT 1,
                    updated_at REAL
                )
            """)
            # 兼容迁移：为旧库补齐新字段（多默认按钮）
            try:
                await conn.execute("ALTER TABLE ad_slots ADD COLUMN default_buttons_json TEXT")
                logger.info("已添加 default_buttons_json 字段到 ad_slots 表")
            except Exception:
                pass

            # 按配置补齐 slot（1..MAX_ROWS）
            for slot_id in range(1, int(SLOT_AD_MAX_ROWS) + 1):
                await conn.execute(
                    "INSERT OR IGNORE INTO ad_slots(slot_id, sell_enabled, updated_at) VALUES (?, 1, strftime('%s','now'))",
                    (slot_id,),
                )

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS slot_ad_creatives (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    button_text TEXT NOT NULL,
                    button_url TEXT NOT NULL,
                    ai_review_result TEXT,
                    ai_review_passed INTEGER,
                    created_at REAL NOT NULL
                )
            """)
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_slot_ad_creatives_user_id ON slot_ad_creatives(user_id)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_slot_ad_creatives_created_at ON slot_ad_creatives(created_at)")

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS slot_ad_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    out_trade_no TEXT UNIQUE,
                    slot_id INTEGER NOT NULL,
                    buyer_user_id INTEGER NOT NULL,
                    creative_id INTEGER NOT NULL,
                    plan_days INTEGER NOT NULL,
                    amount TEXT,
                    currency TEXT,
                    status TEXT NOT NULL,
                    upay_trade_id TEXT,
                    payment_url TEXT,
                    expires_at REAL,
                    start_at REAL,
                    end_at REAL,
                    created_at REAL NOT NULL,
                    paid_at REAL,
                    terminated_at REAL,
                    terminate_reason TEXT,
                    reminder_opt_in INTEGER NOT NULL DEFAULT 0,
                    remind_at REAL,
                    remind_sent INTEGER NOT NULL DEFAULT 0,
                    remind_sent_at REAL
                )
            """)
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_slot_ad_orders_slot_status ON slot_ad_orders(slot_id, status)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_slot_ad_orders_slot_time ON slot_ad_orders(slot_id, start_at, end_at)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_slot_ad_orders_buyer_status ON slot_ad_orders(buyer_user_id, status)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_slot_ad_orders_remind ON slot_ad_orders(remind_at, remind_sent)")

            # Slot Ads 编辑审计（用于“每单每天修改次数限制”与追溯）
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS slot_ad_order_edits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    out_trade_no TEXT NOT NULL,
                    day_key TEXT NOT NULL,
                    editor_type TEXT NOT NULL,
                    editor_user_id INTEGER,
                    old_creative_id INTEGER,
                    new_creative_id INTEGER NOT NULL,
                    note TEXT,
                    created_at REAL NOT NULL
                )
            """)
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_slot_ad_order_edits_order_day ON slot_ad_order_edits(out_trade_no, day_key)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_slot_ad_order_edits_created_at ON slot_ad_order_edits(created_at DESC)")

            await conn.commit()
            logger.info("数据库初始化完成")
    except Exception as e:
        logger.error(f"初始化数据库时出错: {e}")
        raise

async def cleanup_old_data():
    """
    清理过期的会话数据
    """
    try:
        # 首先检查表是否存在
        async with aiosqlite.connect(DB_PATH) as conn:
            c = await conn.cursor()
            await c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='submissions'")
            table_exists = await c.fetchone()
            
        if not table_exists:
            logger.warning("submissions 表不存在，跳过清理")
            return
            
        # 如果表存在，执行清理
        async with get_db() as conn:
            c = await conn.cursor()
            cutoff = datetime.now().timestamp() - TIMEOUT
            await c.execute("DELETE FROM submissions WHERE timestamp < ?", (cutoff,))
            logger.info("已清理过期数据")
    except Exception as e:
        logger.error(f"清理过期数据失败: {e}")
