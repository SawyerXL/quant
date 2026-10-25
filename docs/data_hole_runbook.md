# 数据洞扫描与受控回填 Runbook（2026-10-25 立）

> 适用：交易日历覆盖率异常、宽宇宙数据洞、停牌与真洞的区分。
> 纪律：**先取证 → 独立源确认 → 再受控回填**；不因扫描告警直接全量重拉；
> 任何写入前备份；补洞前不引用受影响窗口的回测/研究数字。

## 1. 扫描（只读）

```bash
# 近端（2026 起）扫描；--full 才读全史（旧版 --full 名不副实，已修）
python3 scripts/calendar_coverage_scan.py
python3 scripts/calendar_coverage_scan.py --full   # 慢，周度/手动

# 日级频次：区分“同几日期的全局洞” vs “个股停牌/个股洞”
python3 scripts/calendar_gap_dates.py
```

2026-10-25 修复点：旧版把**上市前**的交易日算成缺口（首次运行 3537 只，
前 20 全是 2026-09/10 新股）；现在只统计 `[first_bar, last_bar]` 内缺失，
并输出 `first_bar/last_bar/active_missing/stale_end`。文件名也已从
`{time:...}` 字面量改为实际日期。

**盲区（必须声明）**：日历源 = SH000001 自身；**指数自身的洞**（如 2007-10
缺 3 天）本扫描发现不了，需要 Sina/baostock 独立日历源比对。

## 2. 独立源确认（写前必做）

对每个候选票/日期，用独立源确认“源有 bar、本地没有”：

```python
import akshare as ak
d = ak.stock_zh_a_daily(symbol="sh601127", start_date="20260710",
                        end_date="20260825", adjust="qfq")
```

- 源有 bar → 本地洞，可回填；
- 源也无 bar → **停牌**（或退市），不补，记入 `gap_skip`；
- 源连接失败（No value to decode / RemoteDisconnected）→ 重试或换源，
  不得当停牌。

## 3. 受控回填（只补缺失日）

```bash
# 先 dry-run 看锚/行数/跳过原因
python3 scripts/backfill_2026_holes_full.py --universe current800 --dry-run
python3 scripts/backfill_2026_holes_full.py --codes 601127,600875 --dry-run

# 实写：优先当前 CSI800（实盘面板），再 pit800，最后 all
python3 scripts/backfill_2026_holes_full.py --universe current800
python3 scripts/backfill_2026_holes_full.py --universe pit800
python3 scripts/backfill_2026_holes_full.py --universe all --rescale --sleep 0.03
```

规则（脚本已实现）：
1. **只补本地缺失日期**；已有行 keep first，不覆写；
2. **只补源有 bar 的日期**；源无 bar 跳过并记录（停牌）；
3. **锚校验**：洞前/洞后重叠日 `source_close / local_close` 须在 ±0.5%；
   两锚一致但整体偏离 1>0.5% → `--rescale` 按 `1/ratio` 统一重锚
   （qfq as-of 基差，不是跨洞漂移）；两锚互相分歧 → 跳过，人工；
4. **备份**：写前复制 2026 parquet 到
   `data_store/backup_2026_holes_20261025/`（幂等，保留首次备份）；
5. **单位/连续性**：OHLC 括号、量额>0、`amount/volume` 与 close 的
   比值稳定（qfq 与 raw 的固定因子允许，跳变不允许）、>50% 日跳拒绝；
6. **pct_chg**：新增行用源值；洞边界一根按合并后的前收重算；洞前已有行不动。

## 4. 验收（写后）

```bash
python3 scripts/calendar_coverage_scan.py        # 重扫
python3 scripts/verify_backfill_2026.py          # 逐票完整性/异常/停牌清单
```

验收项：
- 已填票 2026 缺失归零（停牌除外）；
- 无重复日期、无 >50% 跳变、无 OHLC 括号错误、量额>0；
- `amount/volume` 的 qfq 固定因子在填前/填后一致（不因回填产生台阶）；
- 下游冒烟：信号面板（793 只 CSI800）能正常加载；必要时重跑分组信号的
  `--plan-only`；补洞后再重跑受影响的研究（如 washout 2026 OOS）。

## 5. 2026-10-25 案例（已执行）

- 规模：3500/5174 只缺 2026-05-11~06-16 / 07-01~07-06；峰值日约 65%；
  CSI800 PIT 338/1335；当前官方 CSI800 30/800；另有 07-13~08-21 子集洞
  （30 只当前 CSI800）和个股洞/停牌。
- 独立源：Sina 确认 601127/600875/600499 在洞内有 bar；600438/000959/
  601995 的短缺口源也无 bar → 停牌。
- current CSI800：31 只 / 872 行，锚 1.0；002756 按 1.00835 重锚；
  残余 13 只 `source_has_no_bar`（停牌）+ 601059/601198 `source_empty`
  （待复核，疑停牌）。
- 全库 all 回填：见 `logs/backfill_2026_holes_20261025.json`（分 universe
  另存 `_current800.json`、`_002756.json`）。
- 补洞前 H-W1 的 2026 OOS 标“待复核”；补洞后重跑并更新
  `logs/washout_hw1_result_20261025.md` 与多重检验账本 N=79。

## 6. 禁止事项

- 不把扫描告警直接送入全量重拉队列（含停牌/新股/退市）；
- 不在盘中写数据（会污染信号链）；
- 不在补洞前切 100 万实盘或引用受影响窗口的研究数字；
- 不跳过独立源确认（P0 纪律：取证判错只浪费时间，写入判错损害生产）。
