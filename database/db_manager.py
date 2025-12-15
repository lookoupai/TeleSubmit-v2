"""
数据库管理模块
"""
import logging
from datetime import datetime
from contextlib import asynccontextmanager
import aiosqlite

from config.settings import DB_PATH, TIMEOUT, DB_CACHE_KB

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
