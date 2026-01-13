"""
广告素材轻度风控审核（儿童/恐怖等明确高风险）

原则：
- 默认宽松：仅拦截“明确高风险”
- AI 审核可用则优先使用；失败则降级为关键词启发式
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

from config.settings import (
    AI_REVIEW_API_BASE,
    AI_REVIEW_API_KEY,
    AI_REVIEW_ENABLED,
    AI_REVIEW_MODEL,
    AI_REVIEW_TIMEOUT,
)
from utils import runtime_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AdRiskReviewResult:
    passed: bool
    category: str
    reason: str
    raw: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": bool(self.passed),
            "category": str(self.category),
            "reason": str(self.reason),
            "raw": self.raw,
        }


def _keyword_fallback(text: str) -> AdRiskReviewResult:
    s = (text or "").lower()
    child_keywords = [
        "未成年", "儿童", "幼女", "萝莉", "小学生", "初中生", "高中生", "未成年人",
    ]
    horror_keywords = [
        "恐怖", "血腥", "虐杀", "尸体", "自杀", "斩首", "爆炸", "枪杀",
    ]
    if any(k.lower() in s for k in child_keywords):
        return AdRiskReviewResult(passed=False, category="儿童/未成年人", reason="命中儿童/未成年人高风险关键词")
    if any(k.lower() in s for k in horror_keywords):
        return AdRiskReviewResult(passed=False, category="恐怖/血腥", reason="命中恐怖/血腥高风险关键词")
    return AdRiskReviewResult(passed=True, category="正常", reason="未命中高风险关键词")


async def review_ad_risk(*, button_text: str, button_url: str) -> AdRiskReviewResult:
    """
    返回 passed=True 表示可继续；passed=False 表示拒绝。
    """
    merged = f"{button_text}\n{button_url}".strip()
    # 测试环境：避免任何外部网络调用，强制走本地启发式降级
    if str(os.getenv("TESTING") or "").strip().lower() in ("1", "true", "yes"):
        return _keyword_fallback(merged)
    if not AI_REVIEW_ENABLED or not AI_REVIEW_API_KEY:
        return _keyword_fallback(merged)

    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(
            api_key=AI_REVIEW_API_KEY,
            base_url=(AI_REVIEW_API_BASE or "").rstrip("/"),
            timeout=int(AI_REVIEW_TIMEOUT),
        )

        prompt = runtime_settings.render_ad_risk_prompt(button_text=button_text, button_url=button_url)

        resp = await client.chat.completions.create(
            model=AI_REVIEW_MODEL,
            messages=[
                {"role": "system", "content": runtime_settings.ad_risk_system_prompt()},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            max_tokens=200,
        )
        content = (resp.choices[0].message.content or "").strip()
        data = json.loads(content)
        passed = bool(data.get("passed", False))
        category = str(data.get("category", "") or "").strip() or ("正常" if passed else "待定")
        reason = str(data.get("reason", "") or "").strip() or ("通过" if passed else "拒绝")
        return AdRiskReviewResult(passed=passed, category=category, reason=reason, raw=data)
    except Exception as e:
        logger.warning(f"广告风控 AI 审核失败，将降级为关键词策略: {e}")
        return _keyword_fallback(merged)
