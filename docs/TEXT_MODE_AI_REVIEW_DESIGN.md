# TeleSubmit-v2 新功能设计方案

> 文档版本：1.0
> 创建日期：2025-12-07
> 功能模块：纯文本投稿、AI 审核、重复检测

---

## 一、功能概述

本次新增三大功能模块：

| 功能模块 | 描述 |
|---------|------|
| **纯文本投稿模式** | 允许用户仅发送文本即可完成投稿，无需上传媒体或文档 |
| **AI 内容审核** | 使用 OpenAI 兼容 API 自动审核投稿内容是否符合频道主题 |
| **重复投稿检测** | 基于多维特征识别 7 天内的重复投稿行为 |

---

## 二、纯文本投稿模式设计

### 2.1 配置项扩展

在 `config.ini` 中新增配置：

```ini
[BOT]
# 现有配置
BOT_MODE = MIXED                    # MEDIA/DOCUMENT/MIXED/TEXT/ALL
                                    # TEXT: 仅文本模式
                                    # ALL: 支持所有模式（文本/媒体/文档）

# 新增配置
TEXT_ONLY_MODE = true               # 是否允许纯文本投稿（默认 true）
DEFAULT_SUBMIT_MODE = TEXT          # 默认投稿模式：TEXT/MEDIA/DOCUMENT
MIN_TEXT_LENGTH = 10                # 纯文本投稿最小字符数
MAX_TEXT_LENGTH = 4000              # 纯文本投稿最大字符数（Telegram 限制）
```

### 2.2 模式选择逻辑

```
BOT_MODE 配置值说明：
├── TEXT      → 仅允许纯文本投稿
├── MEDIA     → 仅允许媒体投稿（现有）
├── DOCUMENT  → 仅允许文档投稿（现有）
├── MIXED     → 媒体+文档可选（现有）
└── ALL       → 文本+媒体+文档全部可选（新增）
```

### 2.3 投稿流程变更

**新流程（ALL 模式）：**

```
/submit 命令
    ↓
模式选择键盘：
┌─────────────┬─────────────┬─────────────┐
│  📝 纯文本   │  🖼 媒体     │  📁 文档     │
└─────────────┴─────────────┴─────────────┘
    ↓
[纯文本模式]
    ├─ 直接输入投稿内容（正文）
    ├─ 输入标签（必填）
    ├─ 输入链接（可选）
    └─ 发布到频道
```

### 2.4 状态常量扩展

在 `models/state.py` 中新增：

```python
STATE = {
    # ... 现有状态
    'TEXT_CONTENT': 14,    # 纯文本内容输入
}
```

### 2.5 数据库表变更

`submissions` 表新增字段：

```sql
ALTER TABLE submissions ADD COLUMN text_content TEXT;  -- 纯文本投稿内容
```

`published_posts` 表新增 `content_type` 值：`text`

---

## 三、AI 内容审核设计

### 3.1 配置项

```ini
[AI_REVIEW]
ENABLED = true                              # 是否启用 AI 审核
API_BASE_URL = https://api.openai.com/v1    # OpenAI 兼容 API 地址
API_KEY = sk-xxx                            # API 密钥
MODEL = gpt-4o-mini                         # 使用的模型
TIMEOUT = 30                                # API 超时时间（秒）
MAX_RETRIES = 2                             # 最大重试次数

# 审核配置
CHANNEL_TOPIC = 接码服务                     # 频道主题描述
TOPIC_KEYWORDS = 接码,短信,验证码,SMS,号码    # 主题关键词（逗号分隔）
STRICT_MODE = false                         # 严格模式（true=必须高度相关）
AUTO_REJECT = true                          # 不相关内容自动拒绝
NOTIFY_USER = true                          # 是否通知用户审核结果

# 缓存机制
CACHE_ENABLED = true                        # 是否启用审核结果缓存
CACHE_TTL_HOURS = 24                        # 缓存有效期（小时）

# 降级策略
FALLBACK_ON_ERROR = manual                  # API失败时：manual/pass/reject

# 管理员通知
NOTIFY_ADMIN_ON_REJECT = true               # 自动拒绝时通知管理员
NOTIFY_ADMIN_ON_DUPLICATE = true            # 重复投稿时通知管理员
```

