"""
AI 内容审核模块
使用 OpenAI 兼容 API 自动审核投稿内容
"""
import json
import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Optional, Dict, Any

from database.db_manager import get_db
from config.settings import (
    AI_REVIEW_API_BASE,
    AI_REVIEW_API_KEY,
    AI_REVIEW_TIMEOUT,
    AI_REVIEW_MAX_RETRIES,
    AI_REVIEW_CACHE_ENABLED,
    AI_REVIEW_CACHE_TTL_HOURS,
)
from utils import runtime_settings

logger = logging.getLogger(__name__)


@dataclass
class ReviewResult:
    """审核结果"""
    approved: bool = False
    confidence: float = 0.0
    reason: str = ""
    category: str = ""
    requires_manual: bool = False
    error: Optional[str] = None
    cached: bool = False

    def to_dict(self) -> dict:
        return {
            'approved': self.approved,
            'confidence': self.confidence,
            'reason': self.reason,
            'category': self.category,
            'requires_manual': self.requires_manual,
            'error': self.error,
            'cached': self.cached
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'ReviewResult':
        return cls(
            approved=data.get('approved', False),
            confidence=data.get('confidence', 0.0),
            reason=data.get('reason', ''),
            category=data.get('category', ''),
            requires_manual=data.get('requires_manual', False),
            error=data.get('error'),
            cached=data.get('cached', False)
        )


class AIReviewer:
    """AI 内容审核器（OpenAI SDK 兼容）"""

    def __init__(self):
        self.api_base = AI_REVIEW_API_BASE.rstrip('/')
        self.api_key = AI_REVIEW_API_KEY
        self.timeout = AI_REVIEW_TIMEOUT
        self.max_retries = AI_REVIEW_MAX_RETRIES
        self._client = None

    def _get_client(self):
        """懒加载 OpenAI 客户端"""
        if self._client is None:
            try:
                from openai import AsyncOpenAI
                self._client = AsyncOpenAI(
                    api_key=self.api_key,
                    base_url=self.api_base,
                    timeout=self.timeout
                )
            except ImportError:
                logger.error("openai 库未安装，请运行: pip install openai")
                raise
        return self._client

    async def review(self, submission: Dict[str, Any]) -> ReviewResult:
        """
        审核投稿内容

        Args:
            submission: {
                'text_content': str,  # 纯文本内容
                'tags': str,          # 标签
                'link': str,          # 链接
                'title': str,         # 标题
                'note': str,          # 简介
                'username': str,      # 用户名
            }

        Returns:
            ReviewResult: 审核结果
        """
        if not runtime_settings.ai_review_enabled():
            return ReviewResult(approved=True, confidence=1.0, reason="AI 审核未启用")

        if not self.api_key:
            logger.warning("AI_REVIEW_API_KEY 未配置，跳过 AI 审核")
            return self._handle_fallback("API Key 未配置")

        # 构建内容用于缓存查询
        content_for_hash = self._build_content_string(submission)
        content_hash = self._compute_hash(content_for_hash)

        # 检查缓存
        if AI_REVIEW_CACHE_ENABLED:
            cached_result = await self._get_cached_result(content_hash)
            if cached_result:
                cached_result.cached = True
                logger.info(f"使用缓存的审核结果: hash={content_hash[:8]}...")
                return cached_result

        # 调用 AI API
        for attempt in range(self.max_retries + 1):
            try:
                result = await self._call_api(submission)

                # 缓存结果
                if AI_REVIEW_CACHE_ENABLED and result.error is None:
                    await self._cache_result(content_hash, result)

                return result

            except Exception as e:
                logger.error(f"AI 审核调用失败 (尝试 {attempt + 1}/{self.max_retries + 1}): {e}")
                if attempt >= self.max_retries:
                    return self._handle_fallback(str(e))

        return self._handle_fallback("未知错误")

    async def _call_api(self, submission: Dict[str, Any]) -> ReviewResult:
        """调用 AI API 进行审核"""
        prompt = self._build_prompt(submission)

        client = self._get_client()

        response = await client.chat.completions.create(
            model=runtime_settings.ai_review_model(),
            messages=[
                {
                    "role": "system",
                    "content": runtime_settings.ai_review_system_prompt()
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            temperature=0.1,  # 低温度以获得更稳定的结果
            max_tokens=500
        )

        # 解析响应
        content = response.choices[0].message.content.strip()

        # 尝试提取 JSON
        result = self._parse_response(content)

        logger.info(f"AI 审核完成: approved={result.approved}, "
                   f"confidence={result.confidence:.2f}, category={result.category}")

        return result

    def _build_prompt(self, submission: Dict[str, Any]) -> str:
        """构建审核 Prompt"""
        text_content = submission.get('text_content', '') or ''
        tags = submission.get('tags', '') or ''
        link = submission.get('link', '') or ''
        title = submission.get('title', '') or ''
        note = submission.get('note', '') or ''

        # 合并所有内容
        all_content = f"{title}\n{text_content}\n{note}".strip()

        strict_note = ""
        channel_topic = runtime_settings.ai_review_channel_topic()
        topic_keywords_csv = runtime_settings.ai_review_topic_keywords_csv()
        if runtime_settings.ai_review_strict_mode():
            strict_note = "注意：请使用严格模式审核，内容必须高度相关才能通过。"

        policy_text = runtime_settings.render_ai_review_policy_text(
            channel_topic=channel_topic,
            topic_keywords=topic_keywords_csv,
        )

        prompt = f"""你是一个 Telegram 频道投稿审核助手。该频道主题是：{channel_topic}

请审核以下投稿内容是否与频道主题相关：

---
投稿内容：
{all_content}

标签：{tags}
链接：{link}
---

审核标准：
{policy_text}
{strict_note}

请以 JSON 格式返回审核结果（只返回 JSON，不要其他内容）：
{{
    "approved": true或false,
    "confidence": 0.0到1.0之间的数字,
    "reason": "简短的审核理由",
    "category": "内容分类（仅可填：相关/无关内容/待定）",
    "requires_manual": true或false
}}"""

        return prompt

    def _parse_response(self, content: str) -> ReviewResult:
        """解析 AI 响应"""
        try:
            # 尝试提取 JSON 块
            if '```json' in content:
                start = content.find('```json') + 7
                end = content.find('```', start)
                content = content[start:end].strip()
            elif '```' in content:
                start = content.find('```') + 3
                end = content.find('```', start)
                content = content[start:end].strip()

            # 清理可能的多余字符
            content = content.strip()
            if content.startswith('{') and content.endswith('}'):
                data = json.loads(content)

                return ReviewResult(
                    approved=data.get('approved', False),
                    confidence=float(data.get('confidence', 0.5)),
                    reason=data.get('reason', ''),
                    category=data.get('category', ''),
                    requires_manual=data.get('requires_manual', False)
                )

        except json.JSONDecodeError as e:
            logger.error(f"解析 AI 响应失败: {e}, content={content[:200]}")

        # 解析失败，使用默认值
        return ReviewResult(
            approved=False,
            confidence=0.5,
            reason="无法解析 AI 响应",
            category="待定",
            requires_manual=True
        )

    def _handle_fallback(self, error: str) -> ReviewResult:
        """处理错误时的降级策略"""
        fallback = runtime_settings.ai_review_fallback_on_error().lower()

        if fallback == 'pass':
            return ReviewResult(
                approved=True,
                confidence=0.5,
                reason=f"AI 审核失败，自动通过: {error}",
                category="自动通过",
                requires_manual=False,
                error=error
            )
        elif fallback == 'reject':
            return ReviewResult(
                approved=False,
                confidence=0.5,
                reason=f"AI 审核失败，自动拒绝: {error}",
                category="自动拒绝",
                requires_manual=False,
                error=error
            )
        else:  # manual
            return ReviewResult(
                approved=False,
                confidence=0.0,
                reason=f"AI 审核失败，需人工审核: {error}",
                category="待人工审核",
                requires_manual=True,
                error=error
            )

    def _build_content_string(self, submission: Dict[str, Any]) -> str:
        """构建用于计算哈希的内容字符串"""
        parts = [
            runtime_settings.ai_review_settings_fingerprint(),
            submission.get('text_content', '') or '',
            submission.get('tags', '') or '',
            submission.get('title', '') or '',
            submission.get('note', '') or ''
        ]
        return '|'.join(parts).lower().strip()

    def _compute_hash(self, content: str) -> str:
        """计算内容哈希"""
        return hashlib.sha256(content.encode('utf-8')).hexdigest()

    async def _get_cached_result(self, content_hash: str) -> Optional[ReviewResult]:
        """从缓存获取审核结果"""
        try:
            async with get_db() as conn:
                cursor = await conn.cursor()
                await cursor.execute('''
                    SELECT approved, confidence, reason, category, requires_manual
                    FROM ai_review_cache
                    WHERE content_hash = ? AND expires_at > ?
                ''', (content_hash, time.time()))

                row = await cursor.fetchone()
                if row:
                    return ReviewResult(
                        approved=bool(row['approved']),
                        confidence=row['confidence'],
                        reason=row['reason'],
                        category=row['category'],
                        requires_manual=bool(row['requires_manual'])
                    )

        except Exception as e:
            logger.error(f"获取缓存失败: {e}")

        return None

    async def _cache_result(self, content_hash: str, result: ReviewResult):
        """缓存审核结果"""
        try:
            expires_at = time.time() + (AI_REVIEW_CACHE_TTL_HOURS * 3600)

            async with get_db() as conn:
                cursor = await conn.cursor()
                await cursor.execute('''
                    INSERT OR REPLACE INTO ai_review_cache
                    (content_hash, approved, confidence, reason, category, requires_manual, expires_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (
                    content_hash,
                    1 if result.approved else 0,
                    result.confidence,
                    result.reason,
                    result.category,
                    1 if result.requires_manual else 0,
                    expires_at
                ))
                await conn.commit()

        except Exception as e:
            logger.error(f"缓存审核结果失败: {e}")

    async def cleanup_expired_cache(self) -> int:
        """清理过期的缓存"""
        try:
            async with get_db() as conn:
                cursor = await conn.cursor()
                await cursor.execute('''
                    DELETE FROM ai_review_cache WHERE expires_at < ?
                ''', (time.time(),))
                deleted = cursor.rowcount
                await conn.commit()
                if deleted:
                    logger.info(f"清理了 {deleted} 条过期的 AI 审核缓存")
                return deleted

        except Exception as e:
            logger.error(f"清理过期缓存失败: {e}")
            return 0

    def should_auto_approve(self, result: ReviewResult) -> bool:
        """判断是否应该自动通过"""
        return result.approved and result.confidence >= 0.8 and not result.requires_manual

    def should_auto_reject(self, result: ReviewResult) -> bool:
        """判断是否应该自动拒绝"""
        if not runtime_settings.ai_review_auto_reject():
            return False
        if self._is_off_topic_category(result.category):
            return True
        return not result.approved and result.confidence >= 0.8 and not result.requires_manual

    def _is_off_topic_category(self, category: str) -> bool:
        """判断分类是否为无关内容"""
        if not category:
            return False
        normalized = str(category).strip().lower()
        if not normalized:
            return False
        return (
            '无关' in normalized
            or 'irrelevant' in normalized
            or 'off-topic' in normalized
            or 'off topic' in normalized
        )

    def is_off_topic_category(self, category: str) -> bool:
        """对外暴露的无关分类判断（避免外部直接调用内部实现）"""
        return self._is_off_topic_category(category)

    def should_manual_review(self, result: ReviewResult) -> bool:
        """判断是否需要人工审核"""
        return result.requires_manual or result.confidence < 0.8


# 全局实例
_reviewer = None


def get_ai_reviewer() -> AIReviewer:
    """获取 AI 审核器单例"""
    global _reviewer
    if _reviewer is None:
        _reviewer = AIReviewer()
    return _reviewer
