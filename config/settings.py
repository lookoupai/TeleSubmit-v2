"""
配置文件读取和变量定义模块
"""
import os
import configparser
import logging

logger = logging.getLogger(__name__)

# 项目根目录
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, 'config.ini')

# 读取配置文件
config = configparser.ConfigParser()

# 安全读取配置文件
if os.path.exists(CONFIG_PATH):
    config.read(CONFIG_PATH)
    logger.info(f"已加载配置文件: {CONFIG_PATH}")
else:
    logger.warning(f"⚠️ 配置文件 {CONFIG_PATH} 不存在，将仅使用环境变量")

# 辅助函数：安全获取配置
def get_config(section, key, fallback=None):
    """安全获取配置值"""
    try:
        return config.get(section, key)
    except (configparser.NoSectionError, configparser.NoOptionError, ValueError):
        return fallback

def get_config_int(section, key, fallback=0):
    """安全获取整数配置值"""
    try:
        return config.getint(section, key)
    except (configparser.NoSectionError, configparser.NoOptionError, ValueError):
        return fallback

def get_config_bool(section, key, fallback=False):
    """安全获取布尔配置值"""
    try:
        return config.getboolean(section, key)
    except (configparser.NoSectionError, configparser.NoOptionError, ValueError):
        return fallback

# 辅助函数：优先从环境变量获取，如果不存在则从配置文件获取
def get_env_or_config(env_key, section, config_key, fallback=None):
    """
    优先从环境变量获取配置，如果环境变量不存在则从配置文件获取
    
    环境变量优先级规则：
    - 如果环境变量存在（即使值为空字符串），使用环境变量的值
    - 如果环境变量不存在，从配置文件读取
    - 如果配置文件也不存在，使用 fallback 默认值
    
    Args:
        env_key: 环境变量名
        section: 配置文件节名
        config_key: 配置文件键名
        fallback: 如果都不存在时的默认值
    
    Returns:
        配置值（可能是字符串、None 或 fallback）
    """
    if env_key in os.environ:
        # 环境变量存在，优先使用（即使值为空字符串，也使用环境变量的值）
        value = os.environ[env_key]
        logger.debug(f"使用环境变量 {env_key}={value[:20] + '...' if value and len(value) > 20 else (value if value else '(空)')}")
        return value
    else:
        # 环境变量不存在，使用配置文件
        value = get_config(section, config_key, fallback)
        if value:
            logger.debug(f"使用配置文件 {section}.{config_key}={value[:20] + '...' if len(value) > 20 else value}")
        return value

# 从环境变量或配置文件获取配置（环境变量优先）
TOKEN = get_env_or_config('TOKEN', 'BOT', 'TOKEN')
CHANNEL_ID = get_env_or_config('CHANNEL_ID', 'BOT', 'CHANNEL_ID')
DB_PATH = get_config('BOT', 'DB_PATH', fallback='data/submissions.db')
TIMEOUT = int(get_env_or_config('TIMEOUT', 'BOT', 'TIMEOUT') or get_config_int('BOT', 'TIMEOUT', 300))
ALLOWED_TAGS = int(get_env_or_config('ALLOWED_TAGS', 'BOT', 'ALLOWED_TAGS') or get_config_int('BOT', 'ALLOWED_TAGS', 30))
NET_TIMEOUT = 120   # 网络请求超时时间（秒）

# OWNER_ID 需要转换为整数类型
_owner_id_str = get_env_or_config('OWNER_ID', 'BOT', 'OWNER_ID')
try:
    OWNER_ID = int(_owner_id_str) if _owner_id_str else None
except (ValueError, TypeError):
    OWNER_ID = None
    logger.warning(f"OWNER_ID 配置无效，无法转换为整数: {_owner_id_str}")

# ADMIN_IDS 管理员ID列表（用于管理命令）
_admin_ids_str = get_env_or_config('ADMIN_IDS', 'BOT', 'ADMIN_IDS') or ''
ADMIN_IDS = []
if _admin_ids_str:
    try:
        # 支持逗号分隔的多个ID
        ADMIN_IDS = [int(id.strip()) for id in _admin_ids_str.split(',') if id.strip()]
    except (ValueError, TypeError):
        logger.warning(f"ADMIN_IDS 配置无效: {_admin_ids_str}")
        ADMIN_IDS = []

