# 付费广告发布（UPAY_PRO）设计方案

> 文档版本：1.0  
> 创建日期：2025-12-13  
> 模块：AI 拒绝引导付费发布 / 广告发布次数（credits）/ UPAY_PRO 支付回调

---

## 1. 背景与目标

当前项目已支持 AI 审核，并在“主题无关（off-topic）”时自动拒绝投稿。新增能力目标：

- 当 AI 判定投稿与频道主题无关而拒绝时，引导用户可付费购买“广告发布次数”。
- 用户支付成功后，次数余额入账；用户可在未来任意时间自行发布“频道主题无关”的广告内容。
- 全局可配置开启/关闭，关闭时维持现有拒绝逻辑不变。
- 支付网关优先直接接入 `UPAY_PRO`（加密货币网关），后续可扩展到“易支付”等其他网关。

非目标（YAGNI）：

- 不在“拒绝分支”保留投稿内容快照并自动发布（用户选择购买后重新提交广告内容）。
- 不做复杂积分商城/多商品体系，仅实现“广告发布次数（整数）”。

---

## 2. 业务流程（用户视角）

### 2.1 AI 拒绝引导

触发条件：
- AI 审核结果分类命中“无关/off-topic/irrelevant”并进入自动拒绝分支。

用户提示（示例）：
- “投稿未通过：主题无关。若需发布广告，可购买广告发布次数（可批量购买，随时使用）。”
- 提供按钮：`购买广告次数`、`查看余额`、`广告发布 /ad`

### 2.2 购买次数（套餐 SKU）

提供固定套餐（可配置扩展）：
- `1 次 = 10 USDT`
- `15 次 = 100 USDT`

购买流程：
1. 用户选择套餐
2. 系统创建订单（本地 `ad_orders`，状态 `created`）
3. 调用 `UPAY_PRO /api/create_order` 获取 `payment_url`
4. Bot 回复用户支付链接（可附“我已支付”兜底按钮）
5. 支付完成后 `UPAY_PRO` 回调 `notify_url` → 本地入账次数

### 2.3 发布广告

用户命令 `/ad` 进入广告发布流程：
- 若余额不足：提示先购买
- 若余额充足：按现有投稿流程收集内容并发布到频道
- 发布成功后扣减 1 次
- 发布消息增加明确标识（如前缀 `📢 广告`），避免与主题内容混淆

---

## 3. 配置设计（占位可改）

建议新增配置段（`config.ini` / 环境变量优先）：

```ini
[PAID_AD]
ENABLED = false

# 套餐（次数:金额），逗号分隔
PACKAGES = 1:10,15:100
CURRENCY = USDT
PUBLISH_PREFIX = 📢 广告

# UPAY_PRO 网关
UPAY_BASE_URL = http://127.0.0.1:8090      # 占位：部署后替换为实际地址（建议 HTTPS/内网）
UPAY_SECRET_KEY =                            # 占位：部署后填写
UPAY_DEFAULT_TYPE = USDT-TRC20               # 默认币种
UPAY_ALLOWED_TYPES = USDT-TRC20,TRX,USDT-BSC,USDT-ERC20,USDT-Polygon

# 对外回调与跳转（占位：部署后替换为实际域名）
# 约定：默认复用现有 WEBHOOK_URL（RUN_MODE=WEBHOOK 时通常已配置为公网 https 域名）
# 若 RUN_MODE=POLLING 且仍需接收回调，则需要额外配置对外域名（或仅使用“查单兜底”）
PUBLIC_BASE_URL = https://example.com
UPAY_NOTIFY_PATH = /pay/notify/upay
UPAY_REDIRECT_PATH = /pay/return

# 订单过期（用于 UI 提示/清理）
PAY_EXPIRE_MINUTES = 30
```

说明：
- `PUBLIC_BASE_URL` 是 Bot 服务对外 HTTPS 域名（用于 `notify_url`），与 `UPAY_BASE_URL`（支付网关地址）不同。
- `UPAY_DEFAULT_TYPE` 默认为 `USDT-TRC20`；币种是网关侧展示/收款地址选择，不影响“次数”本身。

