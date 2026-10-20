# NAV 追踪分腿修正（2026-10-20，机器值）

## 问题

`scripts/qmt_nav_track.py` 旧实现用 **100 万单一 notional**，但快照持仓包含
**CB 腿（25 只，约 58.6 万）**；CB 腿的盈亏被并入股票腿 NAV，导致：

- 最新 `invested_pct` = **126.9%**（持仓市值 128 万 / NAV 101 万）；
- `nav=1.010726` 既不是股票腿 NAV，也不是组合 NAV——是两腿混算的结果；
- 组合绩效与账户回撤显示被污染。

## 修正

分腿 mark-to-market（同一快照、同一价格口径）：

| 列 | 含义 |
|---|---|
| `nav` / `nav_a` | 股票腿（100 万，剔除 CB） |
| `nav_cb` | CB 腿（60 万） |
| `nav_combined` | 组合（股票 equity + CB equity）/ 160 万 |
| `equity_*/invested_*/n_pos_*/ret_*` | 分腿明细 |

测试：`tests/test_nav_track.py`（合成两日快照，股票 +10%、CB +10%，断言
nav_a=1.01、nav_cb=1.016667、nav_combined=1.0125、invested_pct<100%）→ 通过。

## 修正后最新（2026-10-20）

| 口径 | 累计（07-20 起，92 天） | 年化(外推) | 最大回撤 | 夏普 |
|---|---:|---:|---:|---:|
| 股票腿（1M） | **+1.96%** | +8.0% | −5.30% | 0.67 |
| CB 腿（600k） | **−0.99%** | — | — | — |
| **组合（1.6M）** | **+0.85%** | +3.4% | **−3.71%** | 0.46 |

（样本仅 92 天，年化/夏普仅供参考。）

## 影响面

- **熔断 15% 线不受影响**：trader 的 `_build_gateway` 按 track 名义资金与
  `_in_scope` 分腿计算，本修正只改**报告口径**。
- 旧 `nav` 语义修正为股票腿；组合绩效看 `nav_combined`。
- `baseline_snapshot.py` 读取最后一行全部列，自动带上新列；web 若展示账户
  NAV 应改用 `nav_combined`。
- 历史 `logs/qmt_nav_history.parquet` 已按新口径重建（58 行）。
