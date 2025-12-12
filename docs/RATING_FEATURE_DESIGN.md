# TeleSubmit-v2 评分功能设计方案

> 文档版本：1.0  
> 创建日期：2025-12-11  
> 功能模块：投稿评分 / 服务实体聚合评分

---

## 一、功能概述

目标：为频道内每条投稿提供 1~5 星评分能力，并对“同一服务/商家”进行统一评分统计，在后续投稿中复用历史评分结果。

核心需求：
- 在频道消息下提供一排评分按钮（1⭐~5⭐）。
- 每个 Telegram 账号对同一“评分对象”仅记一次评分（支持可选改分）。
- 评分对象不是“单条消息”，而是“同一个服务/商家实体”（例如同一个网站或一组 TG 联系账号）。
- 首次评分后实时更新当前消息的平均分展示；后续相同实体的投稿，自动带出已有平均分。
- 实现上复用现有特征提取 / 指纹体系，保持代码简洁（KISS）并避免重复造轮子（DRY）。

本设计仅为方案，不修改现有代码，仅说明需要变更的模块和推荐实现方式。

---

## 二、业务与交互设计

### 2.1 用户视角

- 频道中每条投稿消息下方有一排星级按钮：
  - `1⭐  2⭐  3⭐  4⭐  5⭐`
  - 下面一行显示当前评分汇总，例如：
    - `⭐ 当前评分：4.2 （123 人参与）`
    - 未有评分时：`⭐ 当前暂无评分，欢迎点击评分`
- 用户点击任一星星：
  - 首次评分：记一条投票，更新平均分和人数，并实时更新当前消息下方的评分展示。
  - 已评分再点击：
    - 若允许改分：更新该用户对该对象的得分，并重新计算平均分。
    - 若不允许改分：不改变得分，弹出提示“你已经给这条内容评分过了”。

### 2.2 评分对象视角

评分的对象是“服务/商家实体”，不是单条消息：

- 同一实体可能有多条不同投稿：
  - 相同网站域名 + 一组联系 TG 号；
  - 多个客服号 + 机器人 + 产品频道构成的一个“供应商”；
  - 同一网站在不同时间、不同人投稿的多条广告。
- 我们为每个实体建立一个 `RatingSubject`：
  - 通过多个标识（URL、域名、@username、t.me 链接、投稿人 ID、来源频道/群组 ID 等）来指向同一实体。
  - 任意标识命中即可找到该实体，实现“统一评分”。

---

## 三、概念模型与数据结构

### 3.1 核心概念

- `RatingSubject`（评分主体 / 实体）
  - 抽象一个“服务/商家”，统一聚合其所有评分。
  - 有一个主键（`subject_type + subject_key`），例如：
    - `subject_type = 'domain'`, `subject_key = '68sms.com'`
    - `subject_type = 'tg_username'`, `subject_key = 'smsbower_support_bot'`
  - 保存聚合后的统计数据：总分、投票数、平均分等。

- `RatingIdentifier`（评分标识）
  - 某个实体的一个“别名/特征”，例如：
    - `domain:68sms.com`
    - `tg_username:us_68sms_2`
    - `tg_username:CF996_bot`
    - `submitter_user_id:123456789`
  - 一个实体可以有多个标识；任一标识命中都可定位到该实体。

- `RatingVote`（评分记录）
  - 某个用户对某个实体的一次评分。
  - 约束：每个 `(subject_id, user_id)` 组合最多一条记录（防刷）。

### 3.2 数据库表设计

在 `database/db_manager.py:init_db` 中新增三张表及索引（仅设计）：

#### 3.2.1 `rating_subjects`

```sql
CREATE TABLE IF NOT EXISTS rating_subjects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_type TEXT NOT NULL,           -- 'domain' / 'tg_username' / 'url' / 'mixed' 等
    subject_key TEXT NOT NULL,            -- 规范化后的主键值，如域名、用户名等
    display_name TEXT,                    -- 展示名，默认可用 subject_key
    score_sum INTEGER DEFAULT 0,          -- 总分（整数：1~5 累加）
    vote_count INTEGER DEFAULT 0,         -- 评分次数
    avg_score REAL DEFAULT 0.0,           -- 冗余存一份平均分，避免每次 sum/count
    created_at REAL DEFAULT (strftime('%s', 'now')),
    updated_at REAL DEFAULT (strftime('%s', 'now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_rating_subject_unique
ON rating_subjects(subject_type, subject_key);
```

