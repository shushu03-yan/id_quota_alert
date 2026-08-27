# 上线前合规与隐私检查清单

> 本清单是项目上线门槛，不是法律意见。任何一项关键数据使用边界未确认时，不公开收费。

## 工程证据状态（2026-08-28）

- [x] 自动测试覆盖 Source Adapter、Poller、State、v1/v2→v3 migration、Matcher、Outbox、Email Worker、激活码、Email Verification、套餐限制、Trial、延期保障、Magic Link、退订、Web 路由、Backup 与 Health。
- [x] 代码使用单一共享 Poller；没有实现自动预约、验证码/排队/限流绕过、账号密码系统或按套餐加速。
- [x] activation / verification / magic token 数据库仅存 hash；SMTP 配置仅从环境变量读取。
- [ ] 真实 3–7 天 soak test（NOT YET VALIDATED）。
- [ ] 真实 Email Provider 测试及完整人工端到端链路（NOT YET VALIDATED）。
- [ ] restart 与 backup restore 演练（NOT YET VALIDATED）。
- [ ] 数据读取、第三方提醒、商业收费边界确认（NOT YET VALIDATED）。

以下原始上线清单继续保持未勾选，只有获得对应真实证据后才可逐项确认。

## 数据来源与商业用途

- [ ] 已向 GovHK / 入境事务处说明实际服务模式。
- [ ] 已确认是否允许程序周期性读取相关公开 quota 数据。
- [ ] 已确认是否允许基于相关数据提供第三方提醒服务。
- [ ] 已确认是否允许该提醒服务收费。
- [ ] 只访问获准的公开数据，不进入预约内部流程。
- [ ] 不绕过验证码、排队、限流或其他安全机制。
- [ ] 获取频率有明确上限、timeout、jitter 和指数退避。
- [ ] 用户数量增加不会增加来源网站的请求频率。
- [ ] 页面和邮件清楚标注数据来源及非官方身份。

## Source Safety

- [ ] Fetch timeout / HTTP error 只记录 observation failure，不改变 quota state。
- [ ] Parser error 只记录 observation failure，不改变 quota state。
- [ ] 明显为空或不完整的 snapshot 会被拒绝。
- [ ] 已验证页面/API 结构变化能够快速触发错误或告警，而不是静默误报。
- [ ] 已记录 `payload_hash`、`parser_version`、`observed_at` 与可用的 `source_updated_at`。
- [ ] 已验证连续缺失确认策略，不会因一次异常快照制造“消失后重现”的假事件。

## 顾客资料

- [ ] 只收集提供服务必需的邮箱、订阅期限和预约目标。
- [ ] 不收集 HKID、证件号码、出生日期、签证编号或查询代码。
- [ ] Trial 一次性限制仅使用邮箱或订单历史判断，不额外采集设备指纹、身份证明或手机号。
- [ ] 已发布隐私声明，并说明资料用途、保存期限和删除方式。
- [ ] 日志、备份和错误报告会遮盖邮箱及密钥。
- [ ] SMTP/API 密钥只通过安全配置提供，不提交到 Git。

## 通知

- [ ] 每位顾客独立发送，不使用共享 To/CC/BCC 群发。
- [ ] 邮件提供准确发送者身份、联络方式和退订入口。
- [ ] 退订后立即停止创建新通知。
- [ ] 标题不误导，不承诺预约成功或固定秒级提醒。
- [ ] 激活测试邮件明确标注“非真实配额提醒”，不让用户误认为当前存在名额。
- [ ] Notification outbox 使用数据库唯一键防止重复创建。
- [ ] Worker 使用 expiring lease，崩溃后任务可恢复。
- [ ] 已接受 Email 语义为 at-least-once + best-effort deduplication，而非承诺 exactly-once。

## 套餐与产品表述

- [ ] 所有套餐共享同一份配额采集结果和同一通知链路。
- [ ] 不把“更高轮询频率”“VIP 优先”“一定更快”“一定抢到”作为收费卖点。
- [ ] 不使用“目标必达”“保证抢到”“未成功自动延期”等容易被理解为预约成功承诺的表述。
- [ ] Family 不宣传“连号”或“多张必得”，只说明多人同时接收提醒和自行协调预约。
- [ ] 前台使用“预约目标”而不是技术化的“规则组”。
- [ ] Goal / Family 的延长保障明确以“原服务期内 0 个有效匹配 quota event”为触发条件。
- [ ] 延长保障最多触发一次，并明确延期不代表未来一定出现名额。
- [ ] Trial 每个邮箱最多使用一次。
- [ ] V1 价格（¥6 / ¥18 / ¥59 / ¥99）被视为试运营基准，可根据真实数据调整，不作为永久价格承诺。

## 可靠性与监控

- [ ] 已设置最后成功获取时间监控。
- [ ] 已设置 source/parser failure 告警。
- [ ] 已设置投递失败和服务停止告警。
- [ ] 已记录 detect / queue / provider latency。
- [ ] 已记录 `activated_at`、`first_matched_event_at`、`first_notification_queued_at`、`first_provider_accepted_at`。
- [ ] 已观察 P50 / P75 / P90 首次匹配等待时间。
- [ ] 已观察 P50 / P95 通知链路延迟。
- [ ] 已统计 Goal / Family 延长保障触发率。
- [ ] 已验证数据库备份能够恢复。
- [ ] 已完成方案列出的关键自动测试和重启恢复测试。

## 商业与产品页面

- [ ] 已准备服务条款、免责声明和退款规则。
- [ ] 商品页面不使用政府标志，不使顾客误认为官方服务。
- [ ] 明确用户收到通知后仍需自行进入官方预约系统。
- [ ] Family 在 V1 先隐藏/手动开通，确认真实多人需求后再决定公开销售。
- [ ] 1 日体验明确用于验证邮件链路和真实服务存在，不承诺 24 小时内一定出现配额提醒。

## 代码与发布

- [ ] 已决定项目采用开源服务收费还是闭源商业服务路线。
- [ ] 在分发策略明确前，没有随意添加宽松商业开源许可证。
- [ ] 仓库、Issue、Actions 日志和发布包不包含顾客资料、数据库或真实密钥。
