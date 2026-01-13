"""
运行时配置（热更新，DB 落库 + 内存快照）

设计目标（KISS/YAGNI）：
- 仅承载“可安全热更新”的业务参数（广告套餐/价格、AI 审核提示词等）
- 密钥/URL/路由类配置仍由 config.ini/环境变量提供（避免安全与初始化副作用）
- 同步读取（handlers 里大量同步函数），避免在业务路径中做 async DB I/O

实现方式：
- runtime_settings 表：key/value/updated_at
- 启动时 refresh() 载入快照
- 管理后台写入后 set_many() 同步更新 DB 并刷新快照
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from database.db_manager import get_db
from config import settings as static

logger = logging.getLogger(__name__)


# DB Keys（仅用于本模块）
KEY_PAID_AD_ENABLED = "paid_ad.enabled"
KEY_PAID_AD_PACKAGES_RAW = "paid_ad.packages_raw"
KEY_PAID_AD_CURRENCY = "paid_ad.currency"
KEY_PAID_AD_PUBLISH_PREFIX = "paid_ad.publish_prefix"

KEY_UPAY_DEFAULT_TYPE = "paid_ad.upay_default_type"
KEY_UPAY_ALLOWED_TYPES = "paid_ad.upay_allowed_types"
KEY_PAY_EXPIRE_MINUTES = "paid_ad.pay_expire_minutes"

KEY_SLOT_AD_ENABLED = "slot_ad.enabled"
KEY_SLOT_AD_PLANS_RAW = "slot_ad.plans_raw"
KEY_SLOT_AD_CURRENCY = "slot_ad.currency"
KEY_SLOT_AD_ACTIVE_ROWS_COUNT = "slot_ad.active_rows_count"
KEY_SLOT_AD_RENEW_PROTECT_DAYS = "slot_ad.renew_protect_days"
KEY_SLOT_AD_BUTTON_TEXT_MAX_LEN = "slot_ad.button_text_max_len"
KEY_SLOT_AD_URL_MAX_LEN = "slot_ad.url_max_len"
KEY_SLOT_AD_REMINDER_ADVANCE_DAYS = "slot_ad.reminder_advance_days"
KEY_SLOT_AD_EDIT_LIMIT_PER_ORDER_PER_DAY = "slot_ad.edit_limit_per_order_per_day"

KEY_AI_REVIEW_ENABLED = "ai_review.enabled"
KEY_AI_REVIEW_MODEL = "ai_review.model"
KEY_AI_REVIEW_CHANNEL_TOPIC = "ai_review.channel_topic"
KEY_AI_REVIEW_TOPIC_KEYWORDS = "ai_review.topic_keywords"
KEY_AI_REVIEW_STRICT_MODE = "ai_review.strict_mode"
KEY_AI_REVIEW_AUTO_REJECT = "ai_review.auto_reject"
KEY_AI_REVIEW_FALLBACK_ON_ERROR = "ai_review.fallback_on_error"
KEY_AI_REVIEW_NOTIFY_USER = "ai_review.notify_user"
KEY_AI_REVIEW_SYSTEM_PROMPT = "ai_review.system_prompt"
KEY_AI_REVIEW_POLICY_TEXT = "ai_review.policy_text"

KEY_AD_RISK_SYSTEM_PROMPT = "ad_risk.system_prompt"
KEY_AD_RISK_PROMPT_TEMPLATE = "ad_risk.prompt_template"

# 投稿配置（热更新）
KEY_BOT_MIN_TEXT_LENGTH = "bot.min_text_length"
KEY_BOT_MAX_TEXT_LENGTH = "bot.max_text_length"
KEY_BOT_ALLOWED_TAGS = "bot.allowed_tags"
KEY_BOT_ALLOWED_FILE_TYPES = "bot.allowed_file_types"
KEY_BOT_SHOW_SUBMITTER = "bot.show_submitter"
KEY_BOT_NOTIFY_OWNER = "bot.notify_owner"

# 重复检测/限流（热更新）
KEY_DUPLICATE_CHECK_ENABLED = "duplicate_check.enabled"
KEY_DUPLICATE_CHECK_WINDOW_DAYS = "duplicate_check.window_days"
KEY_DUPLICATE_SIMILARITY_THRESHOLD = "duplicate_check.similarity_threshold"
KEY_DUPLICATE_CHECK_URLS = "duplicate_check.check_urls"
KEY_DUPLICATE_CHECK_CONTACTS = "duplicate_check.check_contacts"
KEY_DUPLICATE_CHECK_TG_LINKS = "duplicate_check.check_tg_links"
KEY_DUPLICATE_CHECK_USER_BIO = "duplicate_check.check_user_bio"
KEY_DUPLICATE_CHECK_CONTENT_HASH = "duplicate_check.check_content_hash"
KEY_DUPLICATE_AUTO_REJECT = "duplicate_check.auto_reject_duplicate"
KEY_DUPLICATE_NOTIFY_USER = "duplicate_check.notify_user_duplicate"

KEY_RATE_LIMIT_ENABLED = "rate_limit.enabled"
KEY_RATE_LIMIT_COUNT = "rate_limit.count"
KEY_RATE_LIMIT_WINDOW_HOURS = "rate_limit.window_hours"

# 评分（热更新）
KEY_RATING_ENABLED = "rating.enabled"
KEY_RATING_ALLOW_UPDATE = "rating.allow_update"


DEFAULT_AI_REVIEW_SYSTEM_PROMPT = "你是一个专业的内容审核助手。请严格按照要求的 JSON 格式返回审核结果。"

DEFAULT_AI_REVIEW_POLICY_TEXT = "\n".join([
    "1. 内容必须与「{channel_topic}」主题相关",
    "2. 相关关键词包括：{topic_keywords}",
    "3. 本频道允许主题相关的供需/推广/广告内容；只要与主题相关即可通过",
    "4. 与主题无关的内容（包括无关广告、账号供需等）应判为“无关内容”并拒绝",
    "5. 如果内容模糊或无法判断，设置 requires_manual 为 true，分类为“待定”",
])

DEFAULT_AD_RISK_SYSTEM_PROMPT = "你是专业的内容安全审核助手，只返回 JSON。"

DEFAULT_AD_RISK_PROMPT_TEMPLATE = """你是内容安全审核助手。请对“广告按钮素材”做轻度风险审核，仅识别明确高风险。