说明：
- `subject_type + subject_key` 作为“强主键”，不使用“任意 2~3 个特征相同就视为同一对象”的模糊规则。
- `display_name` 仅用于 UI 展示，可为空。

#### 3.2.2 `rating_subject_identifiers`

```sql
CREATE TABLE IF NOT EXISTS rating_subject_identifiers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id INTEGER NOT NULL,
    identifier_type TEXT NOT NULL,        -- 'domain' / 'url' / 'tg_username' / 'tg_link' /
                                          -- 'short_url' / 'submitter_user_id' / 'submitter_chat_id' 等
    identifier_value TEXT NOT NULL,
    created_at REAL DEFAULT (strftime('%s', 'now')),
    FOREIGN KEY(subject_id) REFERENCES rating_subjects(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_rating_identifier_unique
ON rating_subject_identifiers(identifier_type, identifier_value);

CREATE INDEX IF NOT EXISTS idx_rating_identifier_subject
ON rating_subject_identifiers(subject_id);
```

说明：
- 一个 `identifier_type + identifier_value` 只能属于一个 `subject_id`。
- 允许同一实体挂载多个标识；多联系人、多机器人、多频道的场景通过这里统一。

#### 3.2.3 `rating_votes`

```sql
CREATE TABLE IF NOT EXISTS rating_votes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,             -- Telegram 用户 ID
    score INTEGER NOT NULL,               -- 1~5
    created_at REAL DEFAULT (strftime('%s', 'now')),
    updated_at REAL DEFAULT (strftime('%s', 'now')),
    FOREIGN KEY(subject_id) REFERENCES rating_subjects(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_rating_vote_unique
ON rating_votes(subject_id, user_id);

CREATE INDEX IF NOT EXISTS idx_rating_vote_subject
ON rating_votes(subject_id);
```

说明：
- `UNIQUE(subject_id, user_id)` 确保“每个账号对同一评分对象只算一次”。
- 若开启“改分”功能，通过更新 `score` 和 `updated_at` 实现。

#### 3.2.4 `published_posts` 扩展字段

为了让搜索/统计等功能可方便使用评分信息，建议在 `published_posts` 表增加以下列：

```sql
ALTER TABLE published_posts ADD COLUMN rating_subject_id INTEGER;
ALTER TABLE published_posts ADD COLUMN rating_avg REAL DEFAULT 0.0;
ALTER TABLE published_posts ADD COLUMN rating_votes INTEGER DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_published_rating_subject
ON published_posts(rating_subject_id);
```

设计意图：
- `rating_subject_id`：将帖子与评分实体显式关联。
- `rating_avg`、`rating_votes`：保存当时的快照，方便热门排行、用户统计等直接使用（也可以在需要时从 `rating_subjects` 联查重新计算）。

---

## 四、标识提取与评分实体归类

### 4.1 复用现有特征提取器

现有模块 `utils/feature_extractor.py` 已定义：

- `FeatureExtractor.extract_all(text)` 可提取：
  - `urls`：一般 URL（含 `https://t.me/...`）
  - `tg_usernames`：`@username`
  - `tg_links`：`t.me/...` / `telegram.me/...`
  - `phone_numbers` / `emails` 等

重复检测和 AI 审核已通过 `_build_content_for_review` 把投稿各字段合并为一个字符串进行特征提取：

- 参考 `handlers/review_handlers.py:_build_content_for_review`：
  - `text_content` / `title` / `note` / `tags` / `link`

评分实体识别可以直接沿用这套逻辑，保证维度一致，避免重复实现（DRY）。

### 4.2 待提取的标识类型

评分实体识别关注的标识类型：

- 内容级标识：
  - `domain`：从 URL 中提取的域名（主推荐维度）。
  - `url`：完整 URL（保留用于人工排查或未来扩展）。
  - `tg_username`：`@user`，或 `https://t.me/user`、`t.me/channel/123` 等形式解析出的用户名/频道名。
  - `short_url`：短链（`bit.ly` 等），可作为低优先级标识。

- 元数据标识（不参与主键，但可用于统计/排查）：
  - `submitter_user_id`：投稿人 Telegram 数字 ID。
  - `submitter_chat_id`：投稿来源频道/群组 Chat ID（如有）。