# 如果设置了 OWNER_ID 且不在 ADMIN_IDS 中，自动添加
if OWNER_ID and OWNER_ID not in ADMIN_IDS:
    ADMIN_IDS.append(OWNER_ID)

# 布尔值配置：环境变量优先
_show_submitter_env = os.getenv('SHOW_SUBMITTER')
if _show_submitter_env is not None:
    SHOW_SUBMITTER = _show_submitter_env.lower() in ('true', '1', 'yes')
else:
    SHOW_SUBMITTER = get_config_bool('BOT', 'SHOW_SUBMITTER', True)

_notify_owner_env = os.getenv('NOTIFY_OWNER')
if _notify_owner_env is not None:
    NOTIFY_OWNER = _notify_owner_env.lower() in ('true', '1', 'yes')
else:
    NOTIFY_OWNER = get_config_bool('BOT', 'NOTIFY_OWNER', True)

BOT_MODE = get_env_or_config('BOT_MODE', 'BOT', 'BOT_MODE', fallback='MIXED')

# 允许的文件类型配置
ALLOWED_FILE_TYPES = get_env_or_config('ALLOWED_FILE_TYPES', 'BOT', 'ALLOWED_FILE_TYPES', fallback='*')

# 运行模式配置
_run_mode = get_env_or_config('RUN_MODE', 'BOT', 'RUN_MODE', fallback='POLLING')
RUN_MODE = _run_mode.strip().upper() if _run_mode else 'POLLING'

# Webhook 配置（仅当 RUN_MODE = WEBHOOK 时生效）
WEBHOOK_URL = get_env_or_config('WEBHOOK_URL', 'WEBHOOK', 'URL', fallback='')
_webhook_port = get_env_or_config('WEBHOOK_PORT', 'WEBHOOK', 'PORT')
WEBHOOK_PORT = int(_webhook_port) if _webhook_port else get_config_int('WEBHOOK', 'PORT', 8080)
WEBHOOK_PATH = get_env_or_config('WEBHOOK_PATH', 'WEBHOOK', 'PATH', fallback='/webhook')
WEBHOOK_SECRET_TOKEN = get_env_or_config('WEBHOOK_SECRET_TOKEN', 'WEBHOOK', 'SECRET_TOKEN', fallback='')

# 搜索引擎配置
SEARCH_INDEX_DIR = get_env_or_config('SEARCH_INDEX_DIR', 'SEARCH', 'INDEX_DIR', fallback='data/search_index')
_search_enabled_env = os.getenv('SEARCH_ENABLED')
if _search_enabled_env is not None:
    SEARCH_ENABLED = _search_enabled_env.lower() in ('true', '1', 'yes')
else:
    SEARCH_ENABLED = get_config_bool('SEARCH', 'ENABLED', True)
SEARCH_ANALYZER = (get_env_or_config('SEARCH_ANALYZER', 'SEARCH', 'ANALYZER', fallback='jieba') or 'jieba').strip().lower()
_search_highlight_env = os.getenv('SEARCH_HIGHLIGHT')
if _search_highlight_env is not None:
    SEARCH_HIGHLIGHT = _search_highlight_env.lower() in ('true', '1', 'yes')
else:
    SEARCH_HIGHLIGHT = get_config_bool('SEARCH', 'HIGHLIGHT', False)

# 数据库配置
_db_cache_kb = get_env_or_config('DB_CACHE_KB', 'DB', 'CACHE_SIZE_KB')
DB_CACHE_KB = int(_db_cache_kb) if _db_cache_kb else get_config_int('DB', 'CACHE_SIZE_KB', 4096)  # SQLite page cache，单位KB

# 验证必要配置
if not TOKEN:
    raise ValueError("❌ TOKEN 未设置！请在环境变量或 config.ini 中设置")
if not CHANNEL_ID:
    raise ValueError("❌ CHANNEL_ID 未设置！请在环境变量或 config.ini 中设置")

# 模式常量定义
MODE_MEDIA = 'MEDIA'      # 仅媒体上传
MODE_DOCUMENT = 'DOCUMENT'  # 仅文档上传
MODE_MIXED = 'MIXED'      # 混合模式
MODE_TEXT = 'TEXT'        # 仅纯文本模式
MODE_ALL = 'ALL'          # 全部模式（文本+媒体+文档）

