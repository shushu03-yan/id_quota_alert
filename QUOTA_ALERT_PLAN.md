# HKID 公开配额提醒服务方案

## 1. 项目定位

本项目提供 **HKID 公开预约配额变化提醒**。服务读取获准使用的 GovHK / 入境事务处公开配额信息，在平台识别到符合用户预约目标的新事件后发送通知；用户仍需自行进入官方系统预约。

项目不是“代抢服务”或“自动预约工具”。产品价值是：**帮助普通用户及时知道公开名额变化，减少反复手动刷新网页的时间成本**。

项目边界：

- 不自动预约、不代抢、不提交预约表格。
- 不控制官方预约页面。
- 不绕过验证码、排队、限流或其他安全机制。
- 不保存 HKID、旅行证件号码、出生日期、签证编号或查询代码。
- 不承诺提醒绝对实时、邮件必达、数据绝对准确或预约一定成功。
- 不冒充或暗示与香港政府、入境事务处存在隶属关系。
- 不以更高官网轮询频率、优先队列或“更快抢号”作为收费卖点。

---

## 2. 产品模型：任务型服务，而非长期 SaaS

HKID 预约提醒属于典型的 **单次任务型产品（task-oriented utility）**。用户购买的不是“一个月的软件订阅”，而是希望在近期完成一次预约任务。

因此 V1 套餐设计遵循：

1. 服务周期保持短，不设置 30 天主力订阅。
2. 所有套餐共享同一份配额采集结果和同一条通知链路，通知速度不分等级。
3. 套餐主要通过 **服务周期、预约目标数量、接收邮箱数量、是否享受无匹配提醒延长保障** 区分。
4. 前台使用“预约目标”而不是“规则组 / ruleset”等技术术语。
5. Family 的价值来自多人协同，不来自更长服务期或更高抓取频率。

---

## 3. V1 套餐矩阵

V1 对中国内地用户测试阶段使用人民币（CNY / RMB）定价。价格是当前试运营基准，可根据真实数据调整。

| 维度 | 🌱 1 日体验 Trial | ⚡ 3 日快速 Quick | 🎯 14 日目标 Goal | 👨‍👩‍👧 Family（隐藏/手动） |
|---|---:|---:|---:|---:|
| 价格 | **¥6** | **¥18** | **¥59** | **¥99** |
| 服务周期 | 24 小时 | 3 天 | 14 天 | 14 天 |
| 预约目标 | 1 个简单目标 | 最多 3 个 | 最多 6 个 | 最多 10 个 |
| 日期筛选 | 简化 | 完整 | 完整 | 完整 |
| 地点筛选 | 1 个办事处或全部 | 自由选择 | 自由选择 | 自由选择 |
| 名额状态 | 基础 | 可设置 | 可设置 | 可设置 |
| 接收邮箱 | 1 个 | 1 个 | 1 个 | 最多 3 个 |
| 配额事件提醒链路 | 相同 | 相同 | 相同 | 相同 |
| 激活测试邮件 | 有 | 有 | 有 | 有 |
| 无匹配提醒延长保障 | 无 | 无 | **自动 +7 天，最多一次** | **自动 +7 天，最多一次** |

### 3.1 公开销售策略

V1 产品页只公开三个个人套餐：

- ¥6 / 1 日体验
- ¥18 / 3 日快速
- ¥59 / 14 日目标（主推）

Family 暂不在 V1 产品页公开展示。数据模型和 CLI 可预留支持；如果用户主动咨询多人同时接收提醒，可由管理员以 ¥99 手动开通。积累至少 3–5 个真实 Family 需求后，再决定是否正式上架。

---

## 4. “预约目标”的定义

用户侧统一使用 **预约目标（Appointment Target）**，后台可以使用 `target` / `ruleset` 等技术名称。

一个预约目标由以下条件组成：

```text
日期范围
+ 可接受的办事处集合
+ 最低提醒状态（limited / available）
```