### 4.3 URL 与 TG 链接规范化规则

建议在新增 `utils/rating_service.py`（或类似模块）中实现以下逻辑：

1. 构建内容：

```python
def build_content_for_rating(submission_row) -> str:
    parts = []
    # 尽量复用 _build_content_for_review 的逻辑
    if submission_row.get('text_content'):
        parts.append(submission_row['text_content'])
    if submission_row.get('title'):
        parts.append(submission_row['title'])
    if submission_row.get('note'):
        parts.append(submission_row['note'])
    if submission_row.get('tags'):
        parts.append(submission_row['tags'])
    if submission_row.get('link'):
        parts.append(submission_row['link'])
    return "\n".join(parts)
```

2. 使用 `FeatureExtractor` 提取特征：

```python
extractor = get_feature_extractor()
features = extractor.extract_all(content)
```

3. URL 规范化：

- 使用 `urllib.parse.urlparse` 分解 URL。
- 域名统一为小写，去掉前缀 `www.`：
  - 例如 `https://www.68sms.com/cn` → `domain = "68sms.com"`.
- 识别短链域名（可配置）：
  - 如：`bit.ly`, `t.co`, `goo.gl`, `tinyurl.com`, `is.gd` 等。
  - 短链作为 `short_url`，不作为主键优先级最高的标识。
- 处理 `t.me` / `telegram.me`：
  - 若 `netloc` 为 `t.me` 或 `telegram.me`：
    - 若 path 为单段且不是明显的公共前缀（如 `addstickers` / `setlanguage` 等），可视为 `tg_username`。
    - 否则作为 `tg_link` 或忽略。

4. 综合生成标识列表：

伪代码示例：

```python
identifiers = []

# 1. 从普通 URL 提取域名 / URL
for url in features['urls']:
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    domain = domain[4:] if domain.startswith('www.') else domain

    if domain in SHORT_URL_DOMAINS:
        identifiers.append(('short_url', url))
    elif domain in ('t.me', 'telegram.me'):
        # 尝试从 path 提取 TG 名称
        path = parsed.path.strip('/')
        if path and '/' not in path and path not in IGNORED_TG_PATHS:
            identifiers.append(('tg_username', path.lower()))
        else:
            # 如需可保留为 tg_link 以备人工分析
            # identifiers.append(('tg_link', url))
            pass
    else:
        identifiers.append(('domain', domain))
        identifiers.append(('url', url))

# 2. 直接的 @username
for name in features['tg_usernames']:
    identifiers.append(('tg_username', name.lower()))

# 3. 元数据：投稿人 ID、来源 chat
identifiers.append(('submitter_user_id', str(user_id)))
if source_chat_id is not None:
    identifiers.append(('submitter_chat_id', str(source_chat_id)))
```

### 4.4 评分实体选主键策略

在所有候选标识中选择一个作为 `RatingSubject` 的主键：

- 优先级建议：
  1. `domain`
  2. `tg_username`
  3. `tg_link`（如启用）
  4. 兜底：`submitter_user_id`

选择规则：
- 若存在 `domain` 标识：
  - `subject_type = 'domain'`
  - `subject_key = 该域名`
- 否则若存在 `tg_username`：
  - `subject_type = 'tg_username'`
  - `subject_key = 该用户名（无 @ 前缀的小写字符串）`
- 否则可以根据需求选择 `tg_link` 或 `submitter_user_id` 作为兜底。

说明：
- 不使用“2~3 个特征一致即视为同一对象”的模糊规则，避免不可控冲突。
- 投稿人 ID / 来源 chat ID 仅作为附加信息，不参与实体主键的哈希组合。

### 4.5 标识归类算法（get_or_create_subject）

整体算法：

1. 输入：标识列表 `identifiers: List[(identifier_type, identifier_value)]`。
2. 在 `rating_subject_identifiers` 中批量查询所有匹配：

```sql
SELECT subject_id, identifier_type, identifier_value
FROM rating_subject_identifiers
WHERE (identifier_type, identifier_value) IN (..)
```