---

## 4. 支付网关集成（UPAY_PRO）

### 4.1 创建订单

请求：
- `POST {UPAY_BASE_URL}/api/create_order`
- `Content-Type: application/json`

Body（参考 UPAY_PRO 文档）：

```json
{
  "type": "USDT-TRC20",
  "order_id": "AD20251213XXXXXX",
  "amount": 100.0,
  "notify_url": "https://<PUBLIC_BASE_URL>/pay/notify/upay",
  "redirect_url": "https://<PUBLIC_BASE_URL>/pay/return",
  "signature": "<md5>"
}
```

响应关键字段：
- `data.trade_id`：网关交易号（用于查单/展示）
- `data.payment_url`：支付页 URL（二维码/倒计时等）
- `data.expiration_time`：过期时间戳（UPAY_PRO 为 UnixMilli 毫秒）
- `data.actual_amount`：实际应支付金额（网关为避免冲突会做“递增金额”调整，用户必须按该金额支付）
- `data.token`：收款地址（钱包地址）

UPAY_PRO create_order 响应结构（源码 046447fa）：
- 顶层：`status_code`、`message`、`data`
- `data`：`trade_id`、`order_id`、`amount`、`actual_amount`、`token`、`expiration_time`、`payment_url`

### 4.2 异步回调（notify_url）

回调：
- `POST {PUBLIC_BASE_URL}{UPAY_NOTIFY_PATH}`
- `Content-Type: application/json`

成功条件：
- `status == 2` 表示支付成功
- 我方返回 body 包含 `"ok"` 或 `"success"`（建议固定返回 `"ok"`）

幂等要求：
- 同一 `order_id` 可能回调多次（UPAY_PRO 自带最多 5 次重试），必须幂等处理，确保只入账一次。

回调字段（来自 UPAY_PRO 的回调实现，排除 `signature` 后参与签名）：
- `trade_id`：网关订单号
- `order_id`：商户订单号
- `amount`：下单金额（原始金额）
- `actual_amount`：实际收款金额
- `token`：收款地址（钱包地址）
- `block_transaction_id`：链上交易哈希
- `status`：`1=待支付`、`2=支付成功`、`3=过期`
- `signature`：MD5 签名

### 4.4 Bot 侧支付信息展示（增强体验，保留网页支付兜底）

为降低跳转成本，Bot 可在“创建订单成功”后直接在 Telegram 展示：
- 收款二维码（由 Bot 本地生成，内容通常为收款地址）
- 收款地址（`token`）
- 应付金额（`actual_amount`，必须严格按该金额支付）
- 订单有效期（`expiration_time`，注意毫秒/秒单位换算）

同时保留 `payment_url` 的 “打开支付页” 按钮作为兜底（支付页包含同样信息与倒计时）。

### 4.3 查单兜底

UPAY_PRO 提供：
- `GET {UPAY_BASE_URL}/pay/check-status/{trade_id}`

用途：
- 作为“我已支付”按钮兜底（回调丢失/延迟时手动触发确认并入账）
- 不作为主流程依赖（主流程以回调为准）

---

## 5. 签名与验签（安全与兼容）

### 5.1 下单签名（请求签名）

UPAY_PRO 文档定义的参数串（不含 signature）：

```
type={type}&amount={amount}&notify_url={notify_url}&order_id={order_id}&redirect_url={redirect_url}
```

规则：
1. 将参数按字母序排序
2. 使用 `&` 连接
3. 将密钥拼接到末尾后做 MD5

注意：金额建议格式化为固定小数位（如 `"{:.2f}"`），避免签名因浮点展示差异不一致。

### 5.2 回调验签（回调签名）

UPAY_PRO 回调字段（排除 signature）会按字母序参与签名；其实现逻辑为：
1. 仅收集“非空字段”的 `key=value`
2. 按 key 字母序排序，使用 `&` 拼接为参数串
3. 在末尾直接拼接密钥，再做 MD5