例如：

```text
目标 A
9/1–9/10
沙田 / 火炭
少量名额及以上

目标 B
9/1–9/15
所有办事处
仅 available
```

“最多 3 个 / 6 个目标”限制的是 **不同策略组合的数量**，不是限制单个目标内部只能选择一个办事处。

### 4.1 Trial 的简单目标

1 日体验只允许一个简单目标，建议限制为：

- 一个日期范围；
- 选择 1 个具体办事处，或选择全部办事处；
- 一个最低状态。

Trial 不允许创建多个互相独立的时间 × 地点策略组合。

---

## 5. 1 日体验的目的与限制

1 日体验的核心目的不是靠 ¥6 获利，而是帮助第一次接触产品的用户验证：

- 服务真实存在；
- 邮箱配置正确；
- 邮件能够投递；
- 系统会在出现符合目标的新事件时发送提醒。

### 5.1 激活测试邮件

订阅激活后立即发送一封 **激活测试邮件**。测试邮件只验证邮件链路，不代表当前存在预约名额，也不能伪装成真实配额提醒。

这样即使 24 小时内没有真实 quota event，用户仍然能确认邮件服务本身可用。

### 5.2 Trial 仅限一次

1 日体验每个邮箱只允许使用一次。

数据库应通过 `customers.trial_used_at` 或订单历史判断是否已使用 Trial。MVP 不为了防止少量重复试用而收集手机号、设备指纹、HKID 或其他额外个人信息。

---

## 6. 无匹配提醒延长保障

Goal 与 Family 包含一次 **无匹配提醒延长保障**：

> 如果原服务期内，系统没有检测到任何符合该订阅有效预约目标的 quota event，则自动免费延长 7 天。

关键定义：

- 判断依据是系统是否产生 **有效匹配事件**，不是用户是否最终预约成功。
- 每个订单最多自动延长一次。
- 延长不承诺未来一定出现名额。
- 已过期日期、无效日期或明显无法成立的预约目标不应被用于触发保障。
- 保障触发后记录 `guarantee_extended_at`，避免重复延长。

不要使用“未成功延期”“保证抢到”“目标必达”等可能被理解为预约成功承诺的表述。

---

## 7. MVP 技术范围

第一版刻意保持小型：

- 单一集中式公开配额 Poller。
- 配额 observation、validation、confirmed state 与 event engine。
- SQLite 本地存储。
- Email 通知。
- 用户邮箱、订阅期限和预约目标。
- Outbox、失败重试、去重、投递审计。
- 激活测试邮件。
- 一键退订和到期自动停用。
- CLI 管理用户、订阅、预约目标和人工订单。
- 基础健康检查、备份与延迟指标。

第一版明确不做：

- 自动预约或浏览器自动化。
- 身份证明资料保存。
- 在线支付。
- 公开注册页、密码系统和用户中心。
- 微信、WhatsApp 等多通知渠道。
- 免费版每日摘要等第二套调度逻辑。
- Redis、Celery、RabbitMQ、Kubernetes 等与当前规模不匹配的组件。

---

## 8. 核心架构

```text
GovHK / 入境事务处公开配额
              ↓
        单一 Source Adapter
              ↓
         Raw Observation
              ↓
      Snapshot Validation
              ↓
        Confirmed State
              ↓
          Quota Event
              ↓
      Appointment Matcher
              ↓
     Notification Outbox
              ↓
         Email Worker
              ↓
             用户
```

关键原则：

1. 所有用户共享同一份配额采集结果。
2. 用户数量增加不会线性增加对来源网站的请求次数。
3. 网络失败、解析失败和数据异常不能直接改变配额状态。
4. 只有通过 validation 的 snapshot 才允许进入 confirmed-state engine。
5. 配额事件与通知事件分离，便于审计、重试和去重。
6. 所有套餐在 quota event 产生后使用同一通知链路，不设置付费优先队列。
7. 数据库时间统一保存 UTC；面向用户的业务时间使用 `Asia/Hong_Kong`。

