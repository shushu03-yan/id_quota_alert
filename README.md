# ID Quota Alert

一个面向普通用户的 **HKID 公开预约配额变化提醒服务**。

> 本项目不是香港政府或入境事务处的官方服务，不提供自动预约、代抢、验证码绕过或预约成功保证。用户收到提醒后仍需自行前往官方预约系统完成预约。

## 当前状态

项目当前处于 **product skeleton complete / developer test ready**：M1 可靠事件核心、M2 通知链路、M3 订阅生命周期，以及面向普通用户的激活/管理 Web 框架已经接通并有自动测试。真实 3–7 天 soak、真实 Email Provider 端到端投递、重启与备份恢复演练、HTTPS/VPS 和最终政策文本仍为 **NOT YET VALIDATED**，因此尚不能称为 paid production ready。

当前开发策略是先完成完整产品闭环，再让真实 soak 与运行验证并行推进，而不是继续对单个模块做无限局部优化。

目前已经实现：

- 配额领域模型：`unavailable / limited / available`。
- `ValidatedSnapshot`：只有通过验证的完整快照才允许驱动状态变化。
- `quota_observations` 审计模型：获取失败、解析失败与配额状态严格分离。
- GovHK / 入境事务处公开配额 `getSituation` Source Adapter（默认 `svcId=579`）。
- Source Adapter 对 timeout、403、429、5xx、空响应、非法 JSON、未知状态值进行失败分类；完整但较旧的 source 快照记录为 `stale`，不进入状态机。
- `office[] × date` 一致性检查：对响应中出现的日期，缺少预期办事处数据时拒绝快照，不把部分响应误判为名额消失。
- `quotaR / quotaK` 解析与聚合：`quota-g -> available`、`quota-y -> limited`、`quota-r / no-quota* -> unavailable`，并保留当前有名额的 `R / K` 时段标签。
- Confirmed State 状态机。
- 连续缺失确认机制：单次缺失不会直接关闭现有名额 occurrence。
- `occurrence_id`：支持“消失后再次出现”生成新事件。
- 初始基线模式：服务首次成功快照只记录当前状态，不发送历史提醒。
- 单一共享 `QuotaPoller`：所有用户共用一份来源数据，不按用户增加来源请求频率。
- Poller 将 observation、confirmed state、quota event 持久化到 SQLite，并保留 `R / K` service periods。
- 成功快照通过 payload hash 去重；重复 payload 仍记录成功 observation / health 时间，但不重复跑状态机。
- Poller 重启后从 SQLite 恢复 baseline、confirmed state 与 source 更新时间。
- 获取、解析或结构失败采用有上限的指数退避；`stale` 响应保持正常轮询间隔，jitter 只增加等待时间。
- source 时间倒退仍严格禁止更新 confirmed state；同一 source 时间对应不同 payload 时记录 `source_version_conflict` 并拒绝应用。
- `runtime_state` 记录 `last_poll_attempt`、`last_well_formed_poll`、`last_successful_poll`、`last_valid_snapshot`、`last_received_source_updated_at`、`last_source_updated_at`、最近回退秒数等运行信息。
- SQLite schema、Source Adapter、状态机、Poller 重启/失败隔离/重复快照等自动测试。
- GitHub Actions 在 Python 3.11 / 3.12 运行 pytest。
- `health` 与 `soak-summary` 运维摘要；不足 3 天永远显示 `SOAK TEST NOT COMPLETE`，满 3 天也只提示人工复核，不自动宣称通过。
- 通用 notification outbox（`verify_email / activation_test / quota_alert / manage_link`）、数据库去重键、lease recovery、1/5/15/60 分钟有界重试及 delivery audit。
- Appointment Matcher：按有效期、办事处、日期、最低状态和退订状态匹配；一个 target 可包含多个 office。
- 腾讯云 SES `SendEmail` API Provider、模板消息 Email Worker，以及首次匹配/入队/Provider 接受指标。
- 随机一次性激活码（仅存 hash）、Email 验证（仅存 token hash）、激活确认邮件、Trial 每邮箱一次、套餐目标/邮箱上限和 Goal/Family 0-match 一次性延期。
- 短期一次性 Magic Link、目标查看/修改和退订。
- 用户首页 `/`、普通表单 `/activate`、Magic Link 自助申请 `/manage/request`、目标管理 `/manage`、退订 `/unsubscribe`。
- 激活/管理页使用日期、办事处和提醒门槛的普通表单，不再要求普通用户填写 JSON。
- `/privacy`、`/terms`、`/refund` 试运行草案页面；正式公开收费前仍需最终确认。
- `maintenance` one-shot 命令 + systemd timer，用于在 runtime 中实际执行 Goal / Family 的 0-match 延长判断。
- `customer list`、`subscription list/show`、`outbox status` 轻量运营 CLI，避免过早建设 Admin Dashboard。
- `email-smoke --to ...` 使用激活确认模板执行真实腾讯云 SES API smoke test；测试邮件不是配额提醒。
- SQLite 官方 backup API 一致性备份与保留轮换，以及非 root systemd 模板。