### 3.2 审核流程

```
用户完成投稿信息填写
    ↓
┌─────────────────────────────────────┐
│           AI 审核模块                │
├─────────────────────────────────────┤
│ 1. 提取投稿内容（文本/标签/链接等）    │
│ 2. 构建审核 Prompt                   │
│ 3. 调用 OpenAI 兼容 API              │
│ 4. 解析审核结果                      │
└─────────────────────────────────────┘
    ↓
┌─────────┬─────────┬─────────────┐
│  通过   │  拒绝   │  需人工审核   │
└────┬────┴────┬────┴──────┬──────┘
     ↓         ↓            ↓
  发布到频道  通知用户拒绝   通知管理员审核
```

### 3.3 AI 审核模块设计

**文件位置**: `utils/ai_reviewer.py`

```python
class AIReviewer:
    """AI 内容审核器（OpenAI SDK 兼容）"""

    def __init__(self, config: dict):
        self.api_base = config['API_BASE_URL']
        self.api_key = config['API_KEY']
        self.model = config['MODEL']
        self.channel_topic = config['CHANNEL_TOPIC']
        self.topic_keywords = config['TOPIC_KEYWORDS'].split(',')

    async def review(self, submission: dict) -> ReviewResult:
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
                'user_bio': str,      # 用户签名（新增采集）
            }

        Returns:
            ReviewResult: {
                'approved': bool,           # 是否通过
                'confidence': float,        # 置信度 0-1
                'reason': str,              # 审核理由
                'category': str,            # 内容分类
                'requires_manual': bool,    # 是否需要人工审核
            }
        """
        pass
```

### 3.4 审核 Prompt 设计

```
你是一个 Telegram 频道投稿审核助手。该频道主题是：{channel_topic}

请审核以下投稿内容是否与频道主题相关：

---
投稿内容：
{text_content}

标签：{tags}
链接：{link}
标题：{title}
简介：{note}
---

审核标准：
1. 内容必须与「{channel_topic}」主题相关
2. 相关关键词包括：{topic_keywords}
3. 广告、垃圾信息、违规内容应拒绝

请以 JSON 格式返回审核结果：
{
    "approved": true/false,
    "confidence": 0.0-1.0,
    "reason": "审核理由",
    "category": "内容分类（如：接码服务/广告/无关内容）",
    "requires_manual": true/false
}
```

### 3.5 审核结果处理

| 结果 | 条件 | 处理方式 |
|-----|------|---------|
| **自动通过** | `approved=true` 且 `confidence>=0.8` | 直接发布到频道 |
| **自动拒绝** | `approved=false` 且 `confidence>=0.8` | 通知用户拒绝原因 |
| **人工审核** | `confidence<0.8` 或 `requires_manual=true` | 转发给管理员审核 |

---

## 四、重复投稿检测设计

### 4.1 配置项

```ini
[DUPLICATE_CHECK]
ENABLED = true                      # 是否启用重复检测
CHECK_WINDOW_DAYS = 7               # 检测时间窗口（天）
SIMILARITY_THRESHOLD = 0.8          # 相似度阈值（0-1）

# 检测维度开关
CHECK_USER_ID = true                # 检测用户 ID
CHECK_URLS = true                   # 检测 URL
CHECK_CONTACTS = true               # 检测联系方式
CHECK_TG_LINKS = true               # 检测 TG 链接/ID
CHECK_USER_BIO = true               # 检测用户签名中的特征
CHECK_CONTENT_HASH = true           # 检测内容哈希（文本相似度）

# 处理方式
AUTO_REJECT_DUPLICATE = true        # 自动拒绝重复投稿
NOTIFY_USER_DUPLICATE = true        # 通知用户重复原因

# 投稿频率限制
RATE_LIMIT_ENABLED = true           # 是否启用频率限制
RATE_LIMIT_COUNT = 3                # 时间窗口内最大投稿次数
RATE_LIMIT_WINDOW_HOURS = 24        # 频率限制时间窗口（小时）
```

### 4.2 特征提取设计

**文件位置**: `utils/feature_extractor.py`

