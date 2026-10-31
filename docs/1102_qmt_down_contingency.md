# 11-02（周一）QMT 断电/未登录应急预案

> 背景：QMT 自 2026-10-28 起 connect result=-1；10-31 22:26 已补装
> 09:10/14:55 登录探针，但登录/仿真权限仍待券商恢复。11-02 是
> **CB 首次月度 + 股票 g0 双事件日**，执行依赖 QMT。本文只列分支与停止判据，
> 不绕过 `execution/risk.py`，不手工改账户。

## 1. 盘前（09:10–09:30）

- 09:10 `qmt_health_alert.py` 自动跑：OK=静默；FAIL=邮件“终端未登录”。
- 09:10 后人工只读确认：
  - `ssh -p 2222 Administrator@127.0.0.1 'tasklist /FI "IMAGENAME eq XtMiniQmt.exe"'`
  - Windows QMT 终端是否已登录；`XtMiniQmt.exe` 是否在。
- **不在 09:30 前修 002028**：按 preflight 判据 6，11-02 双事件日不混做；
  修复最早 11-03（除非用户显式豁免）。
- 100 万切换保持冻结，不因盘前登录恢复而切换。

## 2. CB 快照（06:30 / 08:10 / 13:30）

- 06:30 周更 `build_cb_data.py`；08:10 `build_cb_snapshot_recent.py`
  （已有 11-01 快照则跳过）；13:30 幂等重试。
- 13:30 仍无 **2026-11-01** 快照 → **停止 CB 执行**，人工 `--force` 补建；
  不得用 10-30/旧快照替代。
- 只读检查：`logs/cron_cb_snap_recent.log`、`logs/cb_monthly_verify.log`。

## 3. 盘中（14:25–15:40）

| 时点 | 动作 | QMT 未登录时 |
|---|---|---|
| 14:25 | 股票 g0 信号 | 仍生成；不执行 |
| 14:28 | CB 信号（rebalance/snap_date） | 仍生成；不执行 |
| 14:30 | `run_daily.bat` 出 plan，tier 分流 | plan 可出；执行 fail-fast |
| 14:35 | 人工/助手复核 tier；tier2 才 `--approve`；tier3 逐笔 | 只复核，不下单 |
| 14:50 | `check_execution_chain.py` 只读告警 | 会报“有信号无结果” |
| 15:05 | QMT 快照 | 6/6 失败；记录 |
| 15:40 | CB 月度验证器 | 会 FAIL/跳过；记录 |
| 17:55 | 实盘对账 | 快照陈旧→不可行；记录 |
| 18:40 | 健康行 | 应报 QMT/快照/负持仓告警 |

## 4. 分支处置

### A. QMT 仍 result=-1（最可能）

1. **不上手工单、不绕过 risk.py、不新建 Windows 计划任务。**
2. 记录 `logs/1102_qmt_down.md`：09:10/14:30/15:05 的 result、plan 是否生成、
   CB/股票信号是否正常、快照/验证器状态。
3. CB 月度与股票 g0 **顺延/人工确认**；下次执行窗口按用户裁定，
   不直接套用 11-03 开盘（需重新检查快照/信号是否仍有效）。
4. 002028 仍 −399，修复顺延；100 万切换继续冻结。

### B. QMT 恢复登录

1. 先跑 `scripts/buy_002028_fix.py` dry-run 只读确认 volume/tick/风控；
   **11-02 不 --go**（判据 6）；最早 11-03。
2. CB/股票按 14:30 plan 的 tier 执行；tier2 需 `--approve`，
   tier3 默认逐笔人工/仿真授权；全部过 risk.py。
3. 15:05 快照确认 negative_positions 仍为 002028 −399（不因登录恢复自动清零）；
   15:40 验证器五项 PASS；17:55 对账。
4. 100 万切换仍需：002028 清零 + 400k cash_sweep dry-run + 股票腿≈0
   + 用户重新裁定日期；不因 11-02 双事件跑通就切。

## 5. 停止判据（沿用 preflight）

1. 13:30 后无 2026-11-01 CB 快照 → 停 CB。
2. 14:28 CB `rebalance=false` 或 `snap_date≠2026-11-01` → 停 CB。
3. 14:28 CB holdings=0 → 停，查 `excluded`。
4. 14:25 股票 g0 `is_rebal=false`（11-02 应为 true）→ 停股票。
5. 15:40 验证器 FAIL / SELFTEST_FAIL / 拒单 → 停，人工核对。
6. 002028 未修复 → 11-02 不用该券做新交易；11-02 后补做。
7. plan tier=2/3 未人工确认/执行 → 视为未完成，14:50 告警。

## 6. 只读检查命令

```bash
tail -20 logs/cron_qmt_health.log
tail -20 logs/cron_cb_snap_recent.log
tail -20 logs/cron_signal.log
tail -20 logs/cron_cb_signal.log
tail -20 logs/cron_cb_monthly_verify.log
python3 scripts/check_config_alignment.py
```
