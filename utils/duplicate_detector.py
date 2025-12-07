"""
重复投稿检测模块
基于多维特征识别重复投稿行为
"""
import json
import logging
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple
from datetime import datetime

from database.db_manager import get_db
from utils.feature_extractor import (
    SubmissionFingerprint,
    get_feature_extractor,
    FINGERPRINT_VERSION
)
from config.settings import (
    DUPLICATE_CHECK_ENABLED,
    DUPLICATE_CHECK_WINDOW_DAYS,
    DUPLICATE_SIMILARITY_THRESHOLD,
    DUPLICATE_CHECK_USER_ID,
    DUPLICATE_CHECK_URLS,
    DUPLICATE_CHECK_CONTACTS,
    DUPLICATE_CHECK_TG_LINKS,
    DUPLICATE_CHECK_USER_BIO,
    DUPLICATE_CHECK_CONTENT_HASH,
    RATE_LIMIT_ENABLED,
    RATE_LIMIT_COUNT,
    RATE_LIMIT_WINDOW_HOURS
)

logger = logging.getLogger(__name__)


@dataclass
class DuplicateResult:
    """重复检测结果"""
    is_duplicate: bool = False
    duplicate_type: str = ""  # exact/fuzzy/related/rate_limit
    matched_features: List[Tuple[str, str]] = None  # [(feature_type, feature_value), ...]
    similarity_score: float = 0.0
    original_fingerprint_id: int = 0
    original_submit_time: float = 0.0
    message: str = ""

    def __post_init__(self):
        if self.matched_features is None:
            self.matched_features = []


