"""
评分服务模块

负责：
- 从投稿数据中提取特征并归类到评分实体（rating_subjects）
- 维护评分实体的标识集合（rating_subject_identifiers）
- 为消息附加评分键盘
"""
import logging
from typing import Dict, Any, List, Optional, Tuple
from urllib.parse import urlparse
from datetime import datetime

from config.settings import CHANNEL_ID, RATING_ENABLED
from database.db_manager import get_db
from utils.feature_extractor import get_feature_extractor
from ui.keyboards import Keyboards

logger = logging.getLogger(__name__)


class RatingService:
    """评分服务"""

    # 常见短链域名列表（不作为主键优先）
    SHORT_URL_DOMAINS = {
        "bit.ly",
        "t.co",
        "goo.gl",
        "tinyurl.com",
        "is.gd",
        "rebrand.ly",
        "cutt.ly",
        "ow.ly",
    }

    # 需要忽略的 t.me 特殊路径
    IGNORED_TG_PATHS = {
        "addstickers",
        "setlanguage",
    }

    # 标识类型优先级（数值越大优先级越高）
    IDENTIFIER_PRIORITY = {
        "domain": 100,
        "tg_username": 90,
        "tg_link": 80,
        "url": 70,
        "short_url": 60,
        "submitter_chat_id": 20,
        "submitter_user_id": 10,
    }

    # 主键类型优先级
    SUBJECT_TYPE_PRIORITY = ["domain", "tg_username", "tg_link", "submitter_user_id"]

    def __init__(self) -> None:
        self.extractor = get_feature_extractor()

    # -------------------------------------------------------------------------
    # 对外主接口
    # -------------------------------------------------------------------------
    async def get_or_create_subject_from_submission(
        self,
        submission_row: Any,
        user_id: int,
        source_chat_id: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        根据投稿数据解析评分实体并返回 subject 信息。

        Args:
            submission_row: sqlite3.Row 或 dict，投稿数据
            user_id: 投稿人 Telegram 用户 ID
            source_chat_id: 来源 chat ID（如有）

        Returns:
            dict: {subject_id, avg_score, vote_count} 或 None（未能识别实体）
        """
        if not RATING_ENABLED:
            return None

        try:
            identifiers = self._build_identifiers_from_submission(
                submission_row, user_id, source_chat_id
            )
            if not identifiers:
                logger.debug("未从投稿中提取到有效评分标识，跳过评分实体创建")
                return None

            subject_id, avg_score, vote_count = await self._get_or_create_subject(
                identifiers
            )
            return {
                "subject_id": subject_id,
                "avg_score": avg_score,
                "vote_count": vote_count,
            }
        except Exception as e:
            logger.error(f"解析评分实体失败: {e}", exc_info=True)
            return None

    async def attach_rating_keyboard(
        self,
        context,
        message_id: int,
        subject_id: int,
        avg_score: float,
        vote_count: int,
    ) -> None:
        """
        为频道消息附加评分键盘。

        Args:
            context: CallbackContext
            message_id: 频道消息 ID
            subject_id: 评分实体 ID
            avg_score: 当前平均分
            vote_count: 评分人数
        """
        if not RATING_ENABLED:
            return

        try:
            keyboard = Keyboards.rating_keyboard(subject_id, avg_score, vote_count)
            await context.bot.edit_message_reply_markup(
                chat_id=CHANNEL_ID,
                message_id=message_id,
                reply_markup=keyboard,
            )
        except Exception as e:
            # 键盘附加失败不应影响主流程
            logger.error(f"为消息 {message_id} 附加评分键盘失败: {e}", exc_info=True)

    # -------------------------------------------------------------------------
    # 内部辅助：标识提取与归类
    # -------------------------------------------------------------------------
    def _build_identifiers_from_submission(
        self,
        row: Any,
        user_id: int,
        source_chat_id: Optional[int] = None,
    ) -> List[Tuple[str, str]]:
        """
        从投稿记录中构建评分标识。

        Args:
            row: sqlite3.Row 或 dict
            user_id: 投稿人 ID
            source_chat_id: 来源 chat ID（如有）
        """
        # 兼容 sqlite3.Row 和 dict
        def get_field(name: str) -> str:
            if row is None:
                return ""
            try:
                if hasattr(row, "keys"):
                    return row[name] if name in row.keys() and row[name] else ""
                return row.get(name, "") or ""
            except Exception:
                return ""

        parts: List[str] = []
        text_content = get_field("text_content")
        if text_content:
            parts.append(str(text_content))
        title = get_field("title")
        if title:
            parts.append(str(title))
        note = get_field("note")
        if note:
            parts.append(str(note))
        tags = get_field("tags")
        if tags:
            parts.append(str(tags))
        link = get_field("link")
        if link:
            parts.append(str(link))

        content = "\n".join(parts)
        if not content:
            # 没有文本内容，仅用元数据也不太可靠，直接返回空
            return []

        features = self.extractor.extract_all(content)
        identifiers: List[Tuple[str, str]] = []

        # 1. URL / 域名处理
        for url in features.get("urls", []):
            try:
                parsed = urlparse(url)
                domain = parsed.netloc.lower()
                if not domain:
                    continue
                if domain.startswith("www."):
                    domain = domain[4:]

                if domain in self.SHORT_URL_DOMAINS:
                    identifiers.append(("short_url", url.lower()))
                    continue

                if domain in ("t.me", "telegram.me"):
                    path = (parsed.path or "").strip("/")
                    if path and "/" not in path and path not in self.IGNORED_TG_PATHS:
                        identifiers.append(("tg_username", path.lower()))
                    else:
                        # 如有需要可保留为 tg_link
                        # identifiers.append(("tg_link", url.lower()))
                        pass
                    continue

                identifiers.append(("domain", domain))
                identifiers.append(("url", url.lower()))
            except Exception:
                continue

        # 2. 直接的 @username
        for name in features.get("tg_usernames", []):
            identifiers.append(("tg_username", name.lower()))

        # 3. Telegram 链接（非 t.me 域名的 part）
        for link_value in features.get("tg_links", []):
            # 这里只保留为 tg_link，不强行转为 tg_username
            identifiers.append(("tg_link", link_value.lower()))

        # 4. 元数据：投稿人 / 来源 chat
        identifiers.append(("submitter_user_id", str(user_id)))
        if source_chat_id is not None:
            identifiers.append(("submitter_chat_id", str(source_chat_id)))

        # 去重
        unique_identifiers: List[Tuple[str, str]] = []
        seen = set()
        for t, v in identifiers:
            key = (t, v)
            if key not in seen:
                seen.add(key)
                unique_identifiers.append(key)

        return unique_identifiers

    async def _get_or_create_subject(
        self,
        identifiers: List[Tuple[str, str]],
    ) -> Tuple[int, float, int]:
        """
        根据标识获取或创建评分实体。

        返回：
            (subject_id, avg_score, vote_count)
        """
        # 先尝试根据标识查找已有 subject
        async with get_db() as conn:
            cursor = await conn.cursor()

            found_subjects: List[Tuple[int, str, str]] = []
            for ident_type, ident_value in identifiers:
                await cursor.execute(
                    """
                    SELECT subject_id FROM rating_subject_identifiers
                    WHERE identifier_type = ? AND identifier_value = ?
                    """,
                    (ident_type, ident_value),
                )
                rows = await cursor.fetchall()
                for row in rows:
                    found_subjects.append(
                        (row["subject_id"], ident_type, ident_value)
                    )

            subject_id: Optional[int] = None

            if not found_subjects:
                # 完全新实体
                subject_type, subject_key = self._choose_subject_key(identifiers)
                if subject_type is None or subject_key is None:
                    raise ValueError("无法根据标识选择评分实体主键")

                now_ts = datetime.now().timestamp()
                await cursor.execute(
                    """
                    INSERT INTO rating_subjects
                    (subject_type, subject_key, display_name,
                     score_sum, vote_count, avg_score, created_at, updated_at)
                    VALUES (?, ?, ?, 0, 0, 0.0, ?, ?)
                    """,
                    (subject_type, subject_key, subject_key, now_ts, now_ts),
                )
                subject_id = cursor.lastrowid
            else:
                # 命中已有 subject，可能多个，按标识类型优先级选一个
                subject_id = self._choose_existing_subject(found_subjects)

            # 将所有标识绑定到选定的 subject（使用 INSERT OR IGNORE 避免重复）
            for ident_type, ident_value in identifiers:
                await cursor.execute(
                    """
                    INSERT OR IGNORE INTO rating_subject_identifiers
                    (subject_id, identifier_type, identifier_value)
                    VALUES (?, ?, ?)
                    """,
                    (subject_id, ident_type, ident_value),
                )

            # 读取最新统计数据
            await cursor.execute(
                """
                SELECT avg_score, vote_count
                FROM rating_subjects
                WHERE id = ?
                """,
                (subject_id,),
            )
            row = await cursor.fetchone()
            avg_score = float(row["avg_score"]) if row and row["avg_score"] is not None else 0.0
            vote_count = int(row["vote_count"]) if row and row["vote_count"] is not None else 0

            return subject_id, avg_score, vote_count

    def _choose_subject_key(
        self,
        identifiers: List[Tuple[str, str]],
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        从标识列表中选择评分实体的主键类型和键值。
        按 SUBJECT_TYPE_PRIORITY 顺序选择第一个可用的标识。
        """
        for subject_type in self.SUBJECT_TYPE_PRIORITY:
            for ident_type, ident_value in identifiers:
                if ident_type == subject_type:
                    return subject_type, ident_value
        # 找不到合适主键
        return None, None

    def _choose_existing_subject(
        self,
        found_subjects: List[Tuple[int, str, str]],
    ) -> int:
        """
        当多个标识命中不同 subject 时，按标识类型优先级选择一个 subject。
        """
        # found_subjects: List[(subject_id, identifier_type, identifier_value)]
        best_subject_id = None
        best_priority = -1

        for subject_id, ident_type, _ in found_subjects:
            priority = self.IDENTIFIER_PRIORITY.get(ident_type, 0)
            if priority > best_priority:
                best_priority = priority
                best_subject_id = subject_id

        # 理论上不会为 None（found_subjects 非空），但为安全起见仍加断言
        if best_subject_id is None:
            best_subject_id = found_subjects[0][0]

        return best_subject_id


_rating_service: Optional[RatingService] = None


def get_rating_service() -> RatingService:
    """获取 RatingService 单例"""
    global _rating_service
    if _rating_service is None:
        _rating_service = RatingService()
    return _rating_service