```python
@dataclass
class SubmissionFingerprint:
    """投稿指纹（用于重复检测）"""
    user_id: int                    # 用户 ID
    username: str                   # 用户名

    # 从投稿内容提取
    urls: List[str]                 # 所有 URL
    tg_usernames: List[str]         # @username 格式
    tg_links: List[str]             # t.me/xxx 链接
    tg_group_ids: List[str]         # 群组/频道 ID
    phone_numbers: List[str]        # 电话号码
    emails: List[str]               # 邮箱地址
    content_hash: str               # 内容哈希（SimHash）

    # 从用户签名提取（新增采集）
    bio_urls: List[str]             # 签名中的 URL
    bio_tg_links: List[str]         # 签名中的 TG 链接
    bio_contacts: List[str]         # 签名中的联系方式

    # 元数据
    submit_time: float              # 投稿时间
    fingerprint_version: int        # 指纹版本（便于后续升级）
```

### 4.3 特征提取正则表达式

```python
class FeatureExtractor:
    """特征提取器"""

    PATTERNS = {
        # URL 提取
        'url': r'https?://[^\s<>"{}|\\^`\[\]]+',

        # Telegram 相关
        'tg_username': r'@([a-zA-Z][a-zA-Z0-9_]{4,31})',
        'tg_link': r't\.me/([a-zA-Z0-9_]+)',
        'tg_link_full': r'(?:https?://)?(?:t\.me|telegram\.me)/([a-zA-Z0-9_+]+)',

        # 联系方式
        'phone': r'(?:\+?[0-9]{1,3}[-.\s]?)?(?:\([0-9]{2,4}\)|[0-9]{2,4})[-.\s]?[0-9]{3,4}[-.\s]?[0-9]{3,4}',
        'email': r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}',

        # 其他平台
        'wechat': r'(?:微信|wx|WeChat)[：:\s]*([a-zA-Z0-9_-]+)',
        'qq': r'(?:QQ|qq)[：:\s]*([0-9]{5,12})',
    }
```

### 4.4 数据库表设计

**新建表**: `submission_fingerprints`

```sql
CREATE TABLE submission_fingerprints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,               -- 用户 ID
    username TEXT,                          -- 用户名

    -- 特征字段（JSON 数组存储）
    urls TEXT,                              -- URL 列表
    tg_usernames TEXT,                      -- TG 用户名列表
    tg_links TEXT,                          -- TG 链接列表
    phone_numbers TEXT,                     -- 电话号码列表
    emails TEXT,                            -- 邮箱列表

    -- 用户签名特征
    bio_features TEXT,                      -- 签名中提取的特征（JSON）

    -- 内容指纹
    content_hash TEXT,                      -- SimHash 值
    content_length INTEGER,                 -- 内容长度

    -- 元数据
    submit_time REAL NOT NULL,              -- 投稿时间
    submission_id INTEGER,                  -- 关联的投稿 ID（如果通过）
    status TEXT DEFAULT 'pending',          -- pending/approved/rejected
    fingerprint_version INTEGER DEFAULT 1,  -- 指纹版本

    -- 索引优化
    created_at REAL DEFAULT (strftime('%s', 'now'))
);

-- 索引
CREATE INDEX idx_fp_user_id ON submission_fingerprints(user_id);
CREATE INDEX idx_fp_submit_time ON submission_fingerprints(submit_time);
CREATE INDEX idx_fp_content_hash ON submission_fingerprints(content_hash);
CREATE INDEX idx_fp_status ON submission_fingerprints(status);

-- 特征索引表（用于快速查找）
CREATE TABLE fingerprint_features (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint_id INTEGER NOT NULL,        -- 关联 submission_fingerprints.id
    feature_type TEXT NOT NULL,             -- url/tg_username/phone/email/etc
    feature_value TEXT NOT NULL,            -- 特征值（标准化后）
    created_at REAL DEFAULT (strftime('%s', 'now')),

    FOREIGN KEY (fingerprint_id) REFERENCES submission_fingerprints(id)
);