素材：
- 按钮文案：{button_text}
- 链接：{button_url}

规则：
- 若涉及“儿童/未成年人性化、引诱、交易”等 -> 拒绝
- 若涉及“恐怖/血腥/暴力细节” -> 拒绝
- 其他内容默认通过（不需要严格审核）

请仅返回 JSON（不要其他文本）：
{
  "passed": true/false,
  "category": "儿童/未成年人|恐怖/血腥|正常",
  "reason": "简短理由"
}"""


@dataclass(frozen=True)
class PaidAdPackage:
    sku_id: str
    credits: int
    amount: Decimal


@dataclass(frozen=True)
class SlotAdPlan:
    sku_id: str
    days: int
    amount: Decimal


_snapshot: Dict[str, str] = {}
_snapshot_loaded_at: float = 0.0


def _bool_from_str(value: Optional[str], fallback: bool) -> bool:
    if value is None:
        return bool(fallback)
    v = str(value).strip().lower()
    if v in ("1", "true", "yes", "y", "on"):
        return True
    if v in ("0", "false", "no", "n", "off"):
        return False
    return bool(fallback)


def _int_from_str(value: Optional[str], fallback: int) -> int:
    if value is None:
        return int(fallback)
    try:
        return int(str(value).strip())
    except Exception:
        return int(fallback)


def _str_from_str(value: Optional[str], fallback: str) -> str:
    v = str(value) if value is not None else ""
    v = v.strip()
    return v if v else str(fallback)


def get_raw(key: str) -> Optional[str]:
    return _snapshot.get(key)


def get_str(key: str, fallback: str) -> str:
    return _str_from_str(get_raw(key), fallback)


def get_bool(key: str, fallback: bool) -> bool:
    return _bool_from_str(get_raw(key), fallback)


def get_int(key: str, fallback: int) -> int:
    return _int_from_str(get_raw(key), fallback)


def _parse_paid_ad_packages_strict(raw: str) -> List[PaidAdPackage]:
    s = (raw or "").strip()
    if not s:
        return []
    parts = [p.strip() for p in s.split(",") if p.strip()]
    packages: List[PaidAdPackage] = []
    for idx, part in enumerate(parts):
        if ":" not in part:
            raise ValueError(f"PAID_AD.PACKAGES 项缺少冒号: {part}")
        credits_str, amount_str = [x.strip() for x in part.split(":", 1)]
        try:
            credits = int(credits_str)
        except (ValueError, TypeError):
            raise ValueError(f"PAID_AD.PACKAGES 次数无效: {part}")
        if credits <= 0:
            raise ValueError(f"PAID_AD.PACKAGES 次数必须>0: {part}")
        try:
            amount = Decimal(amount_str)
        except (InvalidOperation, ValueError, TypeError):
            raise ValueError(f"PAID_AD.PACKAGES 金额无效: {part}")
        if amount <= 0:
            raise ValueError(f"PAID_AD.PACKAGES 金额必须>0: {part}")
        packages.append(PaidAdPackage(sku_id=f"p{idx+1}", credits=credits, amount=amount))
    return packages


def _parse_slot_ad_plans_strict(raw: str) -> List[SlotAdPlan]:
    s = (raw or "").strip()
    if not s:
        return []
    parts = [p.strip() for p in s.split(",") if p.strip()]
    plans: List[SlotAdPlan] = []
    for part in parts:
        if ":" not in part:
            raise ValueError(f"SLOT_AD.PLANS 项缺少冒号: {part}")
        days_str, amount_str = [x.strip() for x in part.split(":", 1)]
        try:
            days = int(days_str)
        except (ValueError, TypeError):
            raise ValueError(f"SLOT_AD.PLANS 天数无效: {part}")
        if days <= 0:
            raise ValueError(f"SLOT_AD.PLANS 天数必须>0: {part}")
        try:
            amount = Decimal(amount_str)
        except (InvalidOperation, ValueError, TypeError):
            raise ValueError(f"SLOT_AD.PLANS 金额无效: {part}")
        if amount <= 0:
            raise ValueError(f"SLOT_AD.PLANS 金额必须>0: {part}")
        plans.append(SlotAdPlan(sku_id=f"d{days}", days=days, amount=amount))
    plans.sort(key=lambda x: int(x.days))
    return plans


def paid_ad_enabled() -> bool:
    return get_bool(KEY_PAID_AD_ENABLED, static.PAID_AD_ENABLED)


def paid_ad_currency() -> str:
    return get_str(KEY_PAID_AD_CURRENCY, static.PAID_AD_CURRENCY)


def paid_ad_publish_prefix() -> str:
    return get_str(KEY_PAID_AD_PUBLISH_PREFIX, static.PAID_AD_PUBLISH_PREFIX)


def paid_ad_packages_raw() -> str:
    return get_str(KEY_PAID_AD_PACKAGES_RAW, static.PAID_AD_PACKAGES_RAW)


def paid_ad_packages() -> List[PaidAdPackage]:
    raw = paid_ad_packages_raw()
    try:
        parsed = _parse_paid_ad_packages_strict(raw)
        if parsed:
            return parsed
    except Exception as e:
        logger.warning(f"运行时 PAID_AD.PACKAGES 配置无效，将回退到静态配置: {e}")
    out: List[PaidAdPackage] = []
    for p in (static.PAID_AD_PACKAGES or []):
        out.append(PaidAdPackage(sku_id=str(p["sku_id"]), credits=int(p["credits"]), amount=p["amount"]))
    return out


def upay_default_type() -> str:
    return get_str(KEY_UPAY_DEFAULT_TYPE, static.UPAY_DEFAULT_TYPE)


def upay_allowed_types() -> List[str]:
    raw = get_str(KEY_UPAY_ALLOWED_TYPES, ",".join(static.UPAY_ALLOWED_TYPES or []))
    types = [t.strip() for t in (raw or "").split(",") if t.strip()]
    return types


def pay_expire_minutes() -> int:
    return get_int(KEY_PAY_EXPIRE_MINUTES, int(static.PAY_EXPIRE_MINUTES))


def slot_ad_enabled() -> bool:
    return get_bool(KEY_SLOT_AD_ENABLED, static.SLOT_AD_ENABLED)


def slot_ad_currency() -> str:
    return get_str(KEY_SLOT_AD_CURRENCY, static.SLOT_AD_CURRENCY)


def slot_ad_plans_raw() -> str:
    return get_str(KEY_SLOT_AD_PLANS_RAW, static.SLOT_AD_PLANS_RAW)


def slot_ad_plans() -> List[SlotAdPlan]:
    raw = slot_ad_plans_raw()
    try:
        parsed = _parse_slot_ad_plans_strict(raw)
        if parsed:
            return parsed
    except Exception as e:
        logger.warning(f"运行时 SLOT_AD.PLANS 配置无效，将回退到静态配置: {e}")
    out: List[SlotAdPlan] = []
    for p in (static.SLOT_AD_PLANS or []):
        out.append(SlotAdPlan(sku_id=str(p["sku_id"]), days=int(p["days"]), amount=p["amount"]))
    return out


def slot_ad_renew_protect_days() -> int:
    return get_int(KEY_SLOT_AD_RENEW_PROTECT_DAYS, int(static.SLOT_AD_RENEW_PROTECT_DAYS))


def slot_ad_button_text_max_len() -> int:
    return get_int(KEY_SLOT_AD_BUTTON_TEXT_MAX_LEN, int(static.SLOT_AD_BUTTON_TEXT_MAX_LEN))


def slot_ad_url_max_len() -> int:
    return get_int(KEY_SLOT_AD_URL_MAX_LEN, int(static.SLOT_AD_URL_MAX_LEN))


def slot_ad_reminder_advance_days() -> int:
    return get_int(KEY_SLOT_AD_REMINDER_ADVANCE_DAYS, int(static.SLOT_AD_REMINDER_ADVANCE_DAYS))


def slot_ad_active_rows_count() -> int:
    """
    定时消息下方展示的“启用行数”（前 N 行）。
    - 运行时可热更新（DB）
    - 回退到 config.ini / 环境变量（静态配置）
    """
    fallback = int(getattr(static, "SLOT_AD_ACTIVE_ROWS_COUNT", 0)) or int(getattr(static, "SLOT_AD_MAX_ROWS", 0))
    return max(0, get_int(KEY_SLOT_AD_ACTIVE_ROWS_COUNT, int(fallback)))

def slot_ad_edit_limit_per_order_per_day() -> int:
    """
    每个 Slot Ads 订单每天允许修改次数（0 表示不限制）。
    """
    raw = get_raw(KEY_SLOT_AD_EDIT_LIMIT_PER_ORDER_PER_DAY)
    if raw is None:
        return 1
    try:
        v = int(str(raw).strip())
    except Exception:
        return 1
    return max(0, min(20, int(v)))


def ai_review_enabled() -> bool:
    return get_bool(KEY_AI_REVIEW_ENABLED, static.AI_REVIEW_ENABLED)


def ai_review_model() -> str:
    return get_str(KEY_AI_REVIEW_MODEL, static.AI_REVIEW_MODEL)


def ai_review_channel_topic() -> str:
    return get_str(KEY_AI_REVIEW_CHANNEL_TOPIC, static.AI_REVIEW_CHANNEL_TOPIC)


def ai_review_topic_keywords_csv() -> str:
    return get_str(KEY_AI_REVIEW_TOPIC_KEYWORDS, static.AI_REVIEW_TOPIC_KEYWORDS)


def ai_review_topic_keywords_list() -> List[str]:
    return [k.strip() for k in ai_review_topic_keywords_csv().split(",") if k.strip()]


def ai_review_strict_mode() -> bool:
    return get_bool(KEY_AI_REVIEW_STRICT_MODE, static.AI_REVIEW_STRICT_MODE)


def ai_review_auto_reject() -> bool:
    return get_bool(KEY_AI_REVIEW_AUTO_REJECT, static.AI_REVIEW_AUTO_REJECT)


def ai_review_fallback_on_error() -> str:
    return get_str(KEY_AI_REVIEW_FALLBACK_ON_ERROR, static.AI_REVIEW_FALLBACK_ON_ERROR)


def ai_review_notify_user() -> bool:
    return get_bool(KEY_AI_REVIEW_NOTIFY_USER, static.AI_REVIEW_NOTIFY_USER)


def ai_review_system_prompt() -> str:
    return get_str(KEY_AI_REVIEW_SYSTEM_PROMPT, DEFAULT_AI_REVIEW_SYSTEM_PROMPT)


def ai_review_policy_text() -> str:
    fallback = DEFAULT_AI_REVIEW_POLICY_TEXT
    v = get_raw(KEY_AI_REVIEW_POLICY_TEXT)
    return (v if v is not None else fallback).strip() or fallback


def render_ai_review_policy_text(*, channel_topic: str, topic_keywords: str) -> str:
    """
    允许在策略文本中使用占位符，避免重复维护：
    - {channel_topic}
    - {topic_keywords}
    """
    policy = ai_review_policy_text()
    return (
        policy
        .replace("{channel_topic}", str(channel_topic))
        .replace("{topic_keywords}", str(topic_keywords))
    )


def ad_risk_system_prompt() -> str:
    return get_str(KEY_AD_RISK_SYSTEM_PROMPT, DEFAULT_AD_RISK_SYSTEM_PROMPT)


def ad_risk_prompt_template() -> str:
    v = get_raw(KEY_AD_RISK_PROMPT_TEMPLATE)
    fallback = DEFAULT_AD_RISK_PROMPT_TEMPLATE
    return (v if v is not None else fallback).strip() or fallback


def render_ad_risk_prompt(*, button_text: str, button_url: str) -> str:
    """
    广告按钮风控 prompt 支持占位符：
    - {button_text}
    - {button_url}
    """
    tpl = ad_risk_prompt_template()
    return (
        tpl
        .replace("{button_text}", str(button_text))
        .replace("{button_url}", str(button_url))
    )


def ai_review_settings_fingerprint() -> str:
    """
    用于缓存隔离：当“审核策略/提示词”变化时，不复用旧缓存。
    """
    parts = [
        "v1",
        str(ai_review_enabled()),
        ai_review_model(),
        ai_review_channel_topic(),
        ai_review_topic_keywords_csv(),
        str(ai_review_strict_mode()),
        str(ai_review_auto_reject()),
        ai_review_fallback_on_error(),
        ai_review_system_prompt(),
        ai_review_policy_text(),
    ]
    return "|".join(parts)


def bot_min_text_length() -> int:
    fallback = int(getattr(static, "MIN_TEXT_LENGTH", 10))
    return max(1, min(4000, get_int(KEY_BOT_MIN_TEXT_LENGTH, int(fallback))))


def bot_max_text_length() -> int:
    fallback = int(getattr(static, "MAX_TEXT_LENGTH", 4000))
    return max(1, min(4000, get_int(KEY_BOT_MAX_TEXT_LENGTH, int(fallback))))


def bot_allowed_tags() -> int:
    fallback = int(getattr(static, "ALLOWED_TAGS", 30))
    return max(0, min(50, get_int(KEY_BOT_ALLOWED_TAGS, int(fallback))))


def bot_allowed_file_types() -> str:
    fallback = str(getattr(static, "ALLOWED_FILE_TYPES", "*"))
    return get_str(KEY_BOT_ALLOWED_FILE_TYPES, fallback)


def bot_show_submitter() -> bool:
    fallback = bool(getattr(static, "SHOW_SUBMITTER", True))
    return get_bool(KEY_BOT_SHOW_SUBMITTER, bool(fallback))


def bot_notify_owner() -> bool:
    fallback = bool(getattr(static, "NOTIFY_OWNER", True))
    return get_bool(KEY_BOT_NOTIFY_OWNER, bool(fallback))


def duplicate_check_enabled() -> bool:
    fallback = bool(getattr(static, "DUPLICATE_CHECK_ENABLED", False))
    return get_bool(KEY_DUPLICATE_CHECK_ENABLED, bool(fallback))


def duplicate_check_window_days() -> int:
    """
    重复投稿检测时间窗口（天）。
    - 运行时可热更新（DB）
    - 回退到 config.ini / 环境变量（静态配置）
    """
    fallback = int(getattr(static, "DUPLICATE_CHECK_WINDOW_DAYS", 7))
    return max(1, get_int(KEY_DUPLICATE_CHECK_WINDOW_DAYS, int(fallback)))


def duplicate_similarity_threshold() -> float:
    fallback = float(getattr(static, "DUPLICATE_SIMILARITY_THRESHOLD", 0.8))
    raw = get_raw(KEY_DUPLICATE_SIMILARITY_THRESHOLD)
    if raw is None:
        return float(fallback)
    try:
        v = float(str(raw).strip())
    except Exception:
        return float(fallback)
    return max(0.0, min(1.0, v))


def duplicate_check_urls() -> bool:
    fallback = bool(getattr(static, "DUPLICATE_CHECK_URLS", True))
    return get_bool(KEY_DUPLICATE_CHECK_URLS, bool(fallback))


def duplicate_check_contacts() -> bool:
    fallback = bool(getattr(static, "DUPLICATE_CHECK_CONTACTS", True))
    return get_bool(KEY_DUPLICATE_CHECK_CONTACTS, bool(fallback))


def duplicate_check_tg_links() -> bool:
    fallback = bool(getattr(static, "DUPLICATE_CHECK_TG_LINKS", True))
    return get_bool(KEY_DUPLICATE_CHECK_TG_LINKS, bool(fallback))


def duplicate_check_user_bio() -> bool:
    fallback = bool(getattr(static, "DUPLICATE_CHECK_USER_BIO", True))
    return get_bool(KEY_DUPLICATE_CHECK_USER_BIO, bool(fallback))


def duplicate_check_content_hash() -> bool:
    fallback = bool(getattr(static, "DUPLICATE_CHECK_CONTENT_HASH", True))
    return get_bool(KEY_DUPLICATE_CHECK_CONTENT_HASH, bool(fallback))


def duplicate_auto_reject_duplicate() -> bool:
    fallback = bool(getattr(static, "DUPLICATE_AUTO_REJECT", True))
    return get_bool(KEY_DUPLICATE_AUTO_REJECT, bool(fallback))


def duplicate_notify_user_duplicate() -> bool:
    fallback = bool(getattr(static, "DUPLICATE_NOTIFY_USER", True))
    return get_bool(KEY_DUPLICATE_NOTIFY_USER, bool(fallback))


def rate_limit_enabled() -> bool:
    fallback = bool(getattr(static, "RATE_LIMIT_ENABLED", True))
    return get_bool(KEY_RATE_LIMIT_ENABLED, bool(fallback))


def rate_limit_count() -> int:
    fallback = int(getattr(static, "RATE_LIMIT_COUNT", 3))
    return max(1, min(20, get_int(KEY_RATE_LIMIT_COUNT, int(fallback))))


def rate_limit_window_hours() -> int:
    fallback = int(getattr(static, "RATE_LIMIT_WINDOW_HOURS", 24))
    return max(1, min(168, get_int(KEY_RATE_LIMIT_WINDOW_HOURS, int(fallback))))


def rating_enabled() -> bool:
    fallback = bool(getattr(static, "RATING_ENABLED", True))
    return get_bool(KEY_RATING_ENABLED, bool(fallback))


def rating_allow_update() -> bool:
    fallback = bool(getattr(static, "RATING_ALLOW_UPDATE", True))
    return get_bool(KEY_RATING_ALLOW_UPDATE, bool(fallback))


async def refresh() -> None:
    """
    从 DB 载入运行时配置快照。
    """
    global _snapshot, _snapshot_loaded_at
    try:
        async with get_db() as conn:
            cursor = await conn.cursor()
            await cursor.execute("SELECT key, value FROM runtime_settings")
            rows = await cursor.fetchall()
            _snapshot = {str(r["key"]): (str(r["value"]) if r["value"] is not None else "") for r in rows}
            _snapshot_loaded_at = time.time()
            logger.info(f"运行时配置已加载: {len(_snapshot)} 项")
    except Exception as e:
        logger.warning(f"加载运行时配置失败（将回退为静态配置）: {e}", exc_info=True)
        _snapshot = {}
        _snapshot_loaded_at = time.time()


async def set_many(*, values: Dict[str, str]) -> None:
    """
    原子写入多项配置，并刷新快照。
    """
    if not values:
        return
    now = time.time()
    async with get_db() as conn:
        cursor = await conn.cursor()
        for k, v in values.items():
            await cursor.execute(
                """
                INSERT INTO runtime_settings(key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (str(k), str(v), now),
            )
        await conn.commit()
    await refresh()


