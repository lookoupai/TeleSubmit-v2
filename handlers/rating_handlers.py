"""
评分回调处理模块

处理用户点击评分按钮（1~5 星）的回调逻辑：
- 每个用户对同一评分实体只记一次（或按配置允许修改）
- 更新评分聚合数据
- 刷新当前消息下方的评分按钮展示
"""
import logging
from telegram import Update
from telegram.ext import CallbackContext

from config.settings import RATING_ENABLED, RATING_ALLOW_UPDATE
from database.db_manager import get_db
from ui.keyboards import Keyboards

logger = logging.getLogger(__name__)


async def handle_rating_callback(update: Update, context: CallbackContext):
    """
    处理评分按钮回调
    callback_data 格式：rating_{subject_id}_{score}
    """
    if not RATING_ENABLED:
        # 功能关闭时给出友好提示
        await update.callback_query.answer("评分功能暂未启用")
        return

    query = update.callback_query
    data = query.data
    # 汇总按钮仅用于展示，不做任何操作
    if data == "rating_info":
        return

    user_id = update.effective_user.id if update.effective_user else None

    try:
        parts = data.split("_")
        if len(parts) != 3:
            await query.answer("评分数据无效", show_alert=True)
            return

        _, subject_id_str, score_str = parts
        subject_id = int(subject_id_str)
        score = int(score_str)
    except Exception:
        await query.answer("评分数据格式错误", show_alert=True)
        return

    if score < 1 or score > 5:
        await query.answer("评分范围必须在 1~5 星", show_alert=True)
        return

    if user_id is None:
        await query.answer("无法识别用户，评分失败", show_alert=True)
        return

    try:
        async with get_db() as conn:
            cursor = await conn.cursor()

            # 查询该用户是否已经对该实体评分
            await cursor.execute(
                """
                SELECT id, score FROM rating_votes
                WHERE subject_id = ? AND user_id = ?
                """,
                (subject_id, user_id),
            )
            row = await cursor.fetchone()

            if row is None:
                # 首次评分：插入记录并更新聚合
                await cursor.execute(
                    """
                    INSERT INTO rating_votes (subject_id, user_id, score, created_at, updated_at)
                    VALUES (?, ?, ?, strftime('%s', 'now'), strftime('%s', 'now'))
                    """,
                    (subject_id, user_id, score),
                )

                await cursor.execute(
                    """
                    UPDATE rating_subjects
                    SET score_sum = score_sum + ?,
                        vote_count = vote_count + 1,
                        avg_score = CAST(score_sum + ? AS REAL) / (vote_count + 1),
                        updated_at = strftime('%s', 'now')
                    WHERE id = ?
                    """,
                    (score, score, subject_id),
                )

                await query.answer("感谢你的评分！")
            else:
                old_score = int(row["score"])

                if not RATING_ALLOW_UPDATE:
                    await query.answer("你已经给这条内容评分过了", show_alert=True)
                else:
                    if old_score == score:
                        await query.answer("你的评分已是当前星级", show_alert=True)
                    else:
                        # 更新评分
                        await cursor.execute(
                            """
                            UPDATE rating_votes
                            SET score = ?, updated_at = strftime('%s', 'now')
                            WHERE id = ?
                            """,
                            (score, row["id"]),
                        )

                        delta = score - old_score
                        await cursor.execute(
                            """
                            UPDATE rating_subjects
                            SET score_sum = score_sum + ?,
                                avg_score = CASE
                                    WHEN vote_count > 0 THEN CAST(score_sum + ? AS REAL) / vote_count
                                    ELSE 0.0
                                END,
                                updated_at = strftime('%s', 'now')
                            WHERE id = ?
                            """,
                            (delta, delta, subject_id),
                        )

                        await query.answer("已更新你的评分")

            # 读取最新聚合结果
            await cursor.execute(
                """
                SELECT avg_score, vote_count
                FROM rating_subjects
                WHERE id = ?
                """,
                (subject_id,),
            )
            subject_row = await cursor.fetchone()
            if not subject_row:
                # 数据异常时不尝试刷新按钮
                return

            avg_score = float(subject_row["avg_score"] or 0.0)
            vote_count = int(subject_row["vote_count"] or 0)

        # 刷新当前消息下方的评分键盘
        try:
            keyboard = Keyboards.rating_keyboard(subject_id, avg_score, vote_count)
            await context.bot.edit_message_reply_markup(
                chat_id=query.message.chat_id,
                message_id=query.message.message_id,
                reply_markup=keyboard,
            )
        except Exception as e:
            # UI 刷新失败不影响评分结果
            logger.error(f"刷新评分键盘失败: {e}", exc_info=True)

    except Exception as e:
        logger.error(f"处理评分回调时出错: {e}", exc_info=True)
        try:
            await query.answer("评分处理失败，请稍后重试", show_alert=True)
        except Exception:
            pass
