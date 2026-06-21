# ⚠️ 作废回测脚本 — CSI500 N=6 (勿信其输出)

这三个脚本（`backtest_csi500_regime.py` / `backtest_csi500_v2.py` / `backtest_csi500_full.py`）
**存在日期错位 bug，OOS 收益被严重虚高，结论作废。**

## bug 是什么
脚本用 `if len(score)<N: continue` 跳过早期（2019 全年）因动量回看不足而出分不够的调仓日，
却用 `index = rd[:len(nav)]` 给净值序列贴标签 —— 把有效净值的日期标签**整体前移约一年**，
正好把 2023-25 强势段塞进 "2024-25 OOS 窗口"，制造出 **OOS +39.6%** 的假象。

## 真实结论（2026-06-21 钉死实验）
同一份净值，只换日期标签：
- `rd[:len]`（bug 方式）→ OOS +35.3% / 回撤 -13.3%
- 真实日期（正确）   → **OOS +2.7% / 回撤 -40.1%，全期回撤 -73%**

且止损 A/B 证明：加 MA10/硬止损只会更差（+2.7% → -11~-17%），救不活。
→ **CSI500 N=6 策略已否决**，与 Track B / Top3 / 小盘同列。

## 注意
- **Track A（实盘）不受此 bug 影响**：`run_backtest_a.py` 用 `pd.Series(0.0, index=all_dates)`
  按全日级预分配索引，日期对齐正确。
- 教训：任何回测 OOS 验收前，先核对净值序列的日期标签 = 真实记录日期，
  尤其当存在"跳过部分调仓日 + 用 rd[:len(nav)] 贴标签"的写法时。
- 验证用脚本：`scripts/recon_datealign.py`（隔离实验）、`scripts/backtest_csi500_stop_ab2.py`（止损A/B，正确记账）。
