# TeleSubmit v2

> 功能强大的 Telegram 频道投稿机器人，支持媒体上传、全文搜索、热度统计

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org)
[![Telegram Bot](https://img.shields.io/badge/Telegram-Bot-blue.svg)](https://core.telegram.org/bots)
[![Docker](https://img.shields.io/badge/Docker-Supported-blue.svg)](https://www.docker.com)

**TeleSubmit v2** 是一个开源的 Telegram 频道内容管理系统，专为频道管理员和内容创作者设计。支持用户投稿、自动审核、全文搜索、热度统计等功能，帮助您高效管理 Telegram 频道内容。

---

## 核心功能

- **投稿管理** - 支持图片、视频、文档批量上传，**新增纯文本投稿模式**
- **AI 智能审核** - 基于 OpenAI 兼容 API 自动审核内容相关性，支持自动通过/拒绝/人工审核
- **重复投稿检测** - 多维特征识别（URL、TG链接、联系方式等），防止 7 天内重复投稿
- **频道监听** - 自动监听频道消息，智能提取标签和内容，自动同步到数据库和搜索索引
- **全文搜索** - 基于 Whoosh 搜索引擎，中文分词优化（jieba/simple），支持标题/标签/文件名多字段搜索
- **热度统计** - 智能热度算法，自动生成排行榜，支持按时间范围筛选
- **标签系统** - 标签云可视化，快速分类内容，支持标签搜索和统计
- **权限管理** - 黑名单系统，灵活控制用户权限，支持管理员命令
- **双模式运行** - 支持 Polling 和 Webhook 两种运行模式，适应不同部署场景
- **容器化部署** - 提供 Docker 和 Docker Compose 支持，一键部署
- **数据持久化** - 基于 SQLite 数据库，支持数据备份和恢复
- **内存优化** - 支持多种内存模式，最低仅需 80-120 MB 内存

---

## 快速开始

### 一键部署（推荐新手）

```bash
git clone https://github.com/lookoupai/TeleSubmit-v2.git
cd TeleSubmit-v2
./quickstart.sh  # 智能检测环境，自动引导部署
```

### 其他部署方式

<details>
<summary><b>完整安装向导</b></summary>

```bash
cp config.ini.example config.ini
nano config.ini  # 编辑配置
./deploy.sh
```

</details>

<details>
<summary><b>手动部署</b></summary>

```bash
pip3 install -r requirements.txt
cp config.ini.example config.ini
nano config.ini
./start.sh
```

</details>

---

## 基本配置

编辑 `config.ini`，填入以下必填项：

```ini
[BOT]
TOKEN = your_bot_token_here        # 从 @BotFather 获取
CHANNEL_ID = @your_channel         # 频道用户名或 ID
OWNER_ID = 123456789               # 管理员 User ID
```

<details>
<summary>如何获取配置信息？</summary>

- **Bot Token**: 向 [@BotFather](https://t.me/BotFather) 发送 `/newbot` 创建机器人
- **Channel ID**: 频道用户名（如 `@mychannel`）或数字 ID
- **Owner ID**: 向 [@userinfobot](https://t.me/userinfobot) 发送消息获取

</details>

### 运行模式选择

| 模式 | 适用场景 | 优点 | 缺点 |
|------|----------|------|------|
| **Polling**（默认） | 本地开发、测试 | 配置简单 | 延迟 1-3 秒 |
| **Webhook** | 生产环境、云服务器 | 响应快（<1秒） | 需 HTTPS 域名 |

<details>
<summary>Webhook 模式配置</summary>

```ini
[BOT]
RUN_MODE = WEBHOOK

[WEBHOOK]
URL = https://your-domain.com
PORT = 8080
PATH = /webhook
```

详见 [Webhook 模式完整指南](docs/WEBHOOK_MODE.md)

</details>

<details>
<summary>AI 审核配置（可选）</summary>

```ini
[AI_REVIEW]
ENABLED = true                              # 启用 AI 审核
API_BASE_URL = https://api.openai.com/v1    # OpenAI 兼容 API
API_KEY = sk-xxx                            # API 密钥
MODEL = gpt-4o-mini                         # 模型名称
CHANNEL_TOPIC = 你的频道主题                 # 频道主题描述
TOPIC_KEYWORDS = 关键词1,关键词2             # 主题关键词
AUTO_REJECT = true                          # 自动拒绝不相关内容
```

支持 OpenAI、Azure、Claude（兼容层）、本地 LLM 等。

</details>

<details>
<summary>重复投稿检测配置（可选）</summary>

```ini
[DUPLICATE_CHECK]
ENABLED = true                      # 启用重复检测
CHECK_WINDOW_DAYS = 7               # 检测时间窗口（天）
SIMILARITY_THRESHOLD = 0.8          # 相似度阈值
AUTO_REJECT_DUPLICATE = true        # 自动拒绝重复投稿
RATE_LIMIT_ENABLED = true           # 启用频率限制
RATE_LIMIT_COUNT = 3                # 24小时内最大投稿次数
```

</details>

---

## 常用命令

### 管理脚本

| 命令 | 说明 | 使用场景 |
|------|------|----------|
| `./start.sh` | 启动机器人 | 首次启动 |
| `./restart.sh` | 重启机器人 | 修改配置后 |
| `./update.sh` | 更新代码 | 定期维护 |
| `./deploy.sh` | Docker 部署 | 生产环境 |

更多脚本请查看 [脚本使用指南](SCRIPTS_GUIDE.md)

**Docker 镜像说明**：
- 默认使用 `ghcr.io/lookoupai/telesubmit-v2:latest`（`./deploy.sh` 会自动 `docker-compose pull`）
- 若镜像为私有包，部署机需先执行 `docker login ghcr.io`
- 生产环境建议固定标签（例如 `sha-xxxxxxx`）：`TELESUBMIT_IMAGE="ghcr.io/lookoupai/telesubmit-v2:sha-ffa158c" docker-compose up -d`

### 用户命令

| 命令 | 说明 |
|------|------|
| `/submit` | 开始新投稿 |
| `/search <关键词>` | 搜索内容 |
| `/hot [时间]` | 查看热门排行 |
| `/mystats` | 我的统计 |
| `/slot_edit <订单号>` | 修改按钮广告内容（Slot Ads） |
| `/help` | 查看帮助 |

<details>
<summary>管理员命令</summary>

| 命令 | 说明 |
|------|------|
| `/debug` | 系统状态 |
| `/blacklist_add <ID>` | 添加黑名单 |
| `/blacklist_list` | 查看黑名单 |
| `/searchuser <ID>` | 查询用户投稿 |

</details>

---

## 按钮广告位（Slot Ads）内容修改

当你购买了“定时消息下方按钮广告位（Slot Ads）”后，如果链接失效/需要换文案，可以在广告有效期内自行修改。

### 用户自助修改（Telegram 私聊）

1. 找到“按钮广告位支付成功通知”的私聊消息，点击其中的 `修改广告内容` 按钮
2. 按提示依次发送新的 `按钮文案` 与 `https://链接`

也可以直接发送命令：

```text
/slot_edit SLTxxxxxx
```

说明：
- 允许修改“已支付待生效（start_at 未到）”与“展示中”的订单；到期后不可修改
- 默认每单每天允许修改 1 次（可在管理后台调整）
- 修改后会尝试立刻刷新“最近一次定时消息”的按钮键盘；若 Telegram 不允许编辑旧消息，则会在下一次定时消息发送时生效

### 管理员后台代修改（Web 后台）

适用于用户不会使用机器人自助修改的场景：
1. 打开管理后台：`广告位` 页面（`/slots`）
2. 在对应 slot 的“展示中/已支付待生效”订单卡片中填写新的按钮文案与链接并保存
3. 如需绕过次数限制，可勾选“强制（忽略限制）”

修改次数限制配置：
- 管理后台 `广告参数` 页面（`/ads`）可配置 `每单每天允许修改次数（0=不限制）`

---

## 投稿流程

### 纯文本投稿（默认）

```
1. 发送 /submit
   ↓
2. 直接输入投稿内容
   ↓
3. 输入标签（必填）
   ↓
4. 输入链接（可选，/skip_optional 跳过）
   ↓
5. AI 自动审核 → 发布到频道 ✅
```

### 媒体/文档投稿

```
1. 发送 /submit
   ↓
2. 选择类型（媒体/文档/混合）
   ↓
3. 上传文件
   ↓
4. 发送 /done_media 或 /done_doc
   ↓
5. 输入标签（必填）
   ↓
6. 输入可选信息（链接、标题、说明）
   ↓
7. 预览并确认
   ↓
8. 发布到频道
```

---

## 搜索功能

### 基础搜索

```
/search Python          # 搜索关键词
/search #编程           # 搜索标签
/search 文件.txt        # 搜索文件名
```

### 高级搜索

```
/search Python -t week      # 限定时间（day/week/month/all）
/search 教程 -n 20          # 限定结果数量
/search #Python -t month -n 15  # 组合使用
```

**搜索特性**:
- 中文分词优化（jieba/simple 可选）
- 中文部分匹配支持（搜索"卫宫"可匹配"卫宫士郎"）
- 多字段匹配（标题/描述/标签/文件名）
- 按相关度和热度排序
- 自动索引管理和同步

---

## 系统要求

### 最低配置

- **操作系统**: Linux / macOS / Windows (WSL2)
- **Python**: 3.9+（推荐 3.11）
- **内存**: 256 MB（优化后可低至 80-120 MB）
- **磁盘**: 100 MB 以上

### 推荐配置

- **内存**: 512 MB
- **磁盘**: 2 GB
- **CPU**: 1 核

### Docker 部署

- **Docker**: >= 20.10
- **Docker Compose**: >= 2.0
- **内存**: 512 MB（容器限制）

---

## 内存优化

<details>
<summary>查看内存优化方案</summary>

### 智能分词器切换（v2.1+）

修改配置后自动适配，无需手动操作。详见 [内存优化指南](MEMORY_USAGE.md)。

```ini
# config.ini
[SEARCH]
ANALYZER = simple  # 轻量级，节省 ~140MB 内存
# ANALYZER = jieba # 高质量中文分词
```

重启后自动完成索引重建！

### 模式切换脚本

```bash
./switch_mode.sh minimal      # 极致省内存 (~80-120 MB)
./switch_mode.sh balanced     # 均衡模式 (~150-200 MB)
./switch_mode.sh performance  # 性能优先 (~200-350 MB)
```

详见 [内存优化指南](MEMORY_USAGE.md)

</details>

---

## 文档导航

| 文档 | 说明 |
|------|------|
| [脚本指南](SCRIPTS_GUIDE.md) | 所有管理脚本详细说明 |
| [部署指南](DEPLOYMENT.md) | 详细部署步骤、故障排查 |
| [Webhook 模式](docs/WEBHOOK_MODE.md) | Webhook 完整配置指南 |
| [Fly.io 部署](docs/FLYIO_DEPLOYMENT.md) | Fly.io 云平台部署指南 |
| [PythonAnywhere 部署](docs/PYTHONANYWHERE_DEPLOYMENT.md) | PythonAnywhere 部署指南 |
| [AI 审核设计](docs/TEXT_MODE_AI_REVIEW_DESIGN.md) | AI 审核与重复检测技术文档 |
| [管理员指南](ADMIN_GUIDE.md) | 管理功能、系统维护 |
| [内存优化](MEMORY_USAGE.md) | 内存使用分析与优化 |
| [测试指南](TESTING.md) | 测试框架、编写和运行测试 |
| [更新日志](CHANGELOG.md) | 版本历史、功能更新 |

### 推荐阅读顺序

1. **首次部署**: README → [脚本指南](SCRIPTS_GUIDE.md) → [部署指南](DEPLOYMENT.md)
2. **日常使用**: README（命令部分）
3. **系统维护**: [管理员指南](ADMIN_GUIDE.md)
4. **生产部署**: [Webhook 模式指南](docs/WEBHOOK_MODE.md)
5. **开发测试**: [测试指南](TESTING.md)

---

## 测试

项目包含完整的测试套件，覆盖核心功能模块。

### 快速测试

```bash
# 运行所有测试
pytest

# 生成覆盖率报告
pytest --cov=. --cov-report=html
open htmlcov/index.html

# 使用 Makefile
make test-cov
```

### 测试覆盖

- **热度计算器**: 86% 覆盖率，9个测试用例
- **UI消息格式化**: 90% 覆盖率，30个测试用例  
- **文件验证器**: 100% 覆盖率，15个测试用例
- **工具函数**: 98% 覆盖率，10+个测试用例

详见 [完整测试指南](TESTING.md) 和 [测试套件说明](tests/README.md)

---

## 故障排查

<details>
<summary>机器人无法启动？</summary>

请查看 [部署指南 - 故障排查](DEPLOYMENT.md#故障排查) 章节。

</details>

<details>
<summary>搜索功能异常？</summary>

请查看 [索引管理器文档](INDEX_MANAGER_README.md)。

```bash
# 重建搜索索引
python3 utils/index_manager.py rebuild
```

</details>

<details>
<summary>热度数据不更新？</summary>

- 热度每小时自动更新一次
- 手动触发：`./restart.sh`

</details>

更多问题请查看 [部署指南](DEPLOYMENT.md) 的故障排查章节。

---

## 项目结构

<details>
<summary>查看项目结构</summary>

```
TeleSubmit-v2/
├── config/              # 配置管理
├── handlers/            # 消息处理器
│   ├── command_handlers.py
│   ├── submit_handlers.py
│   ├── search_handlers.py
│   └── ...
├── utils/               # 工具模块
│   ├── database.py
│   ├── search_engine.py
│   └── ...
├── ui/                  # 用户界面
├── data/                # 数据目录
├── logs/                # 日志目录
├── main.py              # 主程序入口
└── *.sh                 # 管理脚本
```

</details>

---

## 技术栈

- **语言**: Python 3.9+
- **框架**: python-telegram-bot 21.10+
- **AI 审核**: OpenAI SDK 1.0+ (兼容第三方 API)
- **搜索引擎**: Whoosh 2.7.4+
- **中文分词**: jieba 0.42.1+ (可选)
- **数据库**: SQLite (通过 aiosqlite)
- **容器化**: Docker & Docker Compose
- **Web 框架**: aiohttp (Webhook 模式)

## 许可证

本项目采用 [MIT 许可证](LICENSE)。

## 适用场景

- **内容社区管理**: 管理用户投稿，审核内容质量
- **资源分享频道**: 组织和管理分享的资源，支持搜索和分类
- **新闻资讯频道**: 收集和整理新闻资讯，支持标签分类
- **学习资料频道**: 管理学习资料，支持全文搜索和热度排序
- **技术分享频道**: 技术文章和代码分享，支持标签和搜索

## 帮助与支持

- **问题反馈**: [GitHub Issues](https://github.com/lookoupai/TeleSubmit-v2/issues)
- **功能建议**: [GitHub Discussions](https://github.com/lookoupai/TeleSubmit-v2/discussions)
- **开发者**: [@zoidberg-xgd](https://github.com/zoidberg-xgd)

---

<div align="center">

**⭐ 如果觉得有用，请给个 Star！**

Made by [zoidberg-xgd](https://github.com/zoidberg-xgd)

</div>
