# CB 月度调仓运行手册（首次：2026-11-02）

> as_of: 2026-10-23 10:53（preflight 记录；所有相对日期已按此日求值并冻结）
> 口径：月度 snapdelay + par（`schedule=monthly_snapdelay`）。执行日 = **快照日之后的首个交易日**；快照日 = 每月 1 日（1 日休市取上月末收盘）。CB 资金 60 万，双低 TOP25，一手=10 张，价格取快照（无前视）。
> 前置预检（10-21）：`logs/preflight_1102_monthly_20261021.md`（周日快照路径历史、验证器四例、残余风险）；本文件为 10-23 更新版（补验证器状态落盘/健康行/停止判据）。
> 回测依据：月度 40/60 = 12.54%（+3.58pp，`logs/cb_sched_compare_nav.csv` 的 `nav_snapdelay_par`）；100 万实盘口径三件套见 `logs/three_piece_40_60_100w_40_60_20w_20261021.json`。

## 一、11-02 日程与预期

| 时刻 | 任务 | 预期 |
|---|---|---|
| 06:30 周一 | `build_cb_data.py` 周更（全量） | 建出 snap_date=2026-11-01（用 10-30 收盘） |
| 08:10 | `build_cb_snapshot_recent.py` | 当月快照已存在→跳过；周更仍在跑→跳过并等 13:30 |
| 13:30 | 同上（幂等重试） | 保证 14:28 前 `cb_snapshots` 有 11-01 |
| 14:28 | `cb_signal.py` | `rebalance=True`、`snap_date=2026-11-01`、`latest_snapshot=2026-11-01`、holdings=25 |
| **14:30** | `QuantTrackA_Daily` → `run_daily.bat` | 两轨顺序执行：`fetch_and_execute --track a` → `--track cb`；按 buy/sell 增量下单；tick 归一、单笔≤10 万、涨停禁买/跌停禁卖 |
| 14:45 | `Quant-TrackACheck` → `check_exec.ps1` | **只检查/补跑 track a，不补 cb** |
| 15:40 | `verify_cb_monthly_rebalance.py` | selftest 通过 + 五项字段 PASS；状态落 `logs/cb_monthly_verify_state.json`（**跳过也落**，18:40 健康行 `cb_monthly_verify` 可见；含 `next_expect_day`=下一次调仓日） |

关键日期核对（`trade_calendar`，2026-10-23 查）：10-30 周五交易、**11-01 周日休市**、11-02 周一交易。

## 二、Preflight 记录（2026-10-23 10:53 实测）

1. `verify_cb_monthly_rebalance.py --selftest`
   → `SELFTEST_OK (good case passes)` + `SELFTEST_OK (2 known failures caught)`（rebalance=False / 旧 snap_date 均被抓到）。
2. `expected_rebalance_day('2026-11-01')` → **2026-11-02** ✓；节假日路径 `('2026-10-01')` → 2026-10-08 ✓。
3. 建表日期逻辑：`pd.date_range('2026-11-01','2026-11-02',freq='MS')` = `[2026-11-01]`；`valid_dates = [d for d in all_dates if d <= '2026-11-01']` → **2026-10-30**（休市取上月末收盘，无前视）。
4. 当前信号 `signal_cb_latest.json`（2026-10-22）：`rebalance=False`、`snap_date=2026-10-01`、`latest_snapshot=2026-10-01`、holdings=25 → **11-02 触发条件成立**（`latest 11-01 > prev 10-01`）。
5. 数据：`cb_snapshots` 最新 2026-10-01（35772 行，10 月 310 只）；CB 日线 50 只抽样中 11 只止于 2026-10-22，其余尾部自然停在退市日 → 周五 19:45 stage3 + 周一 06:30 周更覆盖 10-30。
6. 交易日历：`trade_calendar` 覆盖至 2026-12-31，含 11-02。

**盲区声明**（闸门七）：本 preflight 只验证日期映射、字段判据与现有工件；**未验证**（也无法在 11-02 前验证）：
- 06:30 周更是否会实际产出 11-01 快照（取决于数据源与运行时长）；
- 08:10 与周更的并发竞争（代码有 `pgrep` 跳过 + 13:30 兜底，但未在真实周一日实跑过）；
- **14:45 的 `check_exec.ps1` 只补跑 track a，不补 cb**；若 14:30
  `run_daily.bat` 在 cb 之前中断，CB 无自动重试，只能靠 15:40 验证器
  告警后人工补跑；
- 券商柜台对 60 万/25 只的批量委托接受度（仿真盘 11-02 才首次跑）；
- 11-01 快照的券池与 10-01 的真实 churn（>20% 会被闸门拒写并转人工）。

## 三、停止/升级判据（任一触发即停，人工确认后才继续）

1. **13:30 后** `cb_snapshots` 仍无 `2026-11-01` → 停止当日 14:30 执行（信号会用旧快照，等于不调）。
2. 14:28 信号 `rebalance=False` 或 `snap_date≠2026-11-01` → 停止执行（防止用旧快照重复调仓）。
3. 14:28 `holdings=0` → 停止（全量剔除事件，先查 `excluded`）。
4. 15:40 verifier FAIL 或 SELFTEST_FAIL → 停止，人工核对；拒单/废单按 tick 纪律（`qmt_client._tick_price`）处理。
5. 执行后 CB 只数 <25 或单只张数偏离信号 >1 手 → 记录对账差异。**月度口径下不在月内追补**（非调仓日 buy=[]），缺口等到 12-01 月度调仓；若差异来自拒单/废单，按执行层告警单独处理。

## 四、变更历史

- 2026-10-21：月度日程定稿（双周→月度 snapdelay+par）；验证器与快照补齐器上线。
- 2026-10-22：08:10 补建增加 13:30 幂等重试（用户授权）。
- 2026-10-23：本 preflight 记录（10:53）。