async def unset_many(*, keys: List[str]) -> None:
    """
    删除多项运行时配置（回退到静态配置），并刷新快照。
    """
    if not keys:
        return
    async with get_db() as conn:
        cursor = await conn.cursor()
        for k in keys:
            await cursor.execute("DELETE FROM runtime_settings WHERE key = ?", (str(k),))
        await conn.commit()
    await refresh()


def validate_paid_ad_packages_raw(raw: str) -> None:
    _parse_paid_ad_packages_strict(raw)


def validate_slot_ad_plans_raw(raw: str) -> None:
    _parse_slot_ad_plans_strict(raw)

def validate_slot_ad_edit_limit_per_order_per_day(limit: int) -> None:
    try:
        v = int(limit)
    except Exception:
        raise ValueError("SLOT_AD.EDIT_LIMIT_PER_ORDER_PER_DAY 必须是整数")
    if v < 0:
        raise ValueError("SLOT_AD.EDIT_LIMIT_PER_ORDER_PER_DAY 不能为负数")
    if v > 20:
        raise ValueError("SLOT_AD.EDIT_LIMIT_PER_ORDER_PER_DAY 不能大于 20")


def validate_duplicate_check_window_days(days: int) -> None:
    try:
        value = int(days)
    except Exception:
        raise ValueError("DUPLICATE_CHECK_WINDOW_DAYS 必须是整数")
    if value <= 0:
        raise ValueError("DUPLICATE_CHECK_WINDOW_DAYS 必须大于 0")
    if value > 3650:
        raise ValueError("DUPLICATE_CHECK_WINDOW_DAYS 过大（建议不超过 3650）")