# ============================================
# 纯文本投稿配置
# ============================================
_text_only_mode_env = os.getenv('TEXT_ONLY_MODE')
if _text_only_mode_env is not None:
    TEXT_ONLY_MODE = _text_only_mode_env.lower() in ('true', '1', 'yes')
else:
    TEXT_ONLY_MODE = get_config_bool('BOT', 'TEXT_ONLY_MODE', True)

DEFAULT_SUBMIT_MODE = get_env_or_config('DEFAULT_SUBMIT_MODE', 'BOT', 'DEFAULT_SUBMIT_MODE', fallback='TEXT')
_min_text_length = get_env_or_config('MIN_TEXT_LENGTH', 'BOT', 'MIN_TEXT_LENGTH')
MIN_TEXT_LENGTH = int(_min_text_length) if _min_text_length else get_config_int('BOT', 'MIN_TEXT_LENGTH', 10)
_max_text_length = get_env_or_config('MAX_TEXT_LENGTH', 'BOT', 'MAX_TEXT_LENGTH')
MAX_TEXT_LENGTH = int(_max_text_length) if _max_text_length else get_config_int('BOT', 'MAX_TEXT_LENGTH', 4000)

# ============================================
# AI 审核配置
# ============================================
_ai_review_enabled_env = os.getenv('AI_REVIEW_ENABLED')
if _ai_review_enabled_env is not None:
    AI_REVIEW_ENABLED = _ai_review_enabled_env.lower() in ('true', '1', 'yes')
else:
    AI_REVIEW_ENABLED = get_config_bool('AI_REVIEW', 'ENABLED', False)

AI_REVIEW_API_BASE = get_env_or_config('AI_REVIEW_API_BASE', 'AI_REVIEW', 'API_BASE_URL', fallback='https://api.openai.com/v1')
AI_REVIEW_API_KEY = get_env_or_config('AI_REVIEW_API_KEY', 'AI_REVIEW', 'API_KEY', fallback='')
AI_REVIEW_MODEL = get_env_or_config('AI_REVIEW_MODEL', 'AI_REVIEW', 'MODEL', fallback='gpt-4o-mini')
_ai_timeout = get_env_or_config('AI_REVIEW_TIMEOUT', 'AI_REVIEW', 'TIMEOUT')
AI_REVIEW_TIMEOUT = int(_ai_timeout) if _ai_timeout else get_config_int('AI_REVIEW', 'TIMEOUT', 30)
_ai_retries = get_env_or_config('AI_REVIEW_MAX_RETRIES', 'AI_REVIEW', 'MAX_RETRIES')
AI_REVIEW_MAX_RETRIES = int(_ai_retries) if _ai_retries else get_config_int('AI_REVIEW', 'MAX_RETRIES', 2)

# 审核主题配置
AI_REVIEW_CHANNEL_TOPIC = get_env_or_config('AI_REVIEW_CHANNEL_TOPIC', 'AI_REVIEW', 'CHANNEL_TOPIC', fallback='接码服务')
AI_REVIEW_TOPIC_KEYWORDS = get_env_or_config('AI_REVIEW_TOPIC_KEYWORDS', 'AI_REVIEW', 'TOPIC_KEYWORDS', fallback='接码,短信,验证码,SMS,号码')

_ai_strict_mode_env = os.getenv('AI_REVIEW_STRICT_MODE')
if _ai_strict_mode_env is not None:
    AI_REVIEW_STRICT_MODE = _ai_strict_mode_env.lower() in ('true', '1', 'yes')
else:
    AI_REVIEW_STRICT_MODE = get_config_bool('AI_REVIEW', 'STRICT_MODE', False)

_ai_auto_reject_env = os.getenv('AI_REVIEW_AUTO_REJECT')
if _ai_auto_reject_env is not None:
    AI_REVIEW_AUTO_REJECT = _ai_auto_reject_env.lower() in ('true', '1', 'yes')
else:
    AI_REVIEW_AUTO_REJECT = get_config_bool('AI_REVIEW', 'AUTO_REJECT', True)

_ai_notify_user_env = os.getenv('AI_REVIEW_NOTIFY_USER')
if _ai_notify_user_env is not None:
    AI_REVIEW_NOTIFY_USER = _ai_notify_user_env.lower() in ('true', '1', 'yes')
