# 11-02 首次月度 CB 调仓 — 盘前预检（2026-10-21 11:32）

## 已验证项

| 项 | 结果 | 依据 |
|---|---|---|
| 快照日 11-01 是周日 | 路径历史已跑通 | 生产表 2026-02-01（周日）377 只、2026-03-01（周日）371 只；`build_snapshots` 取 ≤快照日的最近交易日 |
| 验证器预期调仓日 | `expected_rebalance_day('2026-11-01') = 2026-11-02` ✅ | 直接调用（纯函数） |
| 国庆对照 | `('2026-10-01') = 2026-10-08` ✅ | 同为"快照日后首个交易日"语义 |
| 月度触发边界 | None→False（迁移）/ 10-01→11-01 True / 相等 False / 旧快照 False ✅ | `monthly_snapshot_trigger` 四例 |
| 当前生产快照 | 2026-10-01，310 只 | `cb_snapshots` |
| 实盘 CB 持仓 | 25 只，快照 exported_at 2026-10-20T15:05 | 持仓导出（非空→非调仓日走"沿用上一信号"分支） |
| 上一信号 schema | 10-20 旧 schema（无 snap_date）→ 今天 14:28 走迁移分支 | `signal_cb_latest.json` |
| 验证器 selftest | PASS（1 正常 + 2 已知失败） | `--selftest` |

## 11-02 时序与预期

1. **08:10** `build_cb_snapshot_recent.py`：today(11-02) > first(11-01)，
   生产最新快照 10-01 < 11-01 → 补建；`build_snapshots(start=11-01,
   end=11-02, freq='MS')` 生成 11-01 快照（价格取 10-30 收盘）。
2. **14:28** `cb_signal.py`：`prev_snap=2026-10-01`、`latest=2026-11-01`
   → `rebalance=true`，输出全量 buy/sell 差集。
3. **14:45** Windows 执行；**15:40** 验证器真检（快照/信号/持仓三查）。

## 残余风险与处置

- **周一 06:30 全量周更拖过 08:10** → 快照补建跳过（`pgrep` 守卫），
  14:28 无新快照 → `rebalance=false`；次日 08:10 补建、14:28 触发，
  调仓顺延一天。验证器 11-02 会报"预期调仓日=今天但 rebalance=false"。
  处置：当日 08:15 查 `logs/cron_cb_snap_recent.log`，未建则人工重跑。
- **14:28 信号失败** → 查 `logs/cb_signal_2026-11-02.log` 与
  `logs/cron_cb_signal.log`；验证器 15:40 兜底告警。
- **执行拒单** → 按 F2 v2 判据：拒单即停，不追单，人工确认后再补。

## 当日检查命令

```
tail -20 logs/cron_cb_snap_recent.log      # 08:15
tail -40 logs/cb_signal_2026-11-02.log     # 14:35
tail -20 logs/cron_cb_monthly_verify.log   # 15:45
```