注意：部分文档/示例会写成 `params + "&" + key` 的形式。为兼容差异，建议实现策略（KISS + 兼容）：
- 同时支持两种验签方式：`md5(params + key)` 与 `md5(params + "&" + key)`，任一匹配即通过。
- 通过后仍需校验：`order_id` 存在、金额匹配（优先对 `amount` 做一致性校验，必要时记录 `actual_amount` 以便对账）、状态为 `2`。

---

## 6. 数据模型（最小可用：次数余额 + 订单 + 账本）

为避免重复入账、支持对账审计，建议新增三张表：

### 6.1 用户余额表

- `user_ad_credits(user_id PRIMARY KEY, balance INTEGER NOT NULL, updated_at REAL)`

### 6.2 订单表

- `ad_orders(out_trade_no PRIMARY KEY, user_id, sku_id, credits, amount, currency, status, upay_trade_id, payment_url, expires_at, created_at, paid_at)`

状态建议：
- `created`：已创建但未支付
- `paid`：已支付且已入账
- `expired/canceled`：超时或取消（可选）

### 6.3 账本表（幂等与审计）

- `ad_credit_ledger(id AUTOINCREMENT, out_trade_no UNIQUE, user_id, delta INTEGER, reason TEXT, created_at REAL)`

约束：
- `out_trade_no UNIQUE` 确保同一订单不会重复入账（即使回调重放/重复查单）

---

## 7. 扣减策略（防并发与失败补偿）

广告发布扣减建议采用“预扣 + 失败退回”：

1. 发布前：在事务中检查余额 `>=1` 并扣减 1，记录账本 `delta=-1 reason=consume`（或先记录 `reserved`）
2. Telegram 发布成功：标记消费完成（可写 `published_message_id`）
3. 发布失败：退回 1 次（账本 `delta=+1 reason=refund`）

目标：
- 防止并发重复发导致扣减错乱
- 发布失败不吞额度，降低售后成本

SQLite 原子扣减建议（落地细节）：
- 通过单条 SQL 原子扣减：`UPDATE user_ad_credits SET balance = balance - 1 WHERE user_id = ? AND balance >= 1`
- 以 `rowcount == 1` 判定扣减成功；否则余额不足
- 发布失败时再写一条 `+1` 账本并回补余额（同事务）

---

## 8. 服务端回调承载（端口/路径）

约定回调路径：
- `POST {PUBLIC_BASE_URL}/pay/notify/upay`

端口建议（最省改动）：
- 复用现有 Telegram Webhook aiohttp server 的端口（`WEBHOOK_PORT`）
- 在同一个 aiohttp app 上增加回调路由

注意：
- Telegram Webhook 的鉴权头为 `X-Telegram-Bot-Api-Secret-Token`，支付回调不可复用该校验逻辑，应采用 UPAY_PRO 的签名验签。

运行模式影响：
- 生产建议 `WEBHOOK`：HTTP server 已存在，回调可直接接入
- 若开发期使用 `POLLING`：需额外启动 HTTP server 才能接回调；否则只能依赖“查单兜底”

---

## 9. 风控与合规建议（最低限度）

即使允许主题无关广告，也建议保留：
- 黑名单/频率限制仍生效（防刷屏）
- 基础敏感词/诈骗关键词拦截（必要时转人工）
- 明确广告标识（`PUBLISH_PREFIX`）

实现约定（已确认）：
- `/ad` 发布流程跳过 AI 审核与人工审核（即：不再做主题相关性判断），但应保留黑名单/频率限制等既有风控（避免买次数变成“无限制刷屏”）。

---

## 10. 未来扩展：对接“易支付”等网关

建议抽象接口：
- `PaymentProvider.create_order(sku, user) -> payment_url/trade_id/expires_at`
- `PaymentProvider.verify_notify(payload) -> (ok, out_trade_no, status, amount, trade_id)`
- `PaymentProvider.query_status(trade_id|out_trade_no) -> paid?`

后续新增“易支付”时仅新增实现类与配置，不改变“广告次数余额/扣减/发布”核心逻辑（SOLID：依赖抽象）。