CREATE INDEX idx_ff_type_value ON fingerprint_features(feature_type, feature_value);
CREATE INDEX idx_ff_fingerprint ON fingerprint_features(fingerprint_id);
```

### 4.5 重复检测算法

```python
async def check_duplicate(self, fingerprint: SubmissionFingerprint) -> DuplicateResult:
    """
    重复检测算法：

    1. 精确匹配检测（高优先级）
       - 同一用户 7 天内再次投稿
       - URL 完全匹配
       - TG 链接/用户名完全匹配
       - 电话/邮箱完全匹配

    2. 模糊匹配检测（中优先级）
       - 内容 SimHash 相似度 > 阈值
       - URL 域名相同 + 路径相似

    3. 关联检测（低优先级）
       - 用户签名特征与历史投稿匹配
       - 不同用户但特征高度重合
    """

    # 计算时间窗口
    cutoff_time = time.time() - self.check_window

    # 1. 精确匹配
    exact_matches = await self._check_exact_matches(fingerprint, cutoff_time)
    if exact_matches:
        return DuplicateResult(
            is_duplicate=True,
            duplicate_type='exact',
            matched_features=exact_matches,
            similarity_score=1.0
        )

    # 2. 模糊匹配
    fuzzy_matches = await self._check_fuzzy_matches(fingerprint, cutoff_time)
    if fuzzy_matches and fuzzy_matches.score >= self.threshold:
        return DuplicateResult(
            is_duplicate=True,
            duplicate_type='fuzzy',
            matched_features=fuzzy_matches.features,
            similarity_score=fuzzy_matches.score
        )

    # 3. 关联检测
    related_matches = await self._check_related_submissions(fingerprint, cutoff_time)
    if related_matches:
        return DuplicateResult(
            is_duplicate=True,
            duplicate_type='related',
            matched_features=related_matches,
            similarity_score=0.9
        )

    return DuplicateResult(is_duplicate=False)
```

### 4.6 用户签名采集

在用户发起投稿时，通过 Telegram Bot API 获取用户信息：

```python
async def get_user_bio(self, user_id: int) -> Optional[str]:
    """获取用户签名（bio）"""
    try:
        # 使用 getChat API 获取用户详细信息
        chat = await self.bot.get_chat(user_id)
        return chat.bio  # 用户签名
    except Exception:
        return None
```

---

## 五、完整投稿流程（整合后）

```
/submit 命令
    ↓
[模式选择] ← 根据 BOT_MODE 配置
    ↓
[内容上传/输入]
    ├─ 纯文本：直接输入内容
    ├─ 媒体：上传图片/视频
    └─ 文档：上传文件
    ↓
[信息填写]
    ├─ 标签（必填）
    ├─ 链接（可选）
    ├─ 标题（可选）
    └─ 简介（可选）
    ↓
┌─────────────────────────────────────┐
│         重复投稿检测                 │
│  提取指纹 → 查询历史 → 判断重复      │
└─────────────────────────────────────┘
    ↓
[重复?] ──是──→ 拒绝投稿，通知用户
    │
    否
    ↓
┌─────────────────────────────────────┐
│           AI 内容审核                │
│  构建Prompt → 调用API → 解析结果     │
└─────────────────────────────────────┘
    ↓
[审核结果]
    ├─ 通过 → 发布到频道 → 保存指纹
    ├─ 拒绝 → 通知用户拒绝原因
    └─ 待审 → 转发管理员 → 等待人工审核
```

---

## 六、文件结构变更

```
TeleSubmit-v2/
├── config/
│   └── settings.py              # 修改：新增配置项读取
├── handlers/
│   ├── mode_selection.py        # 修改：新增纯文本模式选择
│   ├── text_handlers.py         # 新增：纯文本投稿处理
│   └── review_handlers.py       # 新增：审核流程处理
├── models/
│   └── state.py                 # 修改：新增状态常量
├── utils/
│   ├── ai_reviewer.py           # 新增：AI 审核模块
│   ├── duplicate_detector.py    # 新增：重复检测模块
│   └── feature_extractor.py     # 新增：特征提取模块
├── database/
│   └── db_manager.py            # 修改：新增表和查询方法
└── ui/
    ├── keyboards.py             # 修改：新增模式选择键盘
    └── messages.py              # 修改：新增提示消息
```

---

## 七、配置文件完整示例

```ini
[BOT]
TOKEN = your_bot_token
CHANNEL_ID = @your_channel
OWNER_ID = 123456789
BOT_MODE = ALL                      # 支持所有模式
TEXT_ONLY_MODE = true               # 允许纯文本
DEFAULT_SUBMIT_MODE = TEXT          # 默认纯文本模式
MIN_TEXT_LENGTH = 10
MAX_TEXT_LENGTH = 4000

