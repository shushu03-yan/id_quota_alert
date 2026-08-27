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

## 核心原则

### 1. 只提醒，不代抢

- 不自动预约。
- 不控制官方预约页面。
- 不绕过验证码、排队或其他安全机制。
- 不收集 HKID、证件号码、出生日期、签证编号或查询代码。

### 2. 一个 Poller 服务所有用户

未来所有用户共享同一份公开配额采集结果：

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
Subscription Matching
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

1. 确认公开配额数据的自动读取及商业提醒使用边界。
2. 完成真实 source adapter，并验证不会因异常页面制造假事件。
3. 完成 Email outbox、重试、退订和延迟监控。
4. 完成隐私声明、服务条款、退款规则和免责声明。
5. 使用少量获同意用户进行试运营，再决定是否增加 Telegram、在线支付或 Web UI。

完整路线见 [QUOTA_ALERT_PLAN.md](QUOTA_ALERT_PLAN.md)。
