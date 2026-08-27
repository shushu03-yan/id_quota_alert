# HKID 公开配额提醒服务方案

## 1. 项目定位

本项目是与私人 `hkid_bot` 完全隔离的多用户提醒服务，只读取 GovHK
公开展示的预约配额变化，并根据顾客预先选择的日期、办事处和配额状态发送通知。

项目坚持以下边界：

- 不自动预约、不代抢、不填写或提交预约表格。
- 不绕过验证码、排队或政府网站的安全机制。
- 不保存 HKID、旅行证件号码、出生日期、签证编号或查询代码。
- 不保证提醒实时、数据绝对准确或预约一定成功。
- 不冒充或暗示与香港政府、入境事务处存在隶属关系。

## 2. MVP 范围

第一版包含：

- 单一集中式公开配额获取任务。
- 邮箱顾客、订阅期限和筛选条件管理。
- 日期范围、办事处、黄色少量/绿色有名额筛选。
- 配额新增、黄色升级绿色、消失后再次出现的事件识别。
- 每位顾客单独发送邮件，不使用群发 To/CC/BCC。
- 发送队列、失败重试、去重和投递日志。
- 一键退订和到期自动停用。
- SQLite 本地存储、CLI 管理和服务健康日志。

第一版不包含：

- 自动预约或浏览器控制。
- 身份证明资料保存。
- 在线支付、公开注册页或顾客密码系统。
- 微信、WhatsApp 等额外通知渠道。
- “2分钟一定更快”等无法由官方数据更新节奏保证的承诺。

## 3. 获取与通知原则

```text
GovHK 公开配额
       ↓
单一 Poller（全平台共享，带超时、退避和抖动）
       ↓
归一化当前状态并计算状态变化
       ↓
生成不可变 quota_event
       ↓
按订阅条件匹配顾客
       ↓
写入 notification_outbox
       ↓
为每位顾客独立投递 Email
```

- 顾客数量增加不会增加对 GovHK 的请求次数。
- 获取频率由公开数据实际更新节奏和服务方许可决定，不作为收费卖点。
- 所有付费顾客在平台识别到符合条件的新事件后立即进入通知队列。
- 免费体验可使用每日摘要；付费差异来自服务期限、筛选数量和通知渠道。
- 请求失败时采用指数退避，避免连续冲击来源网站。
- 官方返回的更新时间和本地观测时间分别保存。

## 4. 事件与去重

每个 `date + office_id` 维护当前状态：

- `unavailable`
- `limited`
- `available`

产生通知事件的变化包括：

- `unavailable -> limited`
- `unavailable -> available`
- `limited -> available`
- 配额消失后再次出现

配额消失会关闭当前 occurrence；再次出现时生成新的 `occurrence_id`。通知去重键为：

```text
subscription_id + quota_event_id + channel
```

不能仅用日期、办事处和状态去重，否则无法正确处理“消失后再次出现”。

## 5. 数据模型

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

### quota_state

- `date`
- `office_id`
- `status`
- `service_periods`
- `occurrence_id`
- `first_observed_at`
- `last_observed_at`
- `source_updated_at`

### quota_events

- `id`
- `date`
- `office_id`
- `from_status`
- `to_status`
- `occurrence_id`
- `observed_at`
- `source_updated_at`

### notification_outbox

- `id`
- `subscription_id`
- `quota_event_id`
- `channel`
- `status` (`pending/sending/sent/failed/cancelled`)
- `attempt_count`
- `next_attempt_at`
- `created_at`

### delivery_attempts

- `outbox_id`
- `attempted_at`
- `provider_message_id`
- `result`
- `error_code`

### orders（人工收款阶段也保留）

- `id`
- `customer_id`
- `plan_code`
- `amount`
- `currency`
- `external_reference`
- `status`
- `paid_at`

## 6. 项目结构

```text
id_quota_alert/
├── app/
│   ├── __init__.py
│   ├── __main__.py
│   ├── config.py
│   ├── quota.py
│   ├── events.py
│   ├── matcher.py
│   ├── notifier.py
│   ├── storage.py
│   ├── scheduler.py
│   └── cli.py
├── tests/
├── docs/
│   └── COMPLIANCE_CHECKLIST.md
├── .env.example
├── .gitignore
├── pyproject.toml
├── README.md
└── QUOTA_ALERT_PLAN.md
```

## 7. 套餐建议

不按官网轮询频率收费。

| 套餐 | 建议功能 |
|---|---|
| 3天体验 | 1组筛选、每日摘要 |
| 14天基础版 | 1组筛选、事件匹配后邮件提醒 |
| 30天标准版 | 最多3组筛选、邮件提醒、提醒历史 |
| 家庭版 | 多位独立收件人、多个筛选条件 |

Telegram 等渠道应在邮件 MVP 稳定后再添加。价格在真实顾客访谈和试运营后决定，
不在代码中写死。

## 8. 邮件要求

- 每位顾客单独生成和发送邮件。
- 邮件标题不得误导，正文明确数据来源与非官方身份。
- 包含官方配额页、官方预约入口和数据更新时间。
- 包含发送方联络方式和唯一退订链接。
- 退订后立即停止创建新通知，并保留必要审计记录。
- SMTP/API 密钥只从环境变量读取。
- 发送成功后才把 outbox 标记为 `sent`。

## 9. 运行与可靠性

- 所有业务时间使用 `Asia/Hong_Kong`，数据库时间使用 UTC。
- 同一时刻只允许一个 Poller，通过数据库租约或进程锁保证。
- 网络错误使用指数退避并设置最大重试间隔。
- 数据库定期备份并验证可恢复性。
- 健康检查记录最后成功获取、最后来源更新时间和最近投递结果。
- 日志不得包含完整邮箱、密钥或任何证件信息。

SQLite 适用于本地小规模 MVP。迁移到多实例/VPS 前，需要重新评估数据库并发、
任务锁、备份、邮件服务限额和监控，不承诺“无痛迁移”。

## 10. 必须测试的场景

- 红/无名额不产生可用事件。
- 无名额到黄色、无名额到绿色、黄色到绿色。
- 名额消失后再次出现会创建新事件。
- 同一事件不会向同一订阅重复发送。
- 邮件失败不会标记为成功，重启后可以安全重试。
- 订阅到期和退订边界。
- 两个进程同时启动时只有一个 Poller 工作。
- 官网超时、无效 JSON、未知状态和更新时间倒退。
- 程序重启后不重复发送旧事件。

## 11. 合规上线门槛

公开收费前必须完成：

1. 向 GovHK/入境事务处说明商业模式并确认公开配额数据的允许使用方式。
2. 准备隐私声明、服务条款、退款规则和免责声明。
3. 每封商业邮件提供准确发送者资料和退订方式。
4. 商品页面不得使用政府标志或让顾客误认为官方服务。
5. 明确不保证数据实时、提醒必达或预约成功。
6. 完成端到端安全测试、数据库备份和故障告警。

## 12. 里程碑

1. **M0：方案与合规确认**——确认数据使用边界和商业表述。
2. **M1：事件核心**——公开配额解析、状态机、SQLite 和单元测试。
3. **M2：单用户投递**——邮件 outbox、重试和独立退订。
4. **M3：多用户订阅**——筛选、期限、CLI 和人工订单记录。
5. **M4：试运营**——少量获同意用户、监控、备份和反馈。
6. **M5：扩展评估**——根据真实需求决定 Telegram、网页后台和在线支付。

当前仓库首先建立安全骨架；在完成 M0 前，不发布收费服务。