3. 情况划分：
   - **无匹配**：
     - 使用 4.4 中的策略选择主键，创建新的 `rating_subjects` 记录。
     - 将所有标识写入 `rating_subject_identifiers` 绑定到该 `subject_id`。
   - **仅匹配到一个 subject_id**：
     - 复用该 `subject_id`。
     - 将本次新增但未存在的标识追加到 `rating_subject_identifiers`。
   - **匹配到多个不同 subject_id（冲突）**：
     - 根据标识类型优先级决定归属：
       - 如命中 `domain` 与 `tg_username` 分属不同 subject，则优先 `domain`。
     - 本次只使用优先级最高的那一个 `subject_id`，不自动合并不同 subject（保守策略，避免误合并）。
     - 可在后续增加管理端“实体合并工具”手工处理。

4. 返回：
   - `subject_id`、当前 `avg_score`、`vote_count` 等。

---

## 五、与投稿流程的集成设计

### 5.1 投稿发布关键路径回顾

当前主流程（简化）：

- 会话 & 投稿信息采集：
  - `handlers/submit_handlers.py` 等，将用户输入写入 `submissions` 表。
- 审核与重复检测：
  - `handlers/review_handlers.perform_review` 内调用：
    - `DuplicateDetector`（特征提取 + 指纹匹配）
    - `AIReviewer`（AI 审核）
- 通过审核后发布：
  - `handlers/publish.py:publish_submission`：
    - 从 `submissions` 表读出数据。
    - 构建 caption：`build_caption(data)`.
    - 调用：
      - `handle_text_publish`（纯文本）
      - `handle_media_publish`（媒体组）
      - `handle_document_publish`（文档）
    - 发布成功后，调用 `save_published_post` 将数据落到 `published_posts`，并写入搜索索引。

评分功能的接入点应尽量靠近“最终发布”环节，以便：
- 可访问完整投稿信息（文本、标签、链接、投稿人信息）。
- 拿到频道消息 `message_id`，用于后续回调更新。

### 5.2 新增 RatingService（示意）

建议新增 `utils/rating_service.py`，封装评分相关逻辑，主要职责：

- 从投稿数据中提取标识并解析成 `identifiers` 列表。
- 调用 `get_or_create_subject` 进行实体归类。
- 更新 `published_posts` 表的 `rating_subject_id`、`rating_avg`、`rating_votes` 快照。
- 为已发布的频道消息附加评分按钮。

示意接口：

```python
class RatingService:
    async def assign_subject_for_post(
        self,
        submission_row,
        user_id: int,
        message_id: int,
        content_type: str,
    ) -> dict:
        """
        解析投稿特征，获取或创建评分实体，更新 published_posts 并返回 subject 信息。
        """
        ...

    async def attach_rating_keyboard(
        self,
        context,
        chat_id: int,
        message_id: int,
        subject_id: int,
        avg_score: float,
        vote_count: int,
    ) -> None:
        """
        使用 edit_message_reply_markup 为指定消息添加评分按钮。
        """
        ...
```

### 5.3 在发布流程中的调用点

在 `handlers/publish.py:publish_submission` 中，发送成功后、保存 `published_posts` 前后插入两段逻辑：

1. **确定 subject 并写入 DB**：

```python
from utils.rating_service import get_rating_service
from config.settings import RATING_ENABLED

...
if RATING_ENABLED:
    rating_service = get_rating_service()
    subject_info = await rating_service.assign_subject_for_post(
        submission_row=data,
        user_id=user_id,
        message_id=sent_message.message_id,
        content_type=content_type,  # 可由现有逻辑推导
    )
    # subject_info 包含 subject_id / avg_score / vote_count
```

2. **为频道消息附加评分按钮**：

由于发送函数已经返回了 `sent_message`，可以通过 `edit_message_reply_markup` 附加 InlineKeyboard，而无需改动 `handle_text_publish` / `handle_media_publish` 签名：

```python
if RATING_ENABLED and subject_info:
    await rating_service.attach_rating_keyboard(
        context=context,
        chat_id=CHANNEL_ID,
        message_id=sent_message.message_id,
        subject_id=subject_info['subject_id'],
        avg_score=subject_info['avg_score'],
        vote_count=subject_info['vote_count'],
    )
```

3. **在 `save_published_post` 中记录 subject 快照**：

在 `save_published_post` 参数或内部增加可选的 `rating_subject_id` / `rating_avg` / `rating_votes` 更新逻辑：