else:
    AI_REVIEW_NOTIFY_USER = get_config_bool('AI_REVIEW', 'NOTIFY_USER', True)

# 缓存配置
_ai_cache_enabled_env = os.getenv('AI_REVIEW_CACHE_ENABLED')
if _ai_cache_enabled_env is not None:
    AI_REVIEW_CACHE_ENABLED = _ai_cache_enabled_env.lower() in ('true', '1', 'yes')
else:
    AI_REVIEW_CACHE_ENABLED = get_config_bool('AI_REVIEW', 'CACHE_ENABLED', True)

_ai_cache_ttl = get_env_or_config('AI_REVIEW_CACHE_TTL_HOURS', 'AI_REVIEW', 'CACHE_TTL_HOURS')
AI_REVIEW_CACHE_TTL_HOURS = int(_ai_cache_ttl) if _ai_cache_ttl else get_config_int('AI_REVIEW', 'CACHE_TTL_HOURS', 24)

# 降级策略: manual/pass/reject
AI_REVIEW_FALLBACK_ON_ERROR = get_env_or_config('AI_REVIEW_FALLBACK_ON_ERROR', 'AI_REVIEW', 'FALLBACK_ON_ERROR', fallback='manual')

# 管理员通知
_ai_notify_admin_reject_env = os.getenv('AI_REVIEW_NOTIFY_ADMIN_ON_REJECT')
if _ai_notify_admin_reject_env is not None:
    AI_REVIEW_NOTIFY_ADMIN_ON_REJECT = _ai_notify_admin_reject_env.lower() in ('true', '1', 'yes')
else:
    AI_REVIEW_NOTIFY_ADMIN_ON_REJECT = get_config_bool('AI_REVIEW', 'NOTIFY_ADMIN_ON_REJECT', True)

_ai_notify_admin_dup_env = os.getenv('AI_REVIEW_NOTIFY_ADMIN_ON_DUPLICATE')
if _ai_notify_admin_dup_env is not None:
    AI_REVIEW_NOTIFY_ADMIN_ON_DUPLICATE = _ai_notify_admin_dup_env.lower() in ('true', '1', 'yes')
else:
    AI_REVIEW_NOTIFY_ADMIN_ON_DUPLICATE = get_config_bool('AI_REVIEW', 'NOTIFY_ADMIN_ON_DUPLICATE', True)

# ============================================
# 重复投稿检测配置
# ============================================
_dup_check_enabled_env = os.getenv('DUPLICATE_CHECK_ENABLED')
if _dup_check_enabled_env is not None:
    DUPLICATE_CHECK_ENABLED = _dup_check_enabled_env.lower() in ('true', '1', 'yes')
else:
    DUPLICATE_CHECK_ENABLED = get_config_bool('DUPLICATE_CHECK', 'ENABLED', False)

_dup_window_days = get_env_or_config('DUPLICATE_CHECK_WINDOW_DAYS', 'DUPLICATE_CHECK', 'CHECK_WINDOW_DAYS')
DUPLICATE_CHECK_WINDOW_DAYS = int(_dup_window_days) if _dup_window_days else get_config_int('DUPLICATE_CHECK', 'CHECK_WINDOW_DAYS', 7)

_dup_threshold = get_env_or_config('DUPLICATE_SIMILARITY_THRESHOLD', 'DUPLICATE_CHECK', 'SIMILARITY_THRESHOLD')
DUPLICATE_SIMILARITY_THRESHOLD = float(_dup_threshold) if _dup_threshold else 0.8

# 检测维度开关
_dup_check_user_id_env = os.getenv('DUPLICATE_CHECK_USER_ID')
if _dup_check_user_id_env is not None:
    DUPLICATE_CHECK_USER_ID = _dup_check_user_id_env.lower() in ('true', '1', 'yes')
else:
    DUPLICATE_CHECK_USER_ID = get_config_bool('DUPLICATE_CHECK', 'CHECK_USER_ID', True)

_dup_check_urls_env = os.getenv('DUPLICATE_CHECK_URLS')
if _dup_check_urls_env is not None:
    DUPLICATE_CHECK_URLS = _dup_check_urls_env.lower() in ('true', '1', 'yes')
else:
    DUPLICATE_CHECK_URLS = get_config_bool('DUPLICATE_CHECK', 'CHECK_URLS', True)

