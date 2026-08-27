# HKID 公开配额提醒服务方案

## 1. 项目定位

本项目提供 **HKID 公开预约配额变化提醒**。服务读取获准使用的 GovHK / 入境事务处公开配额信息，在平台识别到符合用户筛选条件的新事件后发送通知；用户仍需自行进入官方系统预约。

项目边界：

- 不自动预约、不代抢、不提交预约表格。
- 不控制官方预约页面。
- 不绕过验证码、排队、限流或其他安全机制。
- 不保存 HKID、旅行证件号码、出生日期、签证编号或查询代码。
- 不承诺提醒绝对实时、邮件必达、数据绝对准确或预约一定成功。
- 不冒充或暗示与香港政府、入境事务处存在隶属关系。
- 不以更高官网轮询频率作为收费卖点。

产品价值是“帮助普通用户及时知道公开名额变化”，而不是成为另一种抢号脚本。

---

## 2. MVP 原则

第一版刻意保持小型：

- 单一集中式公开配额 Poller。
- 配额 observation、validation、confirmed state 与 event engine。
- SQLite 本地存储。
- Email 通知。
- 用户邮箱、订阅期限和筛选条件。
- 日期范围、办事处、最低状态筛选。
- Outbox、失败重试、去重、投递审计。
- 一键退订和到期自动停用。
- CLI 管理用户、订阅和人工订单。
- 基础健康检查、备份与延迟指标。

第一版明确不做：

- 自动预约或浏览器自动化。
- 身份证明资料保存。
- 在线支付。
- 公开注册页、密码系统和用户中心。
- 微信、WhatsApp 等多通知渠道。
- 免费版每日摘要等第二套调度逻辑。
- Redis、Celery、RabbitMQ、Kubernetes 等与当前规模不匹配的组件。

试用用户与付费用户使用同一条事件通知链路，只通过服务期限和筛选数量区分，避免为了“免费版”额外维护一套摘要系统。

---

## 3. 核心架构

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
      Subscription Matcher
              ↓
     Notification Outbox
              ↓
         Email Worker
```

关键原则：

1. 所有用户共享同一份配额采集结果。
2. 用户数量增加不会线性增加对来源网站的请求次数。
3. 网络失败、解析失败和数据异常不能直接改变配额状态。
4. 只有通过 validation 的 snapshot 才允许进入 confirmed-state engine。
5. 配额事件与通知事件分离，便于审计、重试和去重。
6. 数据库时间统一保存 UTC；面向用户的业务时间使用 `Asia/Hong_Kong`。

---

## 4. Observation 与 Snapshot Validation

### 4.1 为什么需要 Observation 层

以下情况都不能被解释为“没有名额”：

- HTTP timeout。
- 非 2xx 响应。
- 返回空 HTML / 空 JSON。
- 页面结构变化。
- parser 抛错。
- 只返回部分办事处或日期。
- source 更新时间异常倒退。

因此每次采集先生成 observation：

```text
success
fetch_error
parse_error
rejected
```

失败 observation 只用于审计和健康检查，不进入状态机。

### 4.2 成功快照至少记录

- `observed_at`
- `source_updated_at`
- `payload_hash`
- `parser_version`
- `office_count`
- `quota_count`

Source adapter 在已知正常覆盖范围时应提供 `expected_keys` 或等价完整性检查；缺少预期 key 的 snapshot 应被拒绝，而不是被解释为配额消失。

不要求 MVP 保存完整网页正文；优先保存最小审计元数据，降低数据和隐私风险。

---

## 5. Confirmed State 与抗误报

每个 `date + office_id` 维护状态：

- `unavailable`
- `limited`
- `available`

通知事件包括：

- `unavailable -> limited`
- `unavailable -> available`
- `limited -> available`
- 已确认消失后再次出现

下降状态用于维护 confirmed state，但不产生“有名额”提醒。

### 5.1 连续缺失确认

一次成功 snapshot 中没有看到某个此前活跃的 key，不立刻关闭 occurrence。

默认规则：

```text
第一次在有效快照中缺失
→ missing_count = 1
→ 仍保留原 confirmed state

