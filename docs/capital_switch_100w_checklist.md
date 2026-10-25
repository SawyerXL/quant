# 100 万实盘切换清单（2026-10-25 准备，未执行）

> 当前仍是仿真 160 万口径（股票 100 万 + CB 60 万）。本清单只供
> **用户确认 go-live 后**执行；切换是资金口径变更，不是策略参数变更。
> 部署期望（补洞后复跑）：**live {2,7} = 11.97% / 1.536 / −13.84%**；
> 单组合 13.09/1.598 仅研究基线；股票腿 live 0.28%。

## 1. 前置条件（全部满足才切）

- [ ] 用户明确 go-live 日期与实盘资金到账（QMT 真账户 100 万）；
- [ ] 10-29 股票月末锚点完成；**002028 −399 已平仓**，negative_positions 为空；
- [ ] 11-01 CB 快照就绪（11-02 首次月度前）；
- [ ] `check_config_alignment.py` 当前 drift=0（仿真口径已对齐）；
- [ ] 1,428 只 anchor mismatch 的残余声明已知：**仅影响全库研究，不影响
      current800/40-60**（见 `logs/anchor_mismatch_decision_20261025.md`）；
- [ ] 切换窗口：**10-29 收盘后 ~ 11-02 14:25 前**（避开盘中；11-02 g0 用新资金）。

## 2. 要改的 5 个常量（精确）

| 文件 | 常量 | 仿真值 | live 100 万 |
|---|---|---:|---:|
| `scripts/check_config_alignment.py` | `CAPITAL_PLAN` | `"sim160w"` | `"live100w"` |
| `scripts/daily_signal_a_grouped.py` | `GROUP_CAPITAL` | `500_000` | **`200_000`** |
| `scripts/qmt_nav_track.py` | `NOTIONAL` | `1_000_000` | **`400_000`** |
| `scripts/qmt_nav_track.py` | `NOTIONAL_CB` | `600_000` | `600_000`（不变） |
| `scripts/cash_sweep.py` | `STOCK_CAPITAL` | `1_000_000` | **`400_000`** |
| `scripts/cb_signal.py` | `CB_CAPITAL` | `600_000` | `600_000`（不变） |

结构：股票 40 万 = 2 组 × 20 万；CB 60 万；合计 100 万。

## 3. 执行步骤

1. 备份/提交当前状态；用精确文件路径改上表 5 处（不要用通配 add）。
2. 本地/服务器跑：
   ```bash
   python3 scripts/check_config_alignment.py
   ```
   期望：drift 0；资金口径 grouped×2 = nav NOTIONAL = cash STOCK_CAPITAL
   = **400,000**；CB 两处 **600,000**；声明计划 `live100w` 已对齐 ✅。
3. 10-29 收盘后确认 002028 平仓、11-01 快照状态；11-02 14:25 分组信号
   g0 应使用 **200,000/组**，14:28 CB 使用 600,000；14:45 执行；
   15:40 CB 验证器；18:40 健康行。
4. 11-02 五条停止判据照旧（快照/信号/holdings/验证器/拒单）。

## 4. 验收

- `check_config_alignment.py` 无漂移、`live100w` ✅；
- 11-02 信号 plan 的组资金/目标股数按 200k/组；CB 600k；
- 15:05 快照 + 17:55 对账 + 18:40 健康行无新增告警；
- 100 万 NAV 追踪（`qmt_nav_track`）按 400k 股票 + 600k CB notional。

## 5. 回滚

若切换后异常：把 5 个常量改回仿真值（`sim160w`、500k、1M、1M、600k），
跑 `check_config_alignment.py` 确认 drift 0；**不追单、不补单**，当日只做
对账。切换是可逆的配置变更，不需要重建数据。

## 6. 不做什么

- 不改策略参数（pool/MA200/max_vol20/TP/熔断 15%+B 全部不动）；
- 不因切换重跑历史回测（11.97 已是补洞后口径）；
- 不在盘中改常量；
- 不把 1,428 只 anchor mismatch 的残余带进“全库研究已干净”的表述。
