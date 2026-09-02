# M1 本地连续运行（Soak Test）说明

这份文档用于验证 **GovHK Source Adapter + 单一共享 Poller + SQLite Confirmed State/Event** 在真实公开数据上连续运行 3–7 天时是否稳定。

> 这不是生产上线步骤，也不是数据使用许可。开始持续访问来源前，仍应确认允许的自动读取边界与频率；公开收费前还必须完成 `COMPLIANCE_CHECKLIST.md` 中的数据来源与商业用途确认。

## 1. 测试目标

Soak test 不是为了证明“能抓到一次数据”，而是验证以下长期行为：

- timeout、403、429、5xx、解析异常不会把现有名额直接改成 `unavailable`；
- 成功快照可以持续写入 observation，并安全更新 confirmed state；
- 重复 payload 不会重复跑状态机或制造重复 quota event；
- 第一次成功快照只建立 baseline，不发送历史事件；
- 进程停止并重新启动后 baseline、confirmed state、occurrence 与 source 时间仍然连续；
- 来源更新时间倒退时快照记录为 `stale`，而不是覆盖较新的 confirmed state；
- `stale` 保持基础间隔，只有获取、解析或结构失败触发退避；
- `last_poll_attempt` / `last_well_formed_poll` / `last_valid_snapshot` 分别反映进程、响应和数据新鲜度。

## 2. 测试前准备