def validate_bot_text_length(*, min_len: int, max_len: int) -> None:
    try:
        min_value = int(min_len)
        max_value = int(max_len)
    except Exception:
        raise ValueError("字数限制必须是整数")
    if min_value <= 0 or min_value > 4000:
        raise ValueError("MIN_TEXT_LENGTH 范围应为 1~4000")
    if max_value <= 0 or max_value > 4000:
        raise ValueError("MAX_TEXT_LENGTH 范围应为 1~4000")
    if max_value < min_value:
        raise ValueError("MAX_TEXT_LENGTH 不能小于 MIN_TEXT_LENGTH")


def validate_bot_allowed_tags(allowed_tags: int) -> None:
    try:
        value = int(allowed_tags)
    except Exception:
        raise ValueError("ALLOWED_TAGS 必须是整数")
    if value < 0 or value > 50:
        raise ValueError("ALLOWED_TAGS 范围应为 0~50")


def validate_bot_allowed_file_types(raw: str) -> None:
    s = (raw or "").strip()
    if not s:
        return
    if len(s) > 512:
        raise ValueError("ALLOWED_FILE_TYPES 过长（建议不超过 512 字符）")
    from utils.file_validator import FileTypeValidator
    FileTypeValidator(s)


def validate_duplicate_similarity_threshold(value: float) -> None:
    try:
        v = float(value)
    except Exception:
        raise ValueError("SIMILARITY_THRESHOLD 必须是数字")
    if v < 0.0 or v > 1.0:
        raise ValueError("SIMILARITY_THRESHOLD 范围应为 0~1")


def validate_rate_limit(*, count: int, window_hours: int) -> None:
    try:
        c = int(count)
        w = int(window_hours)
    except Exception:
        raise ValueError("频率限制参数必须是整数")
    if c <= 0 or c > 20:
        raise ValueError("RATE_LIMIT_COUNT 范围应为 1~20")
    if w <= 0 or w > 168:
        raise ValueError("RATE_LIMIT_WINDOW_HOURS 范围应为 1~168")
