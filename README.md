# ID Quota Alert

一个规划中的多用户 GovHK 公开预约配额变化提醒服务。

> 本项目不是香港政府或入境事务处的官方服务，不提供自动预约、代抢或成功保证。

## 当前状态

项目目前处于 **M0：方案与合规确认** 阶段，尚不可用于生产环境或收费服务。
完整设计见 [QUOTA_ALERT_PLAN.md](QUOTA_ALERT_PLAN.md)。

## 安全边界

- 只读取公开配额信息。
- 不控制官方预约页面。
- 不绕过验证码或排队机制。
- 不收集或保存身份证明资料。
- 每位顾客独立投递通知，避免泄露其他顾客邮箱。
- 密钥仅通过环境变量提供，禁止提交到 Git。

## 计划中的本地开发方式

```powershell
Copy-Item .env.example .env
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m pytest
python -m app
```

目前入口只会显示“尚未实现”，用于确认项目结构正常；正式功能将在 M1 开始实现。

## 数据来源

- [GovHK 人事登记办事处预约配额预览](https://www.gov.hk/tc/apps/bookidcardquota.htm)
- [网上预约申领香港智能身份证](https://www.immd.gov.hk/hkt/hkid.html)

实际预约情况始终以官方系统为准。

## 贡献与发布

在公开收费前，必须完成方案中的合规确认、隐私说明、退订机制、可靠性测试和
故障监控。请勿把个人邮箱、SMTP 授权码、顾客资料、数据库或运行日志提交到仓库。
