# 100 万实盘切换清单（2026-10-25 准备；2026-10-26 用户批准）

> 当前 main 仍是仿真 160 万口径（股票 100 万 + CB 60 万）。
> **2026-10-26 用户批准按最优方案执行**；已准备切换分支
> `capital-live100w`（commit `6bc345a`，仅 4 处常量），**尚未合并**。
> 本清单在 **11-02 收盘后、002028 已平仓、11-02 06:30 周更产出
> 11-01 CB 快照** 时执行（11-01 为周日，当天不建快照；首次月度先用
> 现 sim 1M 口径跑完，隔离资金切换）；
> 切换是资金口径变更，不是策略参数变更。
> 部署期望（补洞后复跑）：**live {2,7} = 11.97% / 1.536 / −13.84%**；
> 单组合 13.09/1.598 仅研究基线；股票腿 live 0.28%。
> **2026-10-31 同窗跟踪**：7/20→10/19（51 日）live **+0.82%** vs 回测
> **+1.213%**，跟踪差 −0.39pp、TE 10.6%；11.97% 是长期回测年化，不是近窗
> 业绩；切前读 `logs/live_performance_track.md`，不要用 11.97% 做短期预期。
> **注意**：本切换只改配置/名义资金；真实下单仍需 QMT 真账户/资金到账
> 与 F4 窗口，仿真账户切换不等于真钱开跑。

## 1. 前置条件（全部满足才切）

- [ ] 用户明确 go-live 日期与实盘资金到账（QMT 真账户 100 万）；
- [ ] 10-29 股票月末锚点完成；**002028 −399 已平仓**，negative_positions 为空；
      10-29 Windows 任务 `Quant_Fix_002028_1000`（10:00）/
      `Quant_Fix_002028_1330`（13:30:00）已创建 Ready（用户 2026-10-26 授权），
      助手 10:02/13:32/15:12 复核；细则 `logs/fix_002028_20261029_runbook.md`；
- [ ] **11-02 06:30 周更 / 08:10 补建产出 11-01 快照**（11-01 是周日，
      当天不建快照；13:30 仍无 → 人工 `--force` 补建）；
- [ ] `check_config_alignment.py` 当前 drift=0（仿真口径已对齐）；
- [ ] 1,428 只 anchor mismatch 的残余声明已知：**仅影响全库研究，不影响
      current800/40-60**（见 `logs/anchor_mismatch_decision_20261025.md`）；
- [ ] 切换窗口：**建议 11-02 收盘后**（原计划 10-29 收盘后~11-02 14:25 前；
      首次月度先用现 sim 1M 跑完，避免 cash_sweep/g1 过渡同日）。若坚持
      11-02 前切，必须先完成 cash_sweep 只卖超额/分批修复并 dry-run 通过；
- [ ] **切换时股票腿实际持仓≈0**（当前 bear/0，g0/g1 均空）。若 regime 转多且
      g1 仍有旧 500k 口径持仓：不得只改常量——非调仓日 g1 的 `shares` 会保留
      旧股数而 `effective_capital` 变 200k，合并 preflight 会判资金超配拒单；
      需先做一次性减仓/换仓计划，或在股票腿清零后再切。
- [ ] **切换前先跑新口径 `cash_sweep.py --dry-run`**：当前 511360 约 69.6 万，
      `STOCK_CAPITAL=400k`、tier=0.3 时目标 28 万；现有卖出分支在
      `delta<0` 时会**卖出全部 6100 份（约 69.6 万）**，不是只卖超额，且
      单笔远超 10 万、未过 risk.py。若 dry-run 仍显示“拟卖 6100 份”，
      **必须先把卖出改为只卖超额/分批并过风控，或推迟切换**。
- [ ] **建议 11-02 首次月度继续用现 sim 1M 口径跑完，11-02 收盘后再切
      100 万**：把“首次月度 CB + g0”与“资金口径切换/cash_sweep 过渡”
      隔离，避免同日多变量；10-29 只做 002028 与月末锚点。

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

## 3. 执行步骤（2026-10-26 已预置分支）

**已预置**：分支 `capital-live100w`（commit `6bc345a`）只改 4 处常量；
`check_config_alignment.py` 在分支上已验证 `drift=0`、`live100w`、
grouped/nav/cash = **400,000**、CB 600,000/600,000。**尚未合并 main**。

1. **11-02 收盘后**（002028 已在 10-29 平仓、15:05 快照确认
   negative_positions 空；首次月度已用 sim 1M 跑完）：
   ```bash
   git checkout main && git pull --ff-only
   git merge --no-ff capital-live100w -m "switch: live100w"
   python3 scripts/check_config_alignment.py
   git push origin main
   ```
   期望：drift 0；grouped×2 = nav NOTIONAL = cash STOCK_CAPITAL = **400,000**；
   CB 两处 **600,000**；声明计划 `live100w` 已对齐 ✅。
   若 main 已有新提交导致冲突：保留 4 处 live 值，跑检查后提交；
   不要用通配 add。
2. **11-02 收盘后 ~ 11-03 开盘前** 是切换窗口；10-30/11-01 无交易，
   11-03 开盘链读取新口径（11-02 双事件仍用 sim 1M）。
3. **11-02 14:25 仍按 sim 500,000/组**（首次月度不切资金）；14:28 CB
   600,000；**14:30** `run_daily.bat` 先出 plan 并按 tier 分流
   （tier1 自动 / tier2 需 `--approve` / tier3 逐笔人工）；**14:35**
   复核 `trade_plan_20261102_cb.json` 的 tier；14:45 的 check 只补
   track a，不补 cb；15:40 CB 验证器；18:40 健康行。**11-02 收盘后**
   再合并 `capital-live100w`，11-03 起分组/NAV/cash 为 400k；11-09 g1
   用 200,000/组。
4. 11-02 停止判据照旧（快照/信号/holdings/验证器/拒单），另加
   **tier2/3 未人工确认=未完成**。

**手工 fallback**：若分支不可用，按上表 4 处精确路径手工改，跑
`check_config_alignment.py` 验证；不要用 `git add *`/目录通配。

## 4. 验收

- `check_config_alignment.py` 无漂移、`live100w` ✅；
- 11-02 信号 plan 的组资金/目标股数按 200k/组；CB 600k；
- 15:05 快照 + 17:55 对账 + 18:40 健康行无新增告警；
- 100 万 NAV 追踪（`qmt_nav_track`）按 400k 股票 + 600k CB notional。

## 5. 回滚

若切换后异常：
```bash
git revert -m 1 <merge_commit>     # 或 checkout 切换前 main
python3 scripts/check_config_alignment.py   # 期望 sim160w/500k/1M/1M/600k
git push origin main
```
**不追单、不补单**，当日只做对账。切换是可逆的配置变更，不需要重建数据。

## 6. 不做什么

- 不改策略参数（pool/MA200/max_vol20/TP/熔断 15%+B 全部不动）；
- 不因切换重跑历史回测（11.97 已是补洞后口径）；
- 不在盘中改常量；
- 不把 1,428 只 anchor mismatch 的残余带进“全库研究已干净”的表述。
