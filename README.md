# ID Quota Alert

一个面向普通用户的 **HKID 公开预约配额变化提醒服务**。

> 本项目不是香港政府或入境事务处的官方服务，不提供自动预约、代抢、验证码绕过或预约成功保证。用户收到提醒后仍需自行前往官方预约系统完成预约。

## 当前状态

项目已从纯 M0 规划进入 **M1：可靠事件核心**。

目前已经实现：

- 配额领域模型：`unavailable / limited / available`。
- `ValidatedSnapshot`：只有通过验证的完整快照才允许驱动状态变化。
- `quota_observations` 审计模型：获取失败、解析失败与配额状态严格分离。
- Confirmed State 状态机。
- 连续缺失确认机制：单次缺失不会直接关闭现有名额 occurrence。
- `occurrence_id`：支持“消失后再次出现”生成新事件。
- 初始基线模式：服务首次启动时记录当前状态但不发送历史提醒。
- SQLite 初始 schema。
- `notification_outbox` 数据库级唯一约束，避免同一事件重复创建通知。
- Outbox lease 字段，为 worker 崩溃后的安全重试预留基础。
- 核心状态机与 SQLite 约束测试。

目前 **尚未实现**：

- GovHK 实际网络请求与页面/API 解析器。
- 定时 Poller。
- Email 投递 worker。
- 多用户 CLI 管理流程。
- 公开收费、注册、支付或用户后台。

因此当前仓库仍不可直接作为生产服务运行。

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

## 本地开发

```powershell
Copy-Item .env.example .env
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m pytest
python -m app
```

Python 要求：**3.11+**。

## 当前代码结构

```text
id_quota_alert/
├── app/
│   ├── __init__.py
│   ├── __main__.py
│   ├── config.py
│   ├── quota.py
│   ├── observations.py
│   ├── events.py
│   └── storage.py
├── tests/
│   ├── test_project_skeleton.py
│   ├── test_quota_core.py
│   └── test_storage_schema.py
├── docs/
│   └── COMPLIANCE_CHECKLIST.md
├── .env.example
├── pyproject.toml
└── QUOTA_ALERT_PLAN.md
```

后续的 `matcher.py`、`notifier.py`、`scheduler.py` 与 `cli.py` 会在对应里程碑再加入，不提前制造复杂度。

## 数据来源

计划只使用获准的 GovHK / 入境事务处公开配额信息：

- [GovHK 人事登记办事处预约配额预览](https://www.gov.hk/tc/apps/bookidcardquota.htm)
- [网上预约申领香港智能身份证](https://www.immd.gov.hk/hkt/hkid.html)

实际预约情况始终以官方系统为准。

## 上线前要求

公开收费前必须完成：

1. 确认公开配额数据的自动读取、第三方提醒及商业使用边界。
2. 完成真实 source adapter，并验证不会因异常页面制造假事件。
3. 完成 Email outbox、重试、退订、激活测试邮件和延迟监控。
4. 完成 Trial 一次性限制、预约目标数量限制和延长保障逻辑测试。
5. 完成隐私声明、服务条款、退款规则和免责声明。
6. 使用少量获同意用户进行试运营，再根据真实数据调整周期与价格。

完整路线见 [QUOTA_ALERT_PLAN.md](QUOTA_ALERT_PLAN.md)。