Python 需要 3.11+。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m pytest
```

默认配置已经可以用于工程验证。若要临时修改 Poller 参数，请直接使用环境变量，例如：

```powershell
$env:POLL_INTERVAL_SECONDS = "60"
$env:POLL_JITTER_SECONDS = "5"
$env:POLL_MAX_BACKOFF_SECONDS = "900"
$env:QUOTA_SOURCE_TIMEOUT_SECONDS = "20"
```

不要通过增加用户数量来增加来源请求频率。MVP 始终只运行一个共享 Poller。

## 3. 先执行一次单次 observation

```powershell
python -m app poll --once
```

成功时应看到类似：

```text
POLL outcome=success applied=true duplicate=false events=0 backoff=false
```

第一次成功快照 `events=0` 是预期行为，因为它只用于建立 baseline。

失败时可能看到：

```text
POLL outcome=fetch_error applied=false duplicate=false events=0 backoff=true error=http_429
```

这类失败也属于有效审计结果；关键是它不能改变 confirmed quota state。

完整但较旧的响应会看到：

```text
POLL outcome=stale applied=false duplicate=false events=0 backoff=false error=source_time_regression
```

它同样不会改变 confirmed state，但下一轮保持基础间隔，不进入指数退避。

## 4. 检查 SQLite 初始状态

默认数据库：

```text
data/quota_alert.sqlite3
```

优先使用内置只读摘要：

```powershell
python -m app health
python -m app soak-summary
```

`health` 分开显示 Poller 活性、完整响应活性和已应用 source 数据新鲜度。`soak-summary` 在 observation 时间跨度不足 3 天时固定显示 `SOAK TEST NOT COMPLETE`；达到 3 天也只会显示需要人工复核，不会自动显示 PASSED。

可以使用 Python 快速查看关键表：

```powershell
python -c "import sqlite3; c=sqlite3.connect('data/quota_alert.sqlite3'); print('observations=', c.execute('select count(*) from quota_observations').fetchone()[0]); print('state=', c.execute('select count(*) from quota_state').fetchone()[0]); print('events=', c.execute('select count(*) from quota_events').fetchone()[0])"
```

查看 runtime health：

```powershell
python -c "import sqlite3; c=sqlite3.connect('data/quota_alert.sqlite3'); [print(r) for r in c.execute('select key,value,updated_at from runtime_state order by key')]"
```

至少应逐步出现：

- `baseline_initialized`
- `last_poll_attempt`
- `last_poll_outcome`
- `last_well_formed_poll`
- `last_successful_poll`
- `last_valid_snapshot`
- `last_payload_hash`
- 来源提供更新时间时的 `last_source_updated_at`
- `last_received_source_updated_at`
- 发生回退时的 `last_stale_snapshot` / `last_source_regression_seconds`

## 5. 开始连续运行

```powershell
python -m app poll
```

持续运行时每轮会输出摘要日志，例如：

```text
quota poll outcome=success applied=False duplicate=True events=0 backoff=False error=- next_poll_in=63.2s
```

或在失败时：

```text
quota poll outcome=fetch_error applied=False duplicate=False events=0 backoff=True error=http_5xx next_poll_in=124.1s
```

观察重点不是每次都必须是新版本，而是 stale 是否被隔离且保持正常间隔、真实失败是否正确退避。

使用 `Ctrl+C` 正常停止。

## 6. 重启验证

至少在 soak test 期间主动执行 2–3 次停止 / 重启：

```powershell
# Ctrl+C 停止
python -m app poll
```

重启后检查：

1. `baseline_initialized` 仍为 `1`；
2. 当前已有开放名额不会因为进程重启被当作全新的首次事件；
3. `quota_state` 中已有 occurrence 不会无故全部变化；
4. 后续真实升级或重新出现仍能创建新的 `quota_events`；
5. `quota_events` 的数据库唯一约束没有产生重复记录。

## 7. 每日检查

每天至少检查一次 observation 分布：

```powershell
python -c "import sqlite3; c=sqlite3.connect('data/quota_alert.sqlite3'); [print(r) for r in c.execute('select outcome, coalesce(error_code, char(45)), count(*) from quota_observations group by outcome,error_code order by outcome,error_code')]"
```

检查最近 observation：

```powershell
python -c "import sqlite3; c=sqlite3.connect('data/quota_alert.sqlite3'); [print(r) for r in c.execute('select observed_at,outcome,error_code,office_count,quota_count from quota_observations order by id desc limit 20')]"
```

检查最近 quota event：

```powershell
python -c "import sqlite3; c=sqlite3.connect('data/quota_alert.sqlite3'); [print(r) for r in c.execute('select quota_date,office_id,from_status,to_status,occurrence_id,observed_at from quota_events order by id desc limit 20')]"
```

## 8. 需要记录的异常

发现以下任一情况都应停止把 M1 视为“可进入 Email 阶段”，先修 Poller / Source：

- 一次 fetch / parse failure 导致大量 confirmed state 变为 unavailable；
- 同一 payload 重复产生事件；
- 重启后当前已有开放状态被批量当成新事件；
- `quota_count` 或 office coverage 突然大幅下降却仍被标记为 success；
- 持续 403 / 429，说明访问方式或频率需要重新评估；
- `last_poll_attempt` 长时间不更新；或 `last_well_formed_poll` 长时间不更新但进程仍看起来“活着”；
- source 时间倒退却仍覆盖 confirmed state；
- SQLite 锁、损坏或事务异常导致 observation 与 state/event 部分提交；
- 未知 quota token 被静默当成 unavailable，而不是 parse error。

## 9. 建议的通过标准

至少连续运行 **3 天**；准备进入少量真实用户 Pilot 前建议达到 **7 天**。

可以把本阶段判为通过的最低标准：

- 没有发现 fetch/parse failure 造成的假 disappearance / reappearance；
- 没有因部署或进程重启制造历史名额提醒；
- 重复 payload 不制造重复 event；
- 发生网络或来源故障时 observation 能准确分类，confirmed state 保持稳定；
- 失败后退避行为符合配置，没有请求风暴；
- SQLite 在停止 / 重启后状态可恢复；
- 真实来源出现的状态值都能被 parser 明确识别；若出现新 token，先更新 parser 与 fixture/test，再继续；
- 没有持续性 403 / 429；若出现，应先降低频率或停止测试并重新确认来源使用边界；
- health timestamps 能用于判断 Poller 是否仍在工作。

## 10. Soak test 结束后

不要仅凭“进程跑满 7 天”就判定通过。先汇总：

- 总 observation 数；
- success / stale / fetch_error / parse_error / rejected 数量与比例；
- 403 / 429 / 5xx / timeout 次数；
- quota event 数；
- 重启次数；
- 是否出现未知 status token；
- 是否出现 source time regression；
- 是否出现人工确认的误报 / 漏报迹象。

确认可靠性核心通过后，再进入下一里程碑：**Email Worker + Outbox 实际投递链路**。
