# 10-20 14:20 残差审计 + 解锁记录（机器值）

- 执行时刻：2026-10-20 14:21 CST（预授权窗口 14:30 前）
- 权威锁：`H:\quant\logs\manual_exec_lock.json` 在场（`{"date":"2026-10-20","reason":"post-exam residual review"}`）
- 数据来源：Windows 直连 QMT 客户端（account=1633013579）`get_positions()` / `get_today_orders()`；
  对照 `logs/qmt_positions_20261019.json`（exported_at=2026-10-19T15:05:07）。

## 审计结果

| 检查项 | 结果 |
|---|---|
| 今日委托 | **0 笔** |
| 正持仓 | 26 只（25 转债 + 511360 6100） |
| 与 10-19 快照逐券差量 | **NONE**（26 vs 26，volume 全等） |
| 10-19 考试日委托 | 11 笔（全部转债买卖），零拒单 |
| 死区项 | 123107 213 vs 目标 210 = 3 张（<10 张，预期） |
| 强平/强制退出推迟 | 无 |

**判定**：10-19 考试日残差只剩预期项（死区 3 张）→ 按预授权解锁。
执行：14:21 `del /q` 移除 Windows 权威锁，复核输出 `UNLOCKED`（文件不存在）。

## 非预期项（不阻塞 CB 链，但必须挂账）

- **002028.SZ −399 股**：`volume=-399, cost_price=204.077, market_value=-44,923`。
  - 归属：**先于 10-19 考试日存在**的仿真户卖空伪影（10-19 的 11 笔委托全为转债，
    今日 0 委托；若由 10-19 产生则必有对应委托，未发现）→ 不是考试残差。
  - 看不见的原因：`export_qmt_positions.py` 用 `volume > 0` 过滤，负持仓被
    静默排除 → 快照/对账全链对负持仓**盲**（闸门十四类盲区）。
  - 影响面（已核代码）：
    1. `track_cb`：`_in_scope` 只放转债 → CB 执行链**不会**碰 002028（今日 14:28 安全）。
    2. `track_a` 熊市清仓：`if shares <= 0: continue` → 不会下卖单。
    3. `track_a` 风险网关：`current_nav` 含该空头浮盈 **+36,504**（399×(204.08−112.59)），
       `cash = capital − mv_total` 因负市值多算 **+44,923**；nav_high 可被抬高。
    4. 未来调仓若目标含 002028：`buy_qty = target − (−399)` → 多买 399 股（约 4.5 万，
       单票超配风险）。
  - 时间边界：今日不是调仓日；**下一股票调仓日=2026-10-29**（日历核实：
    10-19 为月中调仓日，10-20 非；月末=10-29）。修复窗口=10-29 前。

## 待办（2026-10-21 跟进：三项已闭合）

1. ✅ **负持仓守卫已实装并部署**：`trader._scoped_positions` 按 0 持仓隔离
   + 去重告警，`_build_gateway` 的 NAV/cash 只用过滤后持仓（829e05a /
   0df56f1；Windows 已同步）。
2. ✅ **导出 + 健康行可见**（2026-10-21，7e67a1b）：`export_qmt_positions.py`
   新增 `split_positions()` 与 `negative_positions` 字段（不再 `volume>0`
   静默丢弃）；`daily_health_line.py` 读取该字段并进 alerts；
   `tests/test_export_positions.py` 2 用例通过。Windows 已 scp 部署，
   11:36 冒烟实测 `002028 −399` 已落盘（cost/mv 当日为 0）。
3. ✅ NAV 排除：守卫过滤后的持仓进入 `_build_gateway`，负持仓的 mv/cost
   不再计入 current_nav/cash。