---

## 9. Observation 与 Snapshot Validation

以下情况都不能被解释为“没有名额”：

- HTTP timeout；
- 非 2xx 响应；
- 返回空 HTML / 空 JSON；
- 页面结构变化；
- parser 抛错；
- 只返回部分办事处或日期；
- source 更新时间异常倒退。

每次采集先生成 observation：

```text
success
fetch_error
parse_error
rejected
```

失败 observation 只用于审计和健康检查，不进入状态机。

成功快照至少记录：

- `observed_at`
- `source_updated_at`
- `payload_hash`
- `parser_version`
- `office_count`
- `quota_count`

Source adapter 在已知正常覆盖范围时应提供 `expected_keys` 或等价完整性检查。缺少预期 key 的 snapshot 应被拒绝，而不是被解释为配额消失。

---

## 10. Confirmed State 与抗误报

每个 `date + office_id` 维护：

- `unavailable`
- `limited`
- `available`

可产生提醒的变化包括：

- `unavailable -> limited`
- `unavailable -> available`
- `limited -> available`
- 已确认消失后再次出现

下降状态用于维护 confirmed state，但不产生“有名额”提醒。

### 10.1 连续缺失确认

```text
第一次在有效快照中缺失
→ missing_count = 1
→ 保留原 confirmed state

第二次连续在有效快照中缺失
→ confirmed unavailable
→ 当前 occurrence 关闭
```

默认 `MISSING_CONFIRMATIONS_REQUIRED=2`。Fetch / parse / validation failure **不增加** `missing_count`。

### 10.2 occurrence_id

每次从 confirmed `unavailable` 进入 `limited / available` 时生成新的 `occurrence_id`，用于正确处理“消失后再次出现”。

### 10.3 首次启动 baseline

第一份有效 snapshot 只建立当前 confirmed state，不生成历史提醒。只有 baseline 之后真正出现的新名额或状态升级才产生 quota event。

---

## 11. 数据模型

### customers

至少保存：

- `id`
- `email_normalized`
- `created_at`
- `unsubscribed_at`
- `consent_source`
- `trial_used_at`

### subscriptions

至少保存：

- `id`
- `customer_id`
- `plan_code`
- `starts_at`
- `activated_at`
- `original_expires_at`
- `expires_at`
- `active`
- `guarantee_extended_at`
- `first_matched_event_at`
- `first_notification_queued_at`
- `first_provider_accepted_at`
- `created_at`

其中：

- `activated_at` 用于计算用户从激活到首次匹配的等待时间；
- `first_matched_event_at` 反映产品层面的首次有效机会；
- `first_notification_queued_at` 与 `first_provider_accepted_at` 用于拆分通知链路延迟；
- `original_expires_at` 用于判断无匹配保障的原始服务期；
- `guarantee_extended_at` 为空表示尚未使用自动延长。

### subscription_filters / appointment targets

V1 可以在现有 `subscription_filters` 基础上增加 `target_key`，使多行筛选记录属于同一个预约目标。一个 `target_key` 可以对应多个 `office_id`，但共享相同日期范围和最低状态。

### quota_observations / quota_state / quota_events

继续保持 observation、confirmed state 与 event 分离，避免来源异常直接制造通知。

### notification_outbox

通知去重键仍由数据库强制保证：

```text
subscription_id + quota_event_id + channel
```

Worker 使用 expiring lease，崩溃后可恢复任务。

### orders

人工收款阶段保留：

- `id`
- `customer_id`
- `plan_code`
- `amount`
- `currency`（V1 为 `CNY`）
- `external_reference`
- `status`
- `paid_at`

---

## 12. 核心运营指标

### 12.1 用户等待时间

核心指标：

```text
first_matched_event_at - activated_at
```

观察：