_dup_check_contacts_env = os.getenv('DUPLICATE_CHECK_CONTACTS')
if _dup_check_contacts_env is not None:
    DUPLICATE_CHECK_CONTACTS = _dup_check_contacts_env.lower() in ('true', '1', 'yes')
else:
    DUPLICATE_CHECK_CONTACTS = get_config_bool('DUPLICATE_CHECK', 'CHECK_CONTACTS', True)

_dup_check_tg_links_env = os.getenv('DUPLICATE_CHECK_TG_LINKS')
if _dup_check_tg_links_env is not None:
    DUPLICATE_CHECK_TG_LINKS = _dup_check_tg_links_env.lower() in ('true', '1', 'yes')
else:
    DUPLICATE_CHECK_TG_LINKS = get_config_bool('DUPLICATE_CHECK', 'CHECK_TG_LINKS', True)

_dup_check_user_bio_env = os.getenv('DUPLICATE_CHECK_USER_BIO')
if _dup_check_user_bio_env is not None:
    DUPLICATE_CHECK_USER_BIO = _dup_check_user_bio_env.lower() in ('true', '1', 'yes')
else:
    DUPLICATE_CHECK_USER_BIO = get_config_bool('DUPLICATE_CHECK', 'CHECK_USER_BIO', True)

_dup_check_content_hash_env = os.getenv('DUPLICATE_CHECK_CONTENT_HASH')
if _dup_check_content_hash_env is not None:
    DUPLICATE_CHECK_CONTENT_HASH = _dup_check_content_hash_env.lower() in ('true', '1', 'yes')
else:
    DUPLICATE_CHECK_CONTENT_HASH = get_config_bool('DUPLICATE_CHECK', 'CHECK_CONTENT_HASH', True)

# 处理方式
_dup_auto_reject_env = os.getenv('DUPLICATE_AUTO_REJECT')
if _dup_auto_reject_env is not None:
    DUPLICATE_AUTO_REJECT = _dup_auto_reject_env.lower() in ('true', '1', 'yes')
else:
    DUPLICATE_AUTO_REJECT = get_config_bool('DUPLICATE_CHECK', 'AUTO_REJECT_DUPLICATE', True)

_dup_notify_user_env = os.getenv('DUPLICATE_NOTIFY_USER')
if _dup_notify_user_env is not None:
    DUPLICATE_NOTIFY_USER = _dup_notify_user_env.lower() in ('true', '1', 'yes')
else:
    DUPLICATE_NOTIFY_USER = get_config_bool('DUPLICATE_CHECK', 'NOTIFY_USER_DUPLICATE', True)

# 频率限制
_rate_limit_enabled_env = os.getenv('RATE_LIMIT_ENABLED')
if _rate_limit_enabled_env is not None:
    RATE_LIMIT_ENABLED = _rate_limit_enabled_env.lower() in ('true', '1', 'yes')
else:
    RATE_LIMIT_ENABLED = get_config_bool('DUPLICATE_CHECK', 'RATE_LIMIT_ENABLED', True)

_rate_limit_count = get_env_or_config('RATE_LIMIT_COUNT', 'DUPLICATE_CHECK', 'RATE_LIMIT_COUNT')
RATE_LIMIT_COUNT = int(_rate_limit_count) if _rate_limit_count else get_config_int('DUPLICATE_CHECK', 'RATE_LIMIT_COUNT', 3)

_rate_limit_window = get_env_or_config('RATE_LIMIT_WINDOW_HOURS', 'DUPLICATE_CHECK', 'RATE_LIMIT_WINDOW_HOURS')
RATE_LIMIT_WINDOW_HOURS = int(_rate_limit_window) if _rate_limit_window else get_config_int('DUPLICATE_CHECK', 'RATE_LIMIT_WINDOW_HOURS', 24)

# ============================================
# 评分配置
# ============================================
_rating_enabled_env = os.getenv('RATING_ENABLED')
if _rating_enabled_env is not None:
    RATING_ENABLED = _rating_enabled_env.lower() in ('true', '1', 'yes')
else:
    RATING_ENABLED = get_config_bool('RATING', 'ENABLED', True)

_rating_allow_update_env = os.getenv('RATING_ALLOW_UPDATE')
if _rating_allow_update_env is not None:
    RATING_ALLOW_UPDATE = _rating_allow_update_env.lower() in ('true', '1', 'yes')
