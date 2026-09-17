"""§9 择时臂级执行假设对照 (2026-09-16 晚)。

全量 A3 完整栈 Δ=+3.96−+4.03=−0.07pp 落第一档；但 §9 锁的对象=
择时效应(A2c−A1c)的两种执行差，本脚本按路径配对出该 Δ 收口。
Δ[p] = (A2c−A1c)_close[p] − (A2c−A1c)_t1_open[p]，均值+逐路径。
判读矩阵（锁于 2026-09-13 出数前）：<1.0pp 可接受 / 1.0~2.5pp
依赖执行 / >2.5pp 非可实现 alpha。
用法: timeout 120m ionice -c 3 nice -n 10 .venv/bin/python \
      scripts/smoke_t1_open_timing.py 10 > logs/t1_open_timing_20260916.log 2>&1
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd
from loguru import logger
logger.remove()

from smoke_t1_open import build_panels, START, END
from backtest_config import BacktestConfig, DEFAULT_CONFIG
from backtest_engine import run_backtest, make_rebal_dates, calc_metrics
from backtest_return_attribution import BASE, A0_OV
from s1_attribution_clean import COMPONENTS_OFF

PATHS = int(sys.argv[1]) if len(sys.argv) > 1 else 10

# 与 s1_attribution_clean 同构的两臂（A1c 纯池 / A2c +五档）
ARMS = [
    ("A1c_pool30买持纯", {**A0_OV, **COMPONENTS_OFF,
                         "pool_style": "amount", "pool_size": 30,
                         "initial_capital": 500_000.0}, False),
    ("A2c_pool30+五档纯", {**A0_OV, **COMPONENTS_OFF,
                          "pool_style": "amount", "pool_size": 30,
                          "initial_capital": 500_000.0}, True),
]


def main():
    panel, open_panel, ap, cal = build_panels()
    from data.storage import load_meta
    ic = load_meta("csi800_index").set_index("date")["close"].sort_index()
    ic.index = pd.to_datetime(ic.index)
    base_dates = [d for d in make_rebal_dates(cal, "biweekly")
                  if START <= d <= END]
    idx = {d: i for i, d in enumerate(cal)}
    paths = []
    for off in range(PATHS):
        shifted = [cal[idx.get(d, 0) + off] for d in base_dates
                   if idx.get(d, 0) + off < len(cal)]
        paths.append([d for d in shifted if START <= d <= END])

    # 每臂×每执行×每路径 → 年化
    res = {}
    for arm, ov, with_timing in ARMS:
        for mode, cfg_ov in [("close", {}), ("t1_open", {"exec_t1_open": True})]:
            anns = []
            for dates in paths:
                cfg = BacktestConfig(**{**DEFAULT_CONFIG.to_dict(), **BASE,
                                        **ov, **cfg_ov, "commission": 0.0013})
                nav, info = run_backtest(panel, ap, dates, cfg,
                                         ic if with_timing else None,
                                         open_panel=open_panel)
                nav = nav[nav.index >= pd.Timestamp(START)]
                anns.append(float(calc_metrics(nav)["年化_float"]))
            res[(arm, mode)] = anns
            print(f"{arm} {mode}: 路径均值 {np.mean(anns)*100:+.2f}% "
                  f"(各路径: {[f'{a*100:+.2f}' for a in anns]})", flush=True)

    # §9 配对 Δ：每路径 (A2c−A1c) 的两种执行差
    d_close = [a2 - a1 for a2, a1 in zip(res[("A2c_pool30+五档纯", "close")],
                                         res[("A1c_pool30买持纯", "close")])]
    d_open = [a2 - a1 for a2, a1 in zip(res[("A2c_pool30+五档纯", "t1_open")],
                                        res[("A1c_pool30买持纯", "t1_open")])]
    deltas = [c - o for c, o in zip(d_close, d_open)]
    mean_d = float(np.mean(deltas))
    print(f"择时效应 close: {np.mean(d_close)*100:+.2f}% | t1_open: "
          f"{np.mean(d_open)*100:+.2f}%", flush=True)
    print(f"§9 Δ = close择时 − t1_open择时 = {mean_d*100:+.2f}pp "
          f"(路径配对, 各路径: {[f'{d*100:+.2f}' for d in deltas]})", flush=True)
    band = ("第一档 <1.0pp: 执行乐观度可接受" if abs(mean_d) < 0.01
            else "1.0~2.5pp: 依赖执行" if abs(mean_d) < 0.025
            else ">2.5pp: 不构成可实现 alpha")
    print(f"判读: {band}", flush=True)


if __name__ == "__main__":
    main()