- 24 小时内首次匹配比例；
- 3 天内首次匹配比例；
- 7 天内首次匹配比例；
- 14 天内首次匹配比例；
- P50 / P75 / P90 首次匹配等待时间。

这些数据用于判断 V2 是否应该把 14 天主力套餐缩短到 7 天或其他周期，而不是依靠猜测。

### 12.2 通知链路延迟

至少观察：

```text
source_updated_at
→ observed_at
→ event_created_at
→ first_notification_queued_at
→ first_provider_accepted_at
```

分别计算 detect / queue / provider latency 和 P50 / P95 pipeline latency。

用户邮箱客户端何时真正弹出通知可能无法精确测量，因此产品文案不能承诺固定秒数。

---

## 13. 上线与商业验证原则

早期继续采用人工收款 + CLI 开通，不为了少量用户提前开发：

- 在线支付；
- Web 注册；
- 登录密码；
- 用户中心；
- 忘记密码；
- 复杂订单后台。

Family 先作为隐藏/手动套餐验证真实需求。

V1 价格和周期是试运营基准。积累足够真实数据后，根据转化率、首次匹配时间分布、延期触发率和用户反馈调整套餐，不把当前价格写成永久承诺。

---

## 14. 必须测试的场景

### Source / State

- 网络超时、无效响应和 parser error 不改变 quota state。
- 空或明显不完整 snapshot 被拒绝。
- 初始 baseline 不创建历史提醒。
- 一次缺失不会关闭 occurrence。
- 连续确认缺失后才关闭 occurrence。
- 消失后再次出现生成新的 occurrence。
- 程序重启后不重复发送旧事件。

### Subscription / Product

- Trial 每个邮箱最多使用一次。
- Trial 激活测试邮件与真实 quota alert 有明确区分。
- Quick 最多 3 个预约目标。
- Goal 最多 6 个预约目标。
- Family 最多 10 个预约目标、最多 3 个接收邮箱。
- Goal / Family 原服务期内 0 个有效匹配事件时最多自动延长 7 天一次。
- 已存在有效匹配事件时不触发延长。
- 无效或已过期目标不应被用于滥用延长保障。

### Notification

- 同一事件不会向同一订阅重复创建通知。
- 两个 worker 竞争同一 outbox 时只有一个获得有效 lease。
- worker 在 `sending` 崩溃后任务可在 lease 到期后恢复。
- Provider timeout 可以安全重试。
- 邮件失败不会被标记成功。
- 到期和退订立即停止创建新通知。

---

## 15. 合规上线门槛

公开收费前必须完成：

1. 向 GovHK / 入境事务处确认公开配额数据自动读取、第三方提醒及商业收费的允许使用边界。
2. 准备隐私声明、服务条款、退款规则和免责声明。
3. 明确服务只提供提醒，用户仍需自行在官方系统预约。
4. 商品页面不得使用“必达”“保证抢到”“连号”“VIP 更快”等误导性表述。
5. 完成端到端安全测试、数据库备份和故障告警。
6. 用少量获同意用户进行试运营，再决定是否扩大收费。

---

## 16. 里程碑

1. **M0：方案与合规确认**——确认数据使用、第三方提醒和商业用途边界。
2. **M1A：Source Safety**——真实数据源解析、observation、validation 与异常防护。
3. **M1B：Event Core**——confirmed state、occurrence、event 与持久化。
4. **M2：Email Notification**——激活测试邮件、outbox、重试、退订与延迟指标。
5. **M3：Subscription Product**——Trial / Quick / Goal / Family 规则、预约目标、延期保障、CLI 和人工订单。
6. **M4：Pilot**——少量真实用户试运营，收集转化率与首次匹配时间分布。
7. **M5：Expansion Review**——根据真实需求决定 Telegram、在线支付、网页后台和数据库升级。

当前阶段仍以 **正确检测公开配额 + 抗误报 + 可审计** 为最高优先级，不因套餐设计提前扩大技术复杂度。