```python
async def save_published_post(..., rating_subject_id=None, rating_avg=None, rating_votes=None):
    ...
    await cursor.execute("""
        INSERT INTO published_posts
        (message_id, user_id, username, title, tags, link, note,
         content_type, file_ids, caption, filename, publish_time,
         views, forwards, reactions, heat_score, last_update,
         related_message_ids, is_deleted, text_content,
         rating_subject_id, rating_avg, rating_votes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (...))
```

说明：
- 为保持向后兼容，可以先用 `INSERT` 不包含新字段，再紧接一条 `UPDATE` 写入评分字段；或通过检查列存在性后动态构造 SQL。

---

## 六、按钮与回调协议设计

### 6.1 Inline Keyboard 布局

在 `ui/keyboards.py` 中新增评分键盘生成方法（示意）：

```python
class Keyboards:
    @staticmethod
    def rating_keyboard(subject_id: int, avg_score: float, vote_count: int):
        buttons_row = [
            InlineKeyboardButton("1⭐", callback_data=f"rating_{subject_id}_1"),
            InlineKeyboardButton("2⭐", callback_data=f"rating_{subject_id}_2"),
            InlineKeyboardButton("3⭐", callback_data=f"rating_{subject_id}_3"),
            InlineKeyboardButton("4⭐", callback_data=f"rating_{subject_id}_4"),
            InlineKeyboardButton("5⭐", callback_data=f"rating_{subject_id}_5"),
        ]

        if vote_count > 0:
            summary_text = f"⭐ {avg_score:.1f}（{vote_count} 人参与）"
        else:
            summary_text = "⭐ 当前暂无评分，欢迎点击评分"

        summary_row = [
            InlineKeyboardButton(summary_text, callback_data="rating_info")
        ]

        return InlineKeyboardMarkup([buttons_row, summary_row])
```

说明：
- 评分信息通过第二行按钮展示，可以满足“在消息中展示平均评分”的需求，同时避免修改消息正文/Caption 的复杂性。
- `rating_info` 按钮仅用于展示说明，可在回调中简单 `answer()` 提示，不修改状态。

### 6.2 回调数据格式

采用统一的 `rating_{subject_id}_{score}` 格式，便于在现有 `handle_callback_query` 中路由：

- 示例：
  - `rating_12_5` → 对 subject 12 评分 5 星
  - `rating_12_3` → 对 subject 12 评分 3 星

解析逻辑示意：

```python
data = query.data  # e.g. "rating_12_5"
_, subject_id_str, score_str = data.split("_", 2)
subject_id = int(subject_id_str)
score = int(score_str)
```

在 `handlers/callback_handlers.py:handle_callback_query` 中增加分支：

```python
from handlers.rating_handlers import handle_rating_callback

...
elif data.startswith("rating_"):
    await handle_rating_callback(update, context)
```

---

## 七、评分回调处理流程

### 7.1 回调处理入口

新增模块 `handlers/rating_handlers.py`，单一职责处理评分相关回调（满足 SRP）：

```python
async def handle_rating_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    data = query.data
    user_id = update.effective_user.id

    try:
        _, subject_id_str, score_str = data.split("_", 2)
        subject_id = int(subject_id_str)
        score = int(score_str)
    except Exception:
        await query.answer("评分数据无效", show_alert=True)
        return

    if score < 1 or score > 5:
        await query.answer("评分范围必须在 1~5 星", show_alert=True)
        return

    # 更新数据库并刷新按钮
    ...
```

### 7.2 防刷逻辑与分值更新

在 `handle_rating_callback` 中使用事务更新：

1. 查询是否已有投票：

```sql
SELECT id, score FROM rating_votes
WHERE subject_id = ? AND user_id = ?
```

2. 分支逻辑：

- 未存在记录：
  - 插入新行：

    ```sql
    INSERT INTO rating_votes(subject_id, user_id, score, created_at, updated_at)
    VALUES (?, ?, ?, strftime('%s', 'now'), strftime('%s', 'now'));
    ```

  - 更新 `rating_subjects` 聚合值（示意）：

    ```sql
    UPDATE rating_subjects
    SET score_sum = score_sum + ?,
        vote_count = vote_count + 1,
        avg_score = CAST(score_sum + ? AS REAL) / (vote_count + 1),
        updated_at = strftime('%s', 'now')
    WHERE id = ?;
    ```