class DuplicateDetector:
    """重复投稿检测器"""

    def __init__(self):
        self.check_window = DUPLICATE_CHECK_WINDOW_DAYS * 86400  # 转换为秒
        self.threshold = DUPLICATE_SIMILARITY_THRESHOLD
        self.extractor = get_feature_extractor()

    async def check(self, fingerprint: SubmissionFingerprint) -> DuplicateResult:
        """
        检测是否为重复投稿

        Args:
            fingerprint: 投稿指纹

        Returns:
            DuplicateResult: 检测结果
        """
        if not DUPLICATE_CHECK_ENABLED:
            return DuplicateResult(is_duplicate=False)

        cutoff_time = time.time() - self.check_window

        # 1. 检查频率限制
        if RATE_LIMIT_ENABLED:
            rate_result = await self._check_rate_limit(fingerprint.user_id)
            if rate_result.is_duplicate:
                return rate_result

        # 2. 精确匹配检测
        exact_result = await self._check_exact_matches(fingerprint, cutoff_time)
        if exact_result.is_duplicate:
            return exact_result

        # 3. 模糊匹配检测（内容相似度）
        if DUPLICATE_CHECK_CONTENT_HASH:
            fuzzy_result = await self._check_fuzzy_matches(fingerprint, cutoff_time)
            if fuzzy_result.is_duplicate:
                return fuzzy_result

        # 4. 关联检测（用户签名特征）
        if DUPLICATE_CHECK_USER_BIO:
            related_result = await self._check_related_submissions(fingerprint, cutoff_time)
            if related_result.is_duplicate:
                return related_result

        return DuplicateResult(is_duplicate=False)

    async def _check_rate_limit(self, user_id: int) -> DuplicateResult:
        """检查投稿频率限制"""
        window_seconds = RATE_LIMIT_WINDOW_HOURS * 3600
        cutoff_time = time.time() - window_seconds

        try:
            async with get_db() as conn:
                cursor = await conn.cursor()
                await cursor.execute('''
                    SELECT COUNT(*) as count FROM submission_fingerprints
                    WHERE user_id = ? AND submit_time > ? AND status = 'approved'
                ''', (user_id, cutoff_time))
                row = await cursor.fetchone()
                count = row['count'] if row else 0

                if count >= RATE_LIMIT_COUNT:
                    logger.info(f"用户 {user_id} 触发频率限制: {count}/{RATE_LIMIT_COUNT} "
                              f"(窗口: {RATE_LIMIT_WINDOW_HOURS}小时)")
                    return DuplicateResult(
                        is_duplicate=True,
                        duplicate_type='rate_limit',
                        matched_features=[],
                        similarity_score=1.0,
                        message=f"您在 {RATE_LIMIT_WINDOW_HOURS} 小时内已投稿 {count} 次，已达到上限 {RATE_LIMIT_COUNT} 次"
                    )
        except Exception as e:
            logger.error(f"检查频率限制失败: {e}")

        return DuplicateResult(is_duplicate=False)

    async def _check_exact_matches(
        self,
        fingerprint: SubmissionFingerprint,
        cutoff_time: float
    ) -> DuplicateResult:
        """
        精确匹配检测

        检查以下特征的完全匹配：
        - URL
        - Telegram 链接/用户名
        - 电话号码
        - 邮箱地址
        """
        matched_features = []

        try:
            async with get_db() as conn:
                cursor = await conn.cursor()

                # 获取时间窗口内的所有特征
                await cursor.execute('''
                    SELECT ff.feature_type, ff.feature_value, sf.id, sf.submit_time
                    FROM fingerprint_features ff
                    JOIN submission_fingerprints sf ON ff.fingerprint_id = sf.id
                    WHERE sf.submit_time > ? AND sf.status = 'approved'
                ''', (cutoff_time,))

                existing_features = {}
                async for row in cursor:
                    key = (row['feature_type'], row['feature_value'])
                    existing_features[key] = (row['id'], row['submit_time'])

                # 检查 URL 匹配
                if DUPLICATE_CHECK_URLS:
                    for url in fingerprint.urls:
                        key = ('url', url)
                        if key in existing_features:
                            matched_features.append(key)

                # 检查 Telegram 链接匹配
                if DUPLICATE_CHECK_TG_LINKS:
                    for tg_link in fingerprint.tg_links:
                        key = ('tg_link', tg_link)
                        if key in existing_features:
                            matched_features.append(key)

                    for tg_user in fingerprint.tg_usernames:
                        key = ('tg_username', tg_user)
                        if key in existing_features:
                            matched_features.append(key)

                # 检查联系方式匹配
                if DUPLICATE_CHECK_CONTACTS:
                    for phone in fingerprint.phone_numbers:
                        key = ('phone', phone)
                        if key in existing_features:
                            matched_features.append(key)

                    for email in fingerprint.emails:
                        key = ('email', email)
                        if key in existing_features:
                            matched_features.append(key)

                if matched_features:
                    # 获取原始投稿信息
                    first_match = matched_features[0]
                    fp_id, submit_time = existing_features[first_match]

                    logger.info(f"检测到精确匹配: user_id={fingerprint.user_id}, "
                              f"matched={len(matched_features)} features")

                    return DuplicateResult(
                        is_duplicate=True,
                        duplicate_type='exact',
                        matched_features=matched_features,
                        similarity_score=1.0,
                        original_fingerprint_id=fp_id,
                        original_submit_time=submit_time,
                        message=self._build_duplicate_message(matched_features, submit_time)
                    )

        except Exception as e:
            logger.error(f"精确匹配检测失败: {e}")

        return DuplicateResult(is_duplicate=False)

    async def _check_fuzzy_matches(
        self,
        fingerprint: SubmissionFingerprint,
        cutoff_time: float
    ) -> DuplicateResult:
        """
        模糊匹配检测

        基于内容 SimHash 比较相似度
        """
        if not fingerprint.content_hash:
            return DuplicateResult(is_duplicate=False)

        try:
            async with get_db() as conn:
                cursor = await conn.cursor()

                # 获取时间窗口内的所有内容哈希
                await cursor.execute('''
                    SELECT id, content_hash, submit_time, user_id
                    FROM submission_fingerprints
                    WHERE submit_time > ? AND status = 'approved' AND content_hash IS NOT NULL
                ''', (cutoff_time,))

                async for row in cursor:
                    if not row['content_hash']:
                        continue

                    # 计算汉明距离
                    distance = self.extractor.compute_simhash_distance(
                        fingerprint.content_hash,
                        row['content_hash']
                    )

                    # 距离越小越相似，64位哈希最大距离为64
                    # 距离 <= 3 通常认为是相似内容
                    similarity = 1 - (distance / 64)

                    if similarity >= self.threshold:
                        logger.info(f"检测到模糊匹配: user_id={fingerprint.user_id}, "
                                  f"similarity={similarity:.2f}, distance={distance}")

                        return DuplicateResult(
                            is_duplicate=True,
                            duplicate_type='fuzzy',
                            matched_features=[('content_similarity', f'{similarity:.2%}')],
                            similarity_score=similarity,
                            original_fingerprint_id=row['id'],
                            original_submit_time=row['submit_time'],
                            message=f"检测到与历史投稿内容相似度达 {similarity:.0%}"
                        )

        except Exception as e:
            logger.error(f"模糊匹配检测失败: {e}")

        return DuplicateResult(is_duplicate=False)

    async def _check_related_submissions(
        self,
        fingerprint: SubmissionFingerprint,
        cutoff_time: float
    ) -> DuplicateResult:
        """
        关联检测

        检查用户签名中的特征是否与历史投稿匹配
        """
        bio_features = []
        bio_features.extend([('bio_url', url) for url in fingerprint.bio_urls])
        bio_features.extend([('bio_tg_link', link) for link in fingerprint.bio_tg_links])
        bio_features.extend([('bio_contact', contact) for contact in fingerprint.bio_contacts])

        if not bio_features:
            return DuplicateResult(is_duplicate=False)

        matched = []

        try:
            async with get_db() as conn:
                cursor = await conn.cursor()

                for feature_type, feature_value in bio_features:
                    # 检查是否在历史投稿的内容特征中出现
                    # 将 bio 特征与内容特征对比
                    content_type = feature_type.replace('bio_', '')

                    await cursor.execute('''
                        SELECT sf.id, sf.submit_time
                        FROM fingerprint_features ff
                        JOIN submission_fingerprints sf ON ff.fingerprint_id = sf.id
                        WHERE ff.feature_type = ? AND ff.feature_value = ?
                        AND sf.submit_time > ? AND sf.status = 'approved'
                        AND sf.user_id != ?
                        LIMIT 1
                    ''', (content_type, feature_value, cutoff_time, fingerprint.user_id))

                    row = await cursor.fetchone()
                    if row:
                        matched.append((feature_type, feature_value))

                if matched:
                    logger.info(f"检测到关联匹配: user_id={fingerprint.user_id}, "
                              f"bio_features={len(matched)}")

                    return DuplicateResult(
                        is_duplicate=True,
                        duplicate_type='related',
                        matched_features=matched,
                        similarity_score=0.9,
                        message="您的个人签名中的联系方式与近期其他投稿重复"
                    )

        except Exception as e:
            logger.error(f"关联检测失败: {e}")

        return DuplicateResult(is_duplicate=False)

    def _build_duplicate_message(
        self,
        matched_features: List[Tuple[str, str]],
        original_time: float
    ) -> str:
        """构建重复检测消息"""
        from datetime import datetime

        time_str = datetime.fromtimestamp(original_time).strftime('%Y-%m-%d %H:%M')

        feature_names = {
            'url': 'URL',
            'tg_link': 'Telegram 链接',
            'tg_username': 'Telegram 用户名',
            'phone': '电话号码',
            'email': '邮箱地址'
        }

        feature_list = []
        for ftype, fvalue in matched_features[:3]:  # 最多显示3个
            name = feature_names.get(ftype, ftype)
            feature_list.append(f"• {name}: {fvalue[:30]}...")

        msg = f"检测到与 {time_str} 的投稿存在以下重复特征：\n"
        msg += "\n".join(feature_list)

        if len(matched_features) > 3:
            msg += f"\n...等共 {len(matched_features)} 处重复"

        return msg

    async def save_fingerprint(
        self,
        fingerprint: SubmissionFingerprint,
        status: str = 'approved',
        submission_id: Optional[int] = None
    ) -> int:
        """
        保存投稿指纹到数据库

        Args:
            fingerprint: 投稿指纹
            status: 状态 (pending/approved/rejected)
            submission_id: 关联的投稿ID

        Returns:
            int: 指纹记录ID
        """
        try:
            async with get_db() as conn:
                cursor = await conn.cursor()

                # 插入指纹记录
                await cursor.execute('''
                    INSERT INTO submission_fingerprints
                    (user_id, username, urls, tg_usernames, tg_links,
                     phone_numbers, emails, bio_features, content_hash,
                     content_length, submit_time, submission_id, status, fingerprint_version)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    fingerprint.user_id,
                    fingerprint.username,
                    json.dumps(fingerprint.urls),
                    json.dumps(fingerprint.tg_usernames),
                    json.dumps(fingerprint.tg_links),
                    json.dumps(fingerprint.phone_numbers),
                    json.dumps(fingerprint.emails),
                    json.dumps({
                        'urls': fingerprint.bio_urls,
                        'tg_links': fingerprint.bio_tg_links,
                        'contacts': fingerprint.bio_contacts
                    }),
                    fingerprint.content_hash,
                    fingerprint.content_length,
                    fingerprint.submit_time,
                    submission_id,
                    status,
                    fingerprint.fingerprint_version
                ))

                fingerprint_id = cursor.lastrowid

                # 插入特征索引
                features = fingerprint.get_all_features()
                for feature_type, feature_value in features:
                    await cursor.execute('''
                        INSERT INTO fingerprint_features
                        (fingerprint_id, feature_type, feature_value)
                        VALUES (?, ?, ?)
                    ''', (fingerprint_id, feature_type, feature_value))

                await conn.commit()
                logger.info(f"保存指纹成功: id={fingerprint_id}, user_id={fingerprint.user_id}, "
                          f"features={len(features)}")

                return fingerprint_id

        except Exception as e:
            logger.error(f"保存指纹失败: {e}")
            return 0

    async def cleanup_expired_fingerprints(self) -> int:
        """
        清理过期的指纹记录

        Returns:
            int: 清理的记录数
        """
        cutoff_time = time.time() - self.check_window

        try:
            async with get_db() as conn:
                cursor = await conn.cursor()

                # 先获取要删除的指纹ID
                await cursor.execute('''
                    SELECT id FROM submission_fingerprints
                    WHERE submit_time < ?
                ''', (cutoff_time,))

                ids = [row['id'] async for row in cursor]

                if ids:
                    # 删除特征索引
                    placeholders = ','.join('?' * len(ids))
                    await cursor.execute(f'''
                        DELETE FROM fingerprint_features
                        WHERE fingerprint_id IN ({placeholders})
                    ''', ids)

                    # 删除指纹记录
                    await cursor.execute(f'''
                        DELETE FROM submission_fingerprints
                        WHERE id IN ({placeholders})
                    ''', ids)

                    await conn.commit()
                    logger.info(f"清理了 {len(ids)} 条过期指纹记录")
                    return len(ids)

        except Exception as e:
            logger.error(f"清理过期指纹失败: {e}")

        return 0


# 全局实例
_detector = None


def get_duplicate_detector() -> DuplicateDetector:
    """获取重复检测器单例"""
    global _detector
    if _detector is None:
        _detector = DuplicateDetector()
    return _detector