[AI_REVIEW]
ENABLED = true
API_BASE_URL = https://api.openai.com/v1
API_KEY = sk-xxx
MODEL = gpt-4o-mini
TIMEOUT = 30
MAX_RETRIES = 2
CHANNEL_TOPIC = 接码服务
TOPIC_KEYWORDS = 接码,短信,验证码,SMS,号码,虚拟号,临时号
STRICT_MODE = false
AUTO_REJECT = true
NOTIFY_USER = true
CACHE_ENABLED = true
CACHE_TTL_HOURS = 24
FALLBACK_ON_ERROR = manual
NOTIFY_ADMIN_ON_REJECT = true
NOTIFY_ADMIN_ON_DUPLICATE = true

[DUPLICATE_CHECK]
ENABLED = true
CHECK_WINDOW_DAYS = 7
SIMILARITY_THRESHOLD = 0.8
CHECK_USER_ID = true
CHECK_URLS = true
CHECK_CONTACTS = true
CHECK_TG_LINKS = true
CHECK_USER_BIO = true
CHECK_CONTENT_HASH = true
AUTO_REJECT_DUPLICATE = true
NOTIFY_USER_DUPLICATE = true
RATE_LIMIT_ENABLED = true
RATE_LIMIT_COUNT = 3
RATE_LIMIT_WINDOW_HOURS = 24
```

---

## 八、用户交互消息设计

### 8.1 模式选择

```
📝 请选择投稿方式：

• 纯文本 - 直接发送文字内容
• 媒体 - 上传图片、视频、GIF
• 文档 - 上传文件附件
```

### 8.2 AI 审核反馈

**通过：**
```
✅ 投稿审核通过！
您的内容已发布到频道。
```

**拒绝：**
```
❌ 投稿未通过审核

原因：您的投稿内容与本频道「接码服务」主题无关。

本频道仅接受与接码、短信验证、虚拟号码相关的内容投稿。
如有疑问，请联系管理员。
```

### 8.3 重复检测反馈

```
⚠️ 检测到重复投稿

您的投稿与 {时间} 的历史投稿存在以下重复特征：
• 相同的 Telegram 链接：@xxx
• 相同的联系方式：xxx

为保证频道内容质量，7 天内相同内容不可重复投稿。
如有疑问，请联系管理员。
```

### 8.4 频率限制反馈

```
⚠️ 投稿频率超限

您在 24 小时内已投稿 3 次，已达到上限。
请稍后再试，或联系管理员。
```

---

## 九、管理员审核队列

当内容需要人工审核时，管理员收到的消息格式：

```
🔔 新投稿待审核

投稿人：@username (ID: 123456)
投稿时间：2025-01-01 12:00:00

内容：
{投稿内容}

标签：{tags}
链接：{link}

AI 审核结果：
• 置信度：0.65
• 分类：{category}
• 原因：{reason}

[✅ 通过] [❌ 拒绝] [🚫 拒绝并拉黑]
```

---

## 十、后续扩展建议

1. **审核日志**：记录所有审核结果，便于分析和优化
2. **白名单机制**：信任用户可跳过部分检测
3. **审核统计**：管理员可查看审核通过率、拒绝原因分布
4. **自定义审核规则**：支持正则表达式黑名单/白名单
5. **多频道支持**：不同频道可配置不同的审核主题
6. **审核结果反馈循环**：管理员的审核决策用于优化 AI 模型

---

## 十一、依赖项

新增依赖（需添加到 requirements.txt）：

```
openai>=1.0.0          # OpenAI SDK（兼容第三方 API）
simhash>=2.1.2         # SimHash 算法（内容相似度）
```

---

## 附录：API 兼容性说明

本设计使用 OpenAI SDK 的标准接口，兼容以下第三方服务：

- OpenAI 官方 API
- Azure OpenAI
- Anthropic Claude（通过兼容层）
- 本地部署的 LLM（如 Ollama、vLLM）
- 其他 OpenAI 兼容 API（如 DeepSeek、Moonshot 等）

只需修改 `API_BASE_URL` 和 `API_KEY` 即可切换不同的 API 提供商。