第二次连续在有效快照中缺失
→ confirmed unavailable
→ 当前 occurrence 关闭
```

默认 `MISSING_CONFIRMATIONS_REQUIRED=2`，后续根据真实来源更新行为调整。

Fetch / parse / validation failure **不增加** `missing_count`。

### 5.2 occurrence_id

每次从 confirmed `unavailable` 进入 `limited/available` 时生成新的 `occurrence_id`。

这样可以区分：

```text
available
→ confirmed unavailable
→ available
```

第二次出现必须是新的 occurrence，而不是被旧通知去重掉。

### 5.3 首次启动 baseline

服务第一次获得有效 snapshot 时：

- 建立 confirmed state。
- 记录已有 active occurrence。
- 不生成历史提醒事件。

只有 baseline 之后真正出现的新名额或状态升级才产生 quota event，避免部署或重启后把当前所有名额当成“刚出现”。

生产实现需要把 baseline 已完成状态持久化到 `runtime_state`，不能只存在进程内存里。

---

## 6. 数据模型

### runtime_state

保存少量服务级状态，例如：

- baseline 是否完成
- 最后成功 observation 时间
- schema / parser 版本辅助信息

### quota_observations

- `id`
- `observed_at`
- `outcome`
- `source_updated_at`
- `payload_hash`
- `parser_version`
- `office_count`
- `quota_count`
- `error_code`

### quota_state

- `quota_date`
- `office_id`
- `status`
- `service_periods_json`
- `occurrence_id`
- `first_observed_at`
- `last_observed_at`
- `source_updated_at`
- `missing_count`

主键：

```text
quota_date + office_id
```

### quota_events

- `id`
- `quota_date`
- `office_id`
- `from_status`
- `to_status`
- `occurrence_id`
- `observed_at`
- `source_updated_at`
- `created_at`

数据库层避免同一 occurrence 的同一升级事件被重复插入。

### customers

- `id`
- `email_normalized`
- `created_at`
- `unsubscribed_at`
- `consent_source`

### subscriptions

- `id`
- `customer_id`
- `plan_code`
- `starts_at`
- `expires_at`
- `active`
- `created_at`

### subscription_filters

- `subscription_id`
- `earliest_date`
- `deadline`
- `office_id`
- `minimum_status`

### notification_outbox

- `id`
- `subscription_id`
- `quota_event_id`
- `channel`
- `status` (`pending/sending/sent/failed/cancelled`)
- `attempt_count`
- `next_attempt_at`
- `locked_at`
- `locked_by`
- `lock_expires_at`
- `provider_message_id`
- `created_at`
- `sent_at`

数据库唯一键：

```text
subscription_id + quota_event_id + channel
```

必须由数据库 `UNIQUE` 强制执行，不能只依赖 Python 的 `if not exists`。

### delivery_attempts

- `outbox_id`
- `attempted_at`
- `provider_message_id`
- `result`
- `error_code`

### orders

人工收款阶段也保留最小订单记录：

- `id`
- `customer_id`
- `plan_code`
- `amount`
- `currency`
- `external_reference`
- `status`
- `paid_at`

---

## 7. 通知可靠性

### 7.1 Outbox lease

Worker 把任务从 `pending` 取出后进入 `sending`，同时写入：

```text
locked_at
locked_by
lock_expires_at
```

如果 worker 崩溃，lease 到期后任务可以重新进入投递流程，避免永久卡在 `sending`。

### 7.2 不承诺 exactly-once Email

邮件系统存在经典不确定性：Provider 可能已经接受邮件，但客户端在收到确认前 timeout。

因此目标是：

**at-least-once delivery + best-effort deduplication**

如果未来 Email Provider 支持 idempotency key，优先使用 `notification_outbox.id` 作为稳定幂等键。

只有确认 Provider 接受成功后才把 outbox 标为 `sent`。

### 7.3 每位用户独立投递

- 不使用共享 To/CC/BCC 群发。
- 邮件包含来源、官方预约入口、观测时间和非官方身份说明。
- 每封邮件包含唯一退订入口。
- 退订后立即停止创建新 notification。

---

## 8. 延迟指标

真正重要的不是单独的 poll interval，而是端到端提醒延迟：

```text
source_updated_at
→ observed_at
→ event_created_at
→ outbox_created_at
→ provider_accepted_at
```

至少记录并观察：

- Detect latency：来源变化到平台发现。
- Queue latency：事件到进入通知 worker。
- Provider latency：发送请求到 Provider 接受。
- P50 / P95 notification pipeline latency。

用户邮箱客户端何时真正弹出通知可能无法精确测量，因此产品文案不能承诺固定秒数。

只有真实试运营证明 Email 延迟不足时，才优先评估 Telegram 等第二通知渠道。

---

## 9. 套餐与商业验证

MVP 不按官网轮询频率收费。

建议先用非常简单的期限型方案验证需求：

| 类型 | 建议 |
|---|---|
| 体验 | 24–48 小时，完整事件提醒，少量筛选 |
| 基础 | 14 天，1 组筛选 |
| 标准 | 30 天，最多 3 组筛选 |
| 家庭 | 多位独立收件人和多个筛选条件，确认有真实需求后再实现 |

价格通过少量真实顾客访谈和试运营决定，不写死在代码里。

早期人工收款 + CLI 开通即可，不为了十几位用户提前开发：

- 在线支付
- Web 注册
- 登录密码
- 用户中心
- 忘记密码
- 复杂订单后台

这些功能只有在真实运营负担证明值得自动化时再做。

---

## 10. SQLite 运行策略

SQLite 足够支撑小规模单实例 MVP。

要求：

- `foreign_keys = ON`
- WAL mode
- `busy_timeout`
- 显式 transaction
- schema version / migration 管理
- 定期数据库备份
- 定期执行恢复测试

在迁移到多实例或 VPS 水平扩展前，再重新评估 PostgreSQL、任务锁、邮件限额和监控。

当前不提前引入 Redis / Celery / RabbitMQ。

---

## 11. 必须测试的场景

### Snapshot / Observation

- 网络超时不会改变 quota state。
- 无效 JSON / HTML 不会改变 quota state。
- 空或明显不完整 snapshot 被拒绝。
- 重复 key 被拒绝。
- 未知状态被拒绝或安全失败。
- parser version 被记录。
- source 更新时间倒退时按 source adapter 规则拒绝或告警。

### State / Event

- 初始 baseline 不创建历史提醒事件。
- `unavailable -> limited`。
- `unavailable -> available`。
- `limited -> available`。
- `available -> limited` 不创建“有名额”事件。
- 一次缺失不会关闭 occurrence。
- 连续确认缺失后才关闭 occurrence。
- 消失后再次出现生成新的 occurrence。
- 程序重启后不重复发送旧事件。

### Notification

- 同一事件不会向同一订阅重复创建通知。
- 两个 worker 竞争同一 outbox 时只有一个获得有效 lease。
- worker 在 `sending` 崩溃后任务可在 lease 到期后恢复。
- Provider timeout 可以安全重试。
- 邮件失败不会被标记成功。
- 到期和退订立即停止创建新通知。

### Runtime

- 两个 Poller 同时启动时只有一个有效采集者。
- 数据库备份可恢复。
- 日志不包含完整邮箱、密钥或证件资料。

---

## 12. 合规上线门槛

公开收费前必须完成：

1. 向 GovHK / 入境事务处说明实际商业模式，分别确认：
   - 是否允许程序周期性读取相关公开 quota 数据；
   - 是否允许基于该数据提供第三方提醒服务；
   - 是否允许该提醒服务收费。
2. 根据得到的许可设置明确的请求频率上限、timeout、退避和 jitter。
3. 准备隐私声明、服务条款、退款规则和免责声明。
4. 页面和邮件不得使用政府标志或使用户误认为官方服务。
5. 明确不保证数据实时、通知必达或预约成功。
6. 完成端到端安全测试、备份恢复和故障告警。
7. 决定代码分发策略；在商业模式明确前不随意添加宽松开源许可证。

详细清单见 `docs/COMPLIANCE_CHECKLIST.md`。

---

## 13. 开发里程碑

### M0：数据与商业边界确认

目标：先确认可以怎么合法、克制地使用公开数据。

完成条件：

- 数据自动读取边界明确。
- 商业提醒边界明确。
- 请求频率和技术限制有依据。
- 产品文案不暗示官方身份或预约成功保证。

### M1A：Source Safety

目标：证明“拿到的数据值得信任”。

开发：

- Source adapter。
- Fetch timeout / retry / backoff / jitter。
- Parser。
- Observation audit。
- Snapshot validation。
- payload hash / parser version。
- 数据完整性测试。

完成条件：异常页面、超时和部分响应不会制造假 quota state。

### M1B：Event Core

目标：证明“状态变化不会误报和重复报”。

开发：

- Confirmed state。
- Missing confirmation。
- occurrence_id。
- Baseline persistence。
- quota_events。
- SQLite persistence。
- 单元测试与重启恢复测试。

当前仓库已经开始实现这一层的纯领域模型和初始 SQLite schema。

### M2：单用户 Email 投递

开发：

- Notification outbox。
- Lease / retry。
- Email provider adapter。
- 一键退订。
- delivery_attempts。
- latency metrics。

目标：连续运行并证明通知链路稳定。

### M3：多用户订阅

开发：

- customers。
- subscriptions。
- filters。
- matcher。
- CLI。
- 人工订单记录。

不开发在线支付和用户后台。

### M4：5–20 位真实用户试运营

重点收集：

- 误报率。
- 漏报反馈。
- P50 / P95 检测及投递延迟。
- Email 实际体验。
- 用户真正使用的筛选条件。
- 愿意支付的价格与服务期限。
- 支持工作量和退款原因。

### M5：按真实需求扩展

只有数据支持时再决定：

- Telegram。
- Web UI。
- 在线支付。
- PostgreSQL。
- 更自动化的运营后台。

---

## 14. 当前开发优先级

当前最重要的不是前端、支付或获客页面，而是把 quota engine 做到：

- 连续运行稳定。
- 网络异常不制造假名额。
- 页面结构变化能快速发现。
- 状态变化可审计。
- 消失/重现正确识别。
- 重启不重复通知。

只有这个核心跑稳，才进入 Email、多用户和收费试运营。