- 已存在记录：
  - 若配置 `RATING_ALLOW_UPDATE = false`：
    - 直接 `query.answer("你已经给这条内容评分过了", show_alert=True)`，不改动数据。
  - 若允许改分：
    - 重新计算差值：

      ```sql
      -- old_score -> new_score
      UPDATE rating_votes
      SET score = ?, updated_at = strftime('%s', 'now')
      WHERE id = ?;

      UPDATE rating_subjects
      SET score_sum = score_sum + (? - old_score),
          avg_score = CAST(score_sum + (? - old_score) AS REAL) / vote_count,
          updated_at = strftime('%s', 'now')
      WHERE id = ?;
      ```

3. 获取最新 `avg_score` / `vote_count`：

```sql
SELECT avg_score, vote_count FROM rating_subjects WHERE id = ?
```

### 7.3 更新当前消息的评分展示

评分展示依赖于当前消息的 InlineKeyboard，简单方案：

1. 构造新的 `rating_keyboard(subject_id, avg_score, vote_count)`。
2. 调用 `edit_message_reply_markup` 更新键盘：

```python
await context.bot.edit_message_reply_markup(
    chat_id=query.message.chat_id,
    message_id=query.message.message_id,
    reply_markup=Keyboards.rating_keyboard(subject_id, avg_score, vote_count)
)
```

3. 同时 `query.answer("感谢你的评分！")`。

说明：
- 该方案只修改 InlineKeyboard，不修改消息正文/Caption，避免了复杂的文本重建与长度限制问题，同时满足“在消息中展示平均评分”的需求（信息就在消息下的按钮上）。
- 若未来需要将评分信息写入正文，可在此处增加可选逻辑，通过 `edit_message_text` / `edit_message_caption` 重建消息内容（参见第 8 节扩展建议）。

---

## 八、与现有统计/搜索/指纹的关系

### 8.1 与热度统计（views/forwards/reactions）

当前热度模型主要基于：
- 浏览数 `views`
- 转发数 `forwards`
- 反应数 `reactions`
- 时间衰减等（见 `handlers/stats_handlers.py` 和 `utils/heat_calculator.py`）

评分信息可以作为后续“质量评分”的一个维度：

- 例如在 `calculate_multi_message_heat` 返回值中增加 `avg_rating` 字段，或在 `get_quality_metrics` 中考虑评分。
- 也可以在 `/hot` 排行输出中追加一行展示平均评分：
  - `⭐ 评分：4.3（50 人）`

初版不强依赖评分与热度的耦合（YAGNI），仅在需要时通过 `published_posts.rating_*` 或 `JOIN rating_subjects` 获取。

### 8.2 与搜索引擎（Whoosh）的关系

- 搜索索引结构 `PostDocument` 当前包含 `views` 和 `heat_score`。
- 若需要在搜索结果中按评分排序或展示评分，可以：
  - 在索引中新增一个 `rating` 字段（NUMERIC）。
  - 在 `save_published_post` / 索引同步时，将 `rating_subjects.avg_score` 写入索引。
- 初版可以不入索引，仅在输出搜索结果时，通过 `post_id` / `rating_subject_id` 关联查询评分信息。

### 8.3 与指纹特征（重复检测）的关系

相似点：
- 都基于 URL、TG 用户名、链接等特征。
- 都有一个特征表：`fingerprint_features` 用于重复检测。

区别与设计决策：
- `fingerprint_features` 绑定的是“具体一次投稿”（`submission_fingerprints`），偏行为日志与检测。
- 评分实体 `rating_subjects` 表示“长期存在的服务/商家”，需要稳定主键和可维护的标识集合。
- 因此本设计没有直接复用 `fingerprint_features`，而是设计独立的 `rating_subject_identifiers` 表，但在逻辑上沿用相同的特征解析规则，保证一致性。

---

## 九、配置项设计（config.ini / settings.py）

### 9.1 config.ini 新增配置示意

```ini
[RATING]
ENABLED = true                     # 是否启用评分功能
ALLOW_UPDATE = true                # 是否允许用户修改自己的评分
BUTTON_STYLE = stars               # 按钮样式：stars / numbers / compact
SHOW_ON_PUBLISH = true             # 发布时是否立即附加评分按钮
MIN_VOTES_TO_HIGHLIGHT = 1         # 达到多少条评分后才在统计/搜索中突出展示
```

