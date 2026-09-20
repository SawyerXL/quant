"""PIT 真宇宙主回测 (2026-09-20, PIT 表到手后)。

实盘语义: 信号链在官方 CSI800 宇宙内做成交额 TOP30 选股。此前回测
pool30 在全面板选(宇宙缺口)。本脚本用 PIT 月度快照(csi800_pit_weights,
92 时点)把选股宇宙钉在真 800 上, 复刻实盘。
臂: A3u(pool30∩PIT800 + 五档 + 组件全开, 现网) vs 官方000906价格指数;
对照: A3 全面板版(旧口径)。判读沿用主回测登记(描述措辞, 配对差分
夏普 + block bootstrap SE, σ≥0.35)。
用法: python scripts/pit_main_backtest.py 10 > logs/pit_main_20260920.log 2>&1
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd

from backtest_config import BacktestConfig, DEFAULT_CONFIG
from backtest_engine import run_backtest, make_rebal_dates, calc_metrics
from backtest_return_attribution import BASE, WINDOWS
from smoke_t1_open import build_panels, START, END

PATHS = int(sys.argv[1]) if len(sys.argv) > 1 else 10


def load_pit_map():
    pit = pd.read_parquet("data_store/meta/csi800_pit_weights.parquet")
    pit["trade_date"] = pd.to_datetime(pit["trade_date"])
    snaps = {d: sorted(set(g["code"])) for d, g in pit.groupby("trade_date")}
    # 前向填充: 每个交易日映射到 ≤ 该日的最新快照
    dates = sorted(snaps)
    cal = pd.date_range("2019-01-01", "2026-08-31", freq="D")
    out = {}
    for d in cal:
        prior = [x for x in dates if x <= d]
        if prior:
            out[d.strftime("%Y-%m-%d")] = snaps[prior[-1]]
    return out


def main():
    panel, open_panel, ap, cal = build_panels()
    from data.storage import load_meta
    ic = load_meta("csi800_index").set_index("date")["close"].sort_index()
    ic.index = pd.to_datetime(ic.index)
    pit_map = load_pit_map()

    base_dates = [d for d in make_rebal_dates(cal, "biweekly")
                  if START <= d <= END]
    idx = {d: i for i, d in enumerate(cal)}
    paths = []
    for off in range(PATHS):
        shifted = [cal[idx.get(d, 0) + off] for d in base_dates
                   if idx.get(d, 0) + off < len(cal)]
        paths.append([d for d in shifted if START <= d <= END])

    for label, uni in [("A3u_PIT800宇宙", pit_map), ("A3_全面板(旧口径)", None)]:
        anns, rets = [], []
        for dates in paths:
            cfg = BacktestConfig(**{**DEFAULT_CONFIG.to_dict(), **BASE,
                                    "commission": 0.0013})
            nav, info = run_backtest(panel, ap, dates, cfg, ic,
                                     universe_members=uni)
            nav = nav[nav.index >= pd.Timestamp(START)]
            anns.append(float(calc_metrics(nav)["年化_float"]))
            rets.append(nav.pct_change().dropna())
        ens = (1 + pd.concat(rets, axis=1).dropna().mean(axis=1)).cumprod()
        cm = calc_metrics(ens)
        print(f"{label}: 路径均值 {np.mean(anns)*100:+.2f}% "
              f"摊平 {cm['年化_float']*100:+.2f}% 夏普 {cm['夏普_float']:.2f} "
              f"回撤 {cm['回撤_float']*100:.2f}%", flush=True)
        if label.startswith("A3u"):
            a3u_ens, a3u_anns = ens, anns
        else:
            a3_ens, a3_anns = ens, anns

    # vs 官方指数(配对差分夏普, block bootstrap 同主回测登记)
    idx_c = ic[ic.index.isin(a3u_ens.index)]
    a3u_c = a3u_ens[a3u_ens.index.isin(idx_c.index)]
    d = np.log(a3u_c / a3u_c.shift(1)).dropna().values \
        - np.log(idx_c / idx_c.shift(1)).dropna().values
    sh = d.mean() / d.std(ddof=1) * np.sqrt(252) if d.std(ddof=1) > 0 else 0
    from main_backtest_analysis import circ_block_boot, ann_sharpe_of_diff
    boots = [ann_sharpe_of_diff(x) for x in circ_block_boot(d)]
    se = float(np.std(boots))
    sig = sh / se if se > 0 else 0
    ann_u = (a3u_c.iloc[-1]/a3u_c.iloc[0])**(252/len(a3u_c)) - 1
    ann_i = (idx_c.iloc[-1]/idx_c.iloc[0])**(252/len(idx_c)) - 1
    print(f"\nPIT-800 宇宙下: 差分夏普 {sh:+.2f} ± {se:.2f}(SE_boot) "
          f"→ σ={sig:+.2f} → {'过线' if abs(sig) >= 0.35 else '与0不可分辨'}")
    print(f"年化: A3u {ann_u*100:+.2f}% vs 官方指数 {ann_i*100:+.2f}% "
          f"→ Δ {100*(ann_u-ann_i):+.2f}pp")
    print(f"宇宙效应(全面板→PIT800): {100*(a3u_ens.iloc[-1]/a3_ens.iloc[0])**(252/len(a3u_ens))-100*a3_anns[0]/100:.1f}% 注: 见下方说明")
    print(f"A3u路径均值 {np.mean(a3u_anns)*100:+.2f}% vs A3 {np.mean(a3_anns)*100:+.2f}% "
          f"(宇宙限制的路径均值差 {100*(np.mean(a3u_anns)-np.mean(a3_anns)):+.2f}pp)")


if __name__ == "__main__":
    main()
