# ID Quota Alert

一个面向普通用户的 **HKID 公开预约配额变化提醒服务**。

> 本项目不是香港政府或入境事务处的官方服务，不提供自动预约、代抢、验证码绕过或预约成功保证。用户收到提醒后仍需自行前往官方预约系统完成预约。

## 当前状态

项目当前处于 **developer test ready**：M1 可靠事件核心以及 M2/M3 的本地工程实现已经具备自动测试，但真实 3–7 天 soak、真实 Email Provider、完整人工链路、重启和备份恢复演练均为 **NOT YET VALIDATED**。因此尚不属于 local pilot ready，更不属于 paid production ready。

目前已经实现：

- 配额领域模型：`unavailable / limited / available`。
- `ValidatedSnapshot`：只有通过验证的完整快照才允许驱动状态变化。
- `quota_observations` 审计模型：获取失败、解析失败与配额状态严格分离。
- GovHK / 入境事务处公开配额 `getSituation` Source Adapter（默认 `svcId=579`）。
- Source Adapter 对 timeout、403、429、5xx、空响应、非法 JSON、未知状态值和 source 更新时间倒退进行失败分类。
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
- 连续失败采用有上限的指数退避；jitter 只增加等待时间，不制造更高请求频率。
- `runtime_state` 记录 `last_poll_attempt`、`last_poll_outcome`、`last_successful_poll`、`last_valid_snapshot`、`last_payload_hash`、`last_source_updated_at` 等运行信息。
- SQLite schema、Source Adapter、状态机、Poller 重启/失败隔离/重复快照等自动测试。
- GitHub Actions 在 Python 3.11 / 3.12 运行 pytest。
- `health` 与 `soak-summary` 运维摘要；不足 3 天永远显示 `SOAK TEST NOT COMPLETE`，满 3 天也只提示人工复核，不自动宣称通过。
- 通用 notification outbox（`verify_email / activation_test / quota_alert / manage_link`）、数据库去重键、lease recovery、1/5/15/60 分钟有界重试及 delivery audit。
- Appointment Matcher：按有效期、办事处、日期、最低状态和退订状态匹配；一个 target 可包含多个 office。
- SMTP 开发 Provider 抽象、Email Worker，以及首次匹配/入队/Provider 接受指标。
- 随机一次性激活码（仅存 hash）、Email 验证（仅存 token hash）、激活确认邮件、Trial 每邮箱一次、套餐目标/邮箱上限和 Goal/Family 0-match 一次性延期。
- 短期一次性 Magic Link、目标查看/修改和退订 Web MVP。
- SQLite 官方 backup API 一致性备份与保留轮换，以及非 root systemd 模板。

目前 **尚未完成**：

- 3–7 天真实连续运行 soak test；因此还不能据此宣称 Poller 已达到生产稳定性。
- 真实 SMTP / 正式 Email Provider 投递验证。
- activation → verify → activation email → quota alert 的真实人工端到端验证。
- 备份 restore 演练、进程重启演练、VPS/HTTPS/外部监控与 offsite backup。
- 公开收费、注册、支付或用户后台。

因此当前仓库仍不可直接作为生产收费服务运行。

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

开发环境 Email Worker、激活码、Web 与一致性备份：

```powershell
python -m app activation-code create --plan goal
python -m app activation-code list
python -m app email-worker
python -m app web --host 127.0.0.1 --port 8080
python -m app backup --retain 30
```

SMTP 密钥、`TOKEN_SIGNING_SECRET` 和公开 URL 只从进程环境读取。`.env.example` 仅含占位值；服务不会把 SMTP 密码、激活码明文、验证 token 或 Magic Link token 写入数据库或普通日志。

连续运行前请先阅读 [`docs/LOCAL_SOAK_TEST.md`](docs/LOCAL_SOAK_TEST.md)。默认 Poller 配置只是当前工程测试基准，不代表已经获得来源方对某一具体轮询频率、第三方提醒或商业用途的许可。

Python 要求：**3.11+**。

## 当前代码结构

```text
id_quota_alert/
├── app/
│   ├── __init__.py
│   ├── __main__.py
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
│   ├── test_project_skeleton.py
│   ├── test_quota_core.py
│   ├── test_source_adapter.py
│   ├── test_poller.py
│   └── test_storage_schema.py
├── docs/
│   ├── COMPLIANCE_CHECKLIST.md
│   └── LOCAL_SOAK_TEST.md
├── .env.example
├── pyproject.toml
└── QUOTA_ALERT_PLAN.md
```

部署准备文件位于 `deploy/systemd/`。这些模板不包含密钥，也不代表已经完成 VPS、HTTPS、监控或生产验证。

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
3. 完成 Email outbox、重试、退订、激活测试邮件和延迟监控。
4. 完成 Trial 一次性限制、预约目标数量限制和延长保障逻辑测试。
5. 完成隐私声明、服务条款、退款规则和免责声明。
6. 使用少量获同意用户进行试运营，再根据真实数据调整周期与价格。

完整路线见 [QUOTA_ALERT_PLAN.md](QUOTA_ALERT_PLAN.md)。
