# 定时发布 + 按钮广告位（Slot Ads）设计方案

> 文档版本：1.0  
> 创建日期：2025-12-16  
> 模块：定时发布（Scheduled Publish）/ 按钮广告位（10 Slots）/ 到期提醒（Opt-in）

---

## 1. 目标与非目标

### 1.1 目标

- 支持“定时发布消息”到指定频道：调度配置可热更新（无需重启）。
- 每条定时消息下方展示 N 行广告位（slot_1 ~ slot_N）：
  - N 可热更新（用于灵活增减展示行数），最大行数由配置决定（默认 20）。
  - 付费广告生效时展示用户按钮（文案 + URL）。
  - 未售出时展示购买入口；管理员可为同一行配置多个默认按钮，展示形如 `[默认1] [默认2] ... [购买]`（购买按钮可按行关闭）。
- 广告租期按“31 天”说明（本质为 `start_at/end_at` 时间段），与定时发布频率解耦（未来可从每日一次扩展到每 N 小时等）。
- 7 天续期保护窗：到期前 7 天仅允许当前广告主续期；他人尝试购买时提示“预计可购买时间”。
- 风控：
  - 使用现有 AI 审核能力做“轻度审核”：命中“儿童/未成年人、恐怖/血腥”等明确高风险则拒绝，其余默认通过。
  - 严重违规内容支持管理员“立即终止”且不退款（按规则约定）。
  - 终止后需要立刻修正“当天已发布”那条消息的按钮（edit reply_markup），并且后续定时发布不再展示该广告。
- 到期提醒（仅广告主）：默认关闭，用户自行开启；到期前 1 天私聊提醒一次（前提：用户与机器人有过私聊会话）。

### 1.2 非目标（YAGNI）

- 不做“候补到点提醒我”（非广告主订阅提醒）。
- 不做复杂“预售排队/竞价/多期日历预订”（先不卖未来多期库存）。
- 不引入“跳转中转链接/点击统计”（本期直接使用用户 URL）。
- 不做独立 Web 管理端（优先命令/按钮交互 + 数据落库实现热更新）。

---

## 2. 业务规则（已确认口径）

### 2.1 时区与调度

- 服务器时区为唯一口径。
- 定时发布策略需可配置并热更新：
  - `daily_at(HH:MM)`：每天固定时间。
  - `every_n_hours(N)`：每 N 小时一次。
  - 预留扩展：`weekly(dows, HH:MM)`、`monthly(dom, HH:MM)`（接口保持不变，逐步实现）。
- 系统维护 `next_run_at`，触发时发布并计算下一次执行时间。

### 2.2 广告位（slot）与展示规则

- 固定 10 个 slot：`slot_1 .. slot_10`，每个 slot 同一时刻最多 1 个生效广告（`active`）。
- 展示规则（每个 slot 独立判断）：
  1) 若存在生效广告（`now in [start_at, end_at)` 且状态为 active）：显示广告按钮（单按钮）。
  2) 否则：
     - 若管理员设置了默认按钮：同一行显示 `[默认按钮] [购买]`。
     - 若管理员未设置默认按钮：仅显示 `[购买]`。
- 购买入口需精确定位 slot：深链参数携带 `slot_id`（例如 `/start buy_slot_3`）。

### 2.3 生效时间与边界

- 广告购买完成后不要求“即时影响已发布消息”，但需保证：
  - 购买成功写入 `start_at/end_at`，并从“下一次定时发布”开始展示（若你选择“次日生效”，其本质为 `start_at = next_run_at`）。
- 为避免卡点争议，可设置“发送前冻结窗口”（例如 next_run_at 前 2~5 分钟不再接受新购买/续期）。

### 2.4 续期保护窗（7 天）

- 当 `now >= end_at - 7d`：
  - 仅当前广告主可续期该 slot。
  - 其他用户尝试购买时提示：`预计可购买时间 = end_at（服务器时区）`。
- 续期订单无缝衔接：`start_at = old_end_at`，`end_at = old_end_at + 31d`（或按套餐天数）。

### 2.5 终止与不退款

- 管理员可对 slot 当前生效广告执行“终止（terminate）”：
  - 状态进入 `terminated`（严重违规，不退款）。
  - 未来定时发布不再展示该广告。
  - 需立刻对“最近一次已发布的定时消息”调用 `edit_message_reply_markup` 移除/替换该按钮，使其变为“可购买/管理员默认”。

### 2.6 轻度 AI 审核

- 对广告素材（按钮文案 + URL）进行轻度审核：
  - 命中“儿童/未成年人”或“恐怖/血腥”等明确高风险分类：拒绝。
  - 其他：通过。
- 审核失败时提示用户原因，并允许重新提交素材。

### 2.7 到期提醒（广告主）

- 默认关闭，用户手动开启。
- 到期前 1 天提醒一次：
  - 仅向广告主（订单 buyer）发送私聊消息。
  - 如果用户未与机器人开启私聊/已屏蔽，发送可能失败；失败需落库避免无限重试。

---

## 3. 数据模型（最小可用）

> 存储介质：SQLite（现有 `database/db_manager.py`）

### 3.1 调度配置

