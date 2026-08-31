"""
目标波动率控仓 A/B（2026-08-31，判读矩阵指定出口，唯一排队的研究项）。

三根桩（事前登记）:
  ① 成功标准: 非收益赢五档, 是"目标vol在网格内平台+对估计窗口不敏感"——范式简化即胜利
  ② 已知风险: 急跌顺周期(波动率飙升→降仓→低位轻仓)可能踏空V反, 仓位下限30%保留
  ③ 对照组: 五档基线 + 五档更早降档版(thresh-0.02), 要赢的是修补后的五档
四道闸门(专家2026-08-31): ±扰动离散度收窄 + 目标vol网格平台 + 归因不集中于≤2事件 + OOS复现

变体: 目标vol∈{10,12,15}% × 窗口{20,60} = 6组(非全网格, 下限固定30%);
对照: 五档基线 / 五档更早降档。
必测场景: 2024-09-24前后20日仓位轨迹单独输出(顺周期踏空V反最可能翻车点)。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd
from loguru import logger
logger.remove()

from data.storage import load_daily, load_meta
from backtest_config import BacktestConfig, DEFAULT_CONFIG
from backtest_engine import run_backtest, make_rebal_dates, calc_metrics

START, END = "2019-01-01", "2026-08-28"


def main():
    meta = load_meta("stock_info_full")
    codes = meta["code"].tolist() if not meta.empty else []
    prices, amounts = {}, {}
    for code in codes:
        try:
            d = load_daily(code, START, END)
            if d.empty:
                continue
            d["date"] = pd.to_datetime(d["date"])
            d = d.set_index("date").sort_index()
            cl = pd.to_numeric(d["close"], errors="coerce").dropna()
            amt = pd.to_numeric(d.get("amount", pd.Series(dtype=float)), errors="coerce")
            if len(cl) >= 250:
                prices[code] = cl
                if len(amt) >= 250:
                    amounts[code] = amt
        except Exception:
            pass
    panel = pd.DataFrame(prices).sort_index()
    ap = pd.DataFrame(amounts).sort_index()
    print(f"Panel: {len(prices)}只, {panel.shape[0]}天", flush=True)

    ic = load_meta("csi800_index")
    ic = ic.set_index("date")["close"].sort_index()
    ic.index = pd.to_datetime(ic.index)

    sh = load_daily("000001", "2014-06-01", END)
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))

    variants = [
        ("五档基线(对照)", {}),
        ("五档更早降档(对照)", {"ma200_thresh_shift": -0.02}),
        ("vol10%×20d", {"vol_target": 0.10, "vol_window": 20}),
        ("vol12%×20d", {"vol_target": 0.12, "vol_window": 20}),
        ("vol15%×20d", {"vol_target": 0.15, "vol_window": 20}),
        ("vol10%×60d", {"vol_target": 0.10, "vol_window": 60}),
        ("vol12%×60d", {"vol_target": 0.12, "vol_window": 60}),
        ("vol15%×60d", {"vol_target": 0.15, "vol_window": 60}),
    ]

    for wname, lo, hi in [("全期2019-2026.8", START, END), ("近段2022-2026.8", "2022-01-01", END)]:
        p = panel[(panel.index >= lo) & (panel.index <= hi)]
        a = ap[(ap.index >= lo) & (ap.index <= hi)]
        rebal = [d for d in make_rebal_dates(cal, "biweekly") if lo <= d <= hi]
        print(f"\n=== {wname} ===", flush=True)
        print(f"{'配置':<18}{'年化':>9}{'夏普':>8}{'回撤':>9}{'年成本损耗':>10}", flush=True)
        for name, kw in variants:
            cfg = BacktestConfig(**{**DEFAULT_CONFIG.to_dict(), **kw})
            nav, m = run_backtest(p, a, rebal, cfg, ic)
            cm = calc_metrics(nav)
            ar = float(str(cm["年化收益率"]).strip("%"))
            sr = float(cm["夏普比率"])
            dd = float(str(cm["最大回撤"]).strip("%"))
            print(f"{name:<18}{ar:>+8.2f}%{sr:>8.2f}{dd:>8.2f}%"
                  f"{m.get('annual_cost_drag', 0) * 100:>9.2f}%", flush=True)


if __name__ == "__main__":
    main()