目前 **尚未完成 / 尚未真实验证**：

- 3–7 天真实连续运行 soak test；因此还不能据此宣称 Poller 已达到生产稳定性。
- 四个事务邮件模板审核及腾讯云 SES API 真实投递验证。
- activation → verify → activation email → quota alert 的真实人工端到端验证。
- 备份 restore 演练、进程重启演练、VPS/HTTPS/外部监控与 offsite backup。
- Family 完整自助多邮箱邀请/验证流程（V1 仍计划隐藏、人工处理）。
- 在线支付；V1 计划继续使用外部人工收款 + Activation Code，不把支付开发作为早期上线阻塞项。
- 公开收费前的数据使用、第三方提醒与商业使用边界确认。
- Privacy / Terms / Refund 最终文本与运营联系方式。

因此当前仓库已经具备完整产品框架，但仍不可直接作为未经验证的生产收费服务运行。

## 产品定位

这个项目是 **任务型提醒服务**，而不是长期 SaaS 订阅。

用户购买的不是“一个月的软件”，而是希望在近期完成一次 HKID 预约任务。产品只负责：

```text
公开名额发生变化
        ↓
系统识别并验证
        ↓
匹配用户预约目标
        ↓
发送提醒
        ↓
用户自行进入官方系统预约
```

所有套餐共享同一份配额采集结果和同一条通知链路，不设置“VIP 更快”“更高轮询频率”或付费优先队列。

## V1 套餐设计

V1 试运营阶段使用人民币（CNY / RMB）定价：

| 套餐 | 价格 | 周期 | 预约目标 | 邮箱 | 延长保障 |
|---|---:|---:|---:|---:|---:|
| 🌱 Trial | **¥6** | 24 小时 | 1 个简单目标 | 1 | 无 |
| ⚡ Quick | **¥18** | 3 天 | 最多 3 个 | 1 | 无 |
| 🎯 Goal | **¥59** | 14 天 | 最多 6 个 | 1 | 原服务期 0 个有效匹配事件时自动 +7 天一次 |
| 👨‍👩‍👧 Family | **¥99** | 14 天 | 最多 10 个 | 最多 3 | 原服务期 0 个有效匹配事件时自动 +7 天一次 |

Family 在 V1 先作为 **隐藏/手动开通套餐**，不在产品页公开主推。只有出现真实多人需求时通过 CLI 手动配置，积累足够需求后再决定是否正式上架。

### 预约目标

用户侧使用“预约目标”而不是“规则组”。一个预约目标包含：

```text
日期范围
+ 可接受的办事处集合
+ 最低提醒状态
```

Trial 只允许一个简单目标：一个日期范围 + 1 个具体办事处或全部办事处 + 一个最低状态。

### Trial 只用于验证真实性和邮件链路

1 日体验每个邮箱只允许使用一次。订阅激活后应立即发送一封 **激活测试邮件**，用于确认邮件配置和投递链路正常。

测试邮件必须明确：

- 这不是实际配额提醒；
- 不代表当前存在预约名额；
- 真实提醒只会在后续出现符合预约目标的新 quota event 时发送。

这样即使 24 小时内没有真实放号，用户仍然可以确认服务本身是真实可用的。

### 无匹配提醒延长保障

Goal / Family 的保障定义是：

> 如果原服务期内系统没有检测到任何符合该订阅有效预约目标的 quota event，则自动免费延长 7 天，最多一次。

它不是“未预约成功就延期”，也不意味着“保证抢到”。

## 核心原则

### 1. 只提醒，不代抢

- 不自动预约。
- 不控制官方预约页面。
- 不绕过验证码、排队或其他安全机制。
- 不收集 HKID、证件号码、出生日期、签证编号或查询代码。

### 2. 一个 Poller 服务所有用户

```text
GovHK 公开配额
       ↓
单一 Poller
       ↓
Raw Observation
       ↓
Snapshot Validation
       ↓
Confirmed State
       ↓
Quota Event
       ↓
Appointment Matching
       ↓
Notification Outbox
       ↓
独立 Email
```

用户数量增加不应线性增加对 GovHK 的请求次数。

### 3. 网络失败不等于“没有名额”

超时、无效内容、解析失败、页面结构异常都只能记录为 observation failure，不能把现有状态直接改成 `unavailable`。

### 4. 首次启动不发送历史名额

第一份成功快照作为 baseline，只建立当前 confirmed state。只有之后真正发生的新出现或状态升级才创建提醒事件。

## 本地开发与 Poller

```powershell
Copy-Item .env.example .env
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m pytest
```

默认运行不会发送网络请求：

```powershell
python -m app
```

显式执行一次真实公开配额 observation：

```powershell
python -m app poll --once
```

连续运行共享 Poller：

```powershell
python -m app poll
```

检查运行状态和真实 soak 窗口：

```powershell
python -m app health
python -m app soak-summary
```