- `scheduled_publish_config`
  - `id INTEGER PRIMARY KEY CHECK (id = 1)`
  - `enabled INTEGER NOT NULL DEFAULT 0`
  - `schedule_type TEXT NOT NULL`（daily_at / every_n_hours / weekly / monthly）
  - `schedule_payload TEXT NOT NULL`（JSON，存参数）
  - `message_text TEXT NOT NULL`（定时消息正文，HTML 或纯文本策略由实现决定）
  - `next_run_at REAL`（epoch seconds）
  - `last_run_at REAL`
  - `last_message_chat_id INTEGER`
  - `last_message_id INTEGER`
  - `updated_at REAL`

### 3.2 广告位默认按钮（管理员配置）

- `ad_slots`
  - `slot_id INTEGER PRIMARY KEY`（1..10）
  - `default_text TEXT`
  - `default_url TEXT`
  - `sell_enabled INTEGER NOT NULL DEFAULT 1`
  - `updated_at REAL`

### 3.3 广告素材与订单

- `slot_ad_creatives`
  - `id INTEGER PRIMARY KEY AUTOINCREMENT`
  - `user_id INTEGER NOT NULL`
  - `button_text TEXT NOT NULL`
  - `button_url TEXT NOT NULL`
  - `ai_review_result TEXT`（JSON，可为空）
  - `ai_review_passed INTEGER`（0/1，可为空）
  - `created_at REAL NOT NULL`

- `slot_ad_orders`
  - `id INTEGER PRIMARY KEY AUTOINCREMENT`
  - `slot_id INTEGER NOT NULL`
  - `buyer_user_id INTEGER NOT NULL`
  - `creative_id INTEGER NOT NULL`
  - `status TEXT NOT NULL`（created/paid/active/expired/terminated）
  - `start_at REAL`
  - `end_at REAL`
  - `created_at REAL NOT NULL`
  - `paid_at REAL`
  - `terminated_at REAL`
  - `terminate_reason TEXT`
  - `reminder_opt_in INTEGER NOT NULL DEFAULT 0`
  - `remind_at REAL`
  - `remind_sent INTEGER NOT NULL DEFAULT 0`
  - `remind_sent_at REAL`

索引建议：
- `slot_ad_orders(slot_id, status, start_at, end_at)`
- `slot_ad_orders(buyer_user_id, status)`
- `slot_ad_creatives(user_id, created_at)`

---

## 4. 管理员操作（命令/回调）

### 4.1 定时发布管理

- 查看配置：`/sched_status`
- 开启/关闭：`/sched_on`、`/sched_off`
- 设置正文：`/sched_set_text <text...>`
- 设置 daily：`/sched_daily HH:MM`
- 设置 every_n_hours：`/sched_every_hours N`
- （可选）立即执行一次：`/sched_run_now`

### 4.2 广告位管理

- 设置默认按钮：`/slot_set_default <slot_id> <text> <url>`
- 清空默认按钮：`/slot_clear_default <slot_id>`
- 终止当前广告：`/slot_terminate <slot_id> <reason?>`
- 查看 slot 状态：`/slot_status [slot_id]`

权限：仅 OWNER/ADMIN 可执行（复用现有 is_owner/ADMIN_IDS 机制）。

---

## 5. 用户流程（购买与续期）

> 支付网关可复用现有 UPAY_PRO 体系；本设计只定义流程与状态，具体网关细节复用既有实现。

### 5.1 购买入口

- 用户点击频道消息下的“购买”按钮 → 私聊机器人（深链带 slot_id）。
- 机器人引导用户提交：
  - 按钮文案（限制长度、禁止换行/控制字符）
  - URL（仅允许 https，限制长度）
- 轻度 AI 审核通过后创建订单并引导支付。

### 5.2 生效规则（本期默认）

- 购买成功后写入：
  - `start_at = next_run_at`（下一次定时发送时间）
  - `end_at = start_at + 31d`
- 若在“冻结窗口”内完成支付：自动顺延到下一次（以避免定时任务并发与口径争议）。

### 5.3 续期

- 只有当前广告主在保护窗（到期前 7 天）内可续期：
  - 续期订单 `start_at = old_end_at`，无缝衔接。
- 他人尝试购买时提示 `end_at`（预计可购买时间），不提供“候补提醒”。

### 5.4 到期提醒（可选）

- 用户在订单详情中选择“开启到期前 1 天提醒”。
- 到期前 1 天发送一次私聊提醒（带一键续期入口）。

---

## 6. 关键实现要点（可维护性）

- **热更新**：所有可变内容（调度参数、定时消息正文、slot 默认按钮、订单/素材）均落库；渲染/触发时从 DB 读取（可加短 TTL 缓存，但必须支持失效）。
- **幂等与并发**：
  - 定时发布任务需避免重复执行（以 `next_run_at` 与事务更新为锁）。
  - 续期与购买需确保同一 slot 同一时间只有一个 active 订单（用事务 + 条件写入/查询判定）。
- **最小风险控制**：
  - 终止后立即 edit 已发布消息的键盘（仅改 reply_markup，不改正文/caption）。
  - 终止原因落库，便于审计与售后。