else:
    RATING_ALLOW_UPDATE = get_config_bool('RATING', 'ALLOW_UPDATE', True)

RATING_BUTTON_STYLE = get_env_or_config('RATING_BUTTON_STYLE', 'RATING', 'BUTTON_STYLE', fallback='stars')

_rating_min_votes = get_env_or_config('RATING_MIN_VOTES_TO_HIGHLIGHT', 'RATING', 'MIN_VOTES_TO_HIGHLIGHT')
RATING_MIN_VOTES_TO_HIGHLIGHT = int(_rating_min_votes) if _rating_min_votes else get_config_int('RATING', 'MIN_VOTES_TO_HIGHLIGHT', 1)

# 自定义按钮配置（InlineKeyboard 按行配置）
CUSTOM_BUTTON_ROWS = []
try:
    if config.has_section('CUSTOM_BUTTONS'):
        for _, value in config.items('CUSTOM_BUTTONS'):
            if not value:
                continue
            row_buttons = []
            parts = value.split(';')
            for part in parts:
                part = part.strip()
                if not part:
                    continue
                if '|' not in part:
                    continue
                text, url = part.split('|', 1)
                text = text.strip()
                url = url.strip()
                if not text or not url:
                    continue
                row_buttons.append((text, url))
            if row_buttons:
                CUSTOM_BUTTON_ROWS.append(row_buttons)
except Exception as e:
    logger.warning(f"解析 CUSTOM_BUTTONS 配置失败: {e}")
    CUSTOM_BUTTON_ROWS = []

# 打印配置信息（调试用）
logger.info(f"配置加载完成:")
logger.info(f"  - BOT_MODE: {BOT_MODE}")
logger.info(f"  - RUN_MODE: {RUN_MODE}")
logger.info(f"  - CHANNEL_ID: {CHANNEL_ID}")
logger.info(f"  - DB_PATH: {DB_PATH}")
logger.info(f"  - TIMEOUT: {TIMEOUT}")
logger.info(f"  - OWNER_ID: {OWNER_ID if OWNER_ID else '未设置'}")
logger.info(f"  - ADMIN_IDS: {ADMIN_IDS if ADMIN_IDS else '未设置'}")
logger.info(f"  - ALLOWED_FILE_TYPES: {ALLOWED_FILE_TYPES}")
if RUN_MODE == 'WEBHOOK':
    logger.info(f"  - WEBHOOK_URL: {WEBHOOK_URL if WEBHOOK_URL else '未设置'}")
    logger.info(f"  - WEBHOOK_PORT: {WEBHOOK_PORT}")
    logger.info(f"  - WEBHOOK_PATH: {WEBHOOK_PATH}")
    logger.info(f"  - WEBHOOK_SECRET: {'已设置' if WEBHOOK_SECRET_TOKEN else '未设置（将自动生成）'}")
logger.info(f"  - SEARCH_INDEX_DIR: {SEARCH_INDEX_DIR}")
logger.info(f"  - SEARCH_ENABLED: {SEARCH_ENABLED}")
logger.info(f"  - SEARCH_ANALYZER: {SEARCH_ANALYZER}")
logger.info(f"  - SEARCH_HIGHLIGHT: {SEARCH_HIGHLIGHT}")
logger.info(f"  - DB_CACHE_KB: {DB_CACHE_KB}")
logger.info(f"  - TEXT_ONLY_MODE: {TEXT_ONLY_MODE}")
logger.info(f"  - AI_REVIEW_ENABLED: {AI_REVIEW_ENABLED}")
logger.info(f"  - DUPLICATE_CHECK_ENABLED: {DUPLICATE_CHECK_ENABLED}")
logger.info(f"  - RATING_ENABLED: {RATING_ENABLED}")
if AI_REVIEW_ENABLED:
    logger.info(f"  - AI_REVIEW_MODEL: {AI_REVIEW_MODEL}")
    logger.info(f"  - AI_REVIEW_CHANNEL_TOPIC: {AI_REVIEW_CHANNEL_TOPIC}")
if DUPLICATE_CHECK_ENABLED:
    logger.info(f"  - DUPLICATE_CHECK_WINDOW_DAYS: {DUPLICATE_CHECK_WINDOW_DAYS}")
    logger.info(f"  - RATE_LIMIT_ENABLED: {RATE_LIMIT_ENABLED}")