Launch Skeleton 的主要本地命令：

```powershell
# 腾讯云 SES API Provider smoke（使用激活确认模板，不是配额提醒）
python -m app email-smoke --to your-test@example.com

# Email Worker
python -m app email-worker

# 激活码
python -m app activation-code create --plan goal
python -m app activation-code list

# Web
python -m app web --host 127.0.0.1 --port 8080

# 运营视图
python -m app customer list
python -m app subscription list
python -m app subscription show 1
python -m app outbox status

# 延长保障 runtime maintenance
python -m app maintenance

# 一致性备份
python -m app backup --retain 30
```

腾讯云 API 密钥、模板 ID、`TOKEN_SIGNING_SECRET` 和公开 URL 只从进程环境读取。`.env.example` 仅含占位值；服务不会把 API 密钥、激活码明文、验证 token 或 Magic Link token 写入数据库或普通日志。Email Worker 只提交审核模板 ID 与简单字符串变量；验证和管理邮件只提交签名 token，模板必须固定站点域名与路径。

可直接上传腾讯云审核的四套 HTML / 纯文本模板位于 [`templates/tencent_ses/`](templates/tencent_ses/)，变量、固定链接和建议模板名称见该目录的 `README.md`。

连续运行前请阅读 [`docs/LOCAL_SOAK_TEST.md`](docs/LOCAL_SOAK_TEST.md)。部署框架见 [`docs/DEPLOYMENT_PREP.md`](docs/DEPLOYMENT_PREP.md)。默认 Poller 配置只是当前工程测试基准，不代表已经获得来源方对某一具体轮询频率、第三方提醒或商业用途的许可。

Python 要求：**3.11+**。

## 当前代码结构

```text
id_quota_alert/
├── app/
│   ├── __init__.py
│   ├── __main__.py
│   ├── admin.py
│   ├── config.py
│   ├── source.py
│   ├── quota.py
│   ├── observations.py
│   ├── events.py
│   ├── poller.py
│   ├── reporting.py
│   ├── notifications.py
│   ├── email_provider.py
│   ├── lifecycle.py
│   ├── web.py
│   ├── backup.py
│   └── storage.py
├── tests/
│   ├── test_launch_skeleton.py
│   ├── test_project_skeleton.py
│   ├── test_quota_core.py
│   ├── test_source_adapter.py
│   ├── test_poller.py
│   └── test_storage_schema.py
├── docs/
│   ├── COMPLIANCE_CHECKLIST.md
│   ├── DEPLOYMENT_PREP.md
│   └── LOCAL_SOAK_TEST.md
├── deploy/systemd/
│   ├── hkid-poller.service
│   ├── hkid-email-worker.service
│   ├── hkid-web.service
│   ├── hkid-maintenance.service
│   └── hkid-maintenance.timer
├── .env.example
├── pyproject.toml
└── QUOTA_ALERT_PLAN.md
```

部署模板不包含密钥，也不代表已经完成 VPS、HTTPS、监控或生产验证。

## 数据来源

计划只使用获准的 GovHK / 入境事务处公开配额信息：

- [GovHK 人事登记办事处预约配额预览](https://www.gov.hk/tc/apps/bookidcardquota.htm)
- [入境事务处公开配额预览](https://eservices.es2.immd.gov.hk/es/quota-enquiry-client/?l=zh-CN&appId=579)
- Source Adapter 当前读取该预览所使用的公开 `getSituation` JSON 数据（`svcId=579`）。
- [网上预约申领香港智能身份证](https://www.immd.gov.hk/hkt/hkid.html)

实际预约情况始终以官方系统为准。Source Adapter / Poller 的实现不代表已经完成上线前的数据使用、自动读取频率或商业用途确认。

## V1 要采集的产品指标

为了以后决定 14 天是否应该缩短为 7 天或其他周期，订阅至少记录：

- `activated_at`
- `first_matched_event_at`
- `first_notification_queued_at`
- `first_provider_accepted_at`

重点观察：

- 24 小时 / 3 天 / 7 天 / 14 天内首次匹配比例；
- P50 / P75 / P90 首次匹配等待时间；
- Detect / Queue / Provider latency；
- Goal / Family 延长保障触发率。

## 上线前要求

公开收费前必须完成：

1. 确认公开配额数据的自动读取、第三方提醒及商业使用边界。
2. 对真实 Source Adapter + Poller 完成 3–7 天连续运行验证，确认结构变化、网络异常和来源异常不会制造假事件。
3. 对真实 Email Provider 完成 activation → verify → activation email → quota alert 人工 E2E。
4. 完成重启恢复和 backup restore 演练。
5. 配置域名、HTTPS、基础监控与 offsite backup。
6. 最终确认隐私声明、服务条款、退款规则和免责声明。
7. 使用少量获同意用户进行 private pilot，再根据真实数据调整周期与价格。

完整路线见 [QUOTA_ALERT_PLAN.md](QUOTA_ALERT_PLAN.md)。
