# qfq 重锚模式 — 002250 首例（2026-10-22）

## 1. 触发（P1 漂移门）

- `logs/qfq_drift_002250_20261022.json`：`anchor_shift`，last_date 2026-10-21，
  库内 **13.76** vs 新鲜源 **13.66**，比值 **−0.73%**；最近 5 个共同日
  −0.71%~−0.73% 同向一致（不是单点噪声）。

## 2. 独立源确认（P0：先确认后写）

- **公告**（MCP）：2026-10-16「2026年半年度权益分派实施公告」。
- **分红**（MCP）：2026-06-30 中期，每股现金（税后）**0.1 元**；
  `div_proc=预披露`（MCP 尚未回填 ex_date）。
- 数量级核对：0.1 / 13.76 ≈ **0.73%**，与源端重锚幅度一致；源端重锚日
  = 2026-10-22（ex-date）。→ 确认是**新除权重锚**，不是数据错误。

## 3. 重锚执行（手动、备份、可复检）

- D 重建脚本新增 `--reanchor-file`：不受 rebal60 池限制；**跳过前沿锚**
  （0.5% 阈值会把 0.73% 的整体偏移误判为"不过"）；源端权威；备份+全史
  按年覆写；保留源端 max 之后 bar。
- 命令：
  `python scripts/rebuild_d_universe_qfq.py --reanchor-file logs/reanchor_002250.txt --json-out logs/reanchor_qfq_20261022.json`
- 结果：`n_codes=1, n_fixed=1, n_skipped=0, n_failed=0, n_tail_kept=0`；
  **002250 全史 4,423 行 qfq 覆写**；前沿偏离 0.0073 已记录；备份在
  `data_store/backup_qfq_rebuild_20261021/`（与 D 同一备份目录）。

## 4. 复检（P1 重跑）

- `logs/qfq_drift_002250_post_20261022.json`：
  库内 **13.66** = 源端 **13.66**，最近 5 日比值 **全 0**，`status=ok`，
  `flags=[]`。
- 说明：10-22 的 bar 由 17:00 日更追加（源端 ex-date 收盘），明日 08:20
  P1 会以 10-22 为共同日再检（预期 0）。

## 5. 残余与下一步

- 002250 不在策略 TOP30 池（股票腿当前 bear/0），本重锚属数据质量修复，
  不改变任何策略参数。
- 重锚流程（写死，仅手动触发）：
  **P1 `anchor_shift` 告警 → 核对公告/分红 → 写重锚清单 → D `--reanchor-file`
  → P1 复检 → 健康行清零**。不做自动重锚；无公告确认不写库。
- baostock 独立源尚未重锚，其与库内差 0.73% < 1% 独立源容差（D 验收口径），
  不影响 PASS；若未来 baostock 先重锚，P1 以新浪为准、baostock 只作参照。
- 若 TOP30 池出现 flag：按同一流程处理；健康行 18:40 会显示 `qfq_drift`
  与告警，08:20 cron 每日检测。

## 6. 工件

- `scripts/rebuild_d_universe_qfq.py`（`--reanchor-file`）
- `logs/reanchor_002250.txt`、`logs/reanchor_qfq_20261022.json`、
  `logs/reanchor_qfq_20261022.log`
- `logs/qfq_drift_002250_20261022.json`（重锚前）、
  `logs/qfq_drift_002250_post_20261022.json`（重锚后）