### 9.2 settings.py 读取配置示意

在 `config/settings.py` 中增加：

```python
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

RATING_BUTTON_STYLE = get_env_or_config(
    'RATING_BUTTON_STYLE', 'RATING', 'BUTTON_STYLE', fallback='stars'
)

_min_votes_highlight = get_env_or_config(
    'RATING_MIN_VOTES_TO_HIGHLIGHT', 'RATING', 'MIN_VOTES_TO_HIGHLIGHT'
)
RATING_MIN_VOTES_TO_HIGHLIGHT = int(_min_votes_highlight) if _min_votes_highlight \
    else get_config_int('RATING', 'MIN_VOTES_TO_HIGHLIGHT', 1)
```

---

## 十、实现步骤建议（迭代计划）

为控制复杂度，建议按以下迭代顺序实现：

### 阶段 1：基础评分闭环（MVP）

1. **DB 迁移与模型层**：
   - 在 `init_db` 中创建 `rating_subjects`、`rating_subject_identifiers`、`rating_votes` 三张表。
   - 为 `published_posts` 增加 `rating_subject_id`、`rating_avg`、`rating_votes` 字段（可选）。

2. **RatingService 实现**：
   - 实现内容构建 + 特征提取 + 标识列表生成。
   - 实现 `get_or_create_subject` 归类算法。
   - 在 `publish_submission` 中调用，写入 `rating_subject_id` 与评分快照。

3. **评分按钮 & 回调**：
   - 在 `ui/keyboards.py` 中新增 `rating_keyboard`。
   - 在 `publish_submission` 成功后通过 `edit_message_reply_markup` 附加评分键盘。
   - 新增 `handlers/rating_handlers.py`，在 `callback_handlers` 中路由 `rating_` 回调。
   - 完成“首次评分 → 更新平均分 → 更新按钮展示”的闭环。

4. **防刷 & 错误处理**：
   - 完善 `RATING_ALLOW_UPDATE` 配置逻辑。
   - 在数据库层加上 UNIQUE 约束，避免应用层 bug 导致重复插入。

### 阶段 2：与统计/搜索集成（可选）

1. 在 `/hot`、`/mystats` 等输出中追加评分信息展示（读取 `published_posts.rating_*` 或 `rating_subjects`）。
2. 在搜索结果（`handlers/search_handlers.py` + `ui/messages.py`）中展示评分片段。
3. 视需要将评分写入 Whoosh 索引，支持“按评分排序”之类的高级查询。

### 阶段 3：高级能力（可选）

1. **正文内评分信息**：
   - 在评分回调中增加可选逻辑，将平均分写入消息正文/Caption。
   - 需要解决：
     - 纯文本投稿：`text_content + caption` 的重建。
     - 媒体投稿：Caption 长度上限与是否有单独 caption 消息的问题。
   - 推荐做法：
     - 在 `published_posts` 中增加 `has_spoiler` / `message_format` 等字段，记录发送时的关键格式信息。
     - 每次编辑时从 DB 重建“基础文本 + 评分附加行”，而不是修改当前消息文本。

2. **实体合并/拆分后台工具**：
   - 提供简单脚本或管理命令用于手工合并两个 `rating_subjects`，适用于发现“多账号/多域名其实是同一家”的场景。

3. **多维评分或标签（如服务质量/稳定性/价格）**：
   - 当前设计只支持单一 1~5 星评分。
   - 若未来需要，可以在 `rating_votes` 中扩展更多字段（遵守 YAGNI，当前不预留）。

---

## 十一、小结

本方案在不改变现有核心架构的前提下，引入了一个干净的评分子系统：

- 通过 `RatingSubject + RatingIdentifier + RatingVote` 三层模型，将“投稿内容”与“服务/商家实体”解耦，避免依赖模糊匹配规则。
- 利用现有 `FeatureExtractor` 的特征解析能力，保持实现一致性和简洁性（KISS、DRY）。
- 通过 Inline Keyboard 展示评分信息，先实现一个稳定的 MVP，再按需扩展到正文展示和更多分析维度（YAGNI）。

后续若你决定开始实现，我可以基于该文档直接给出每个文件的具体改动建议（代码级 diff），并按阶段拆分提交，降低风险。 

