"""
择时集成 A/B（2026-09-05，专家评审立项 #24，事前登记在本文件顶部）。

设计: 不改五档结构, 改输入——MA{150,200,250}三条比值:
  mean = 三比值均值+shift 喂状态机; vote = 三票各投一档取中位档(绕过确认态机)。
事前登记(专家):
  目标 = 路径极差7.26pp再压缩 + ±0.02扰动离散度收窄(抗噪轮标准原样)
  预测 = 收益持平或-0.3pp, 极差收窄20~30%
  红旗 = 收益大涨 → 按事件归因流程审查
口径: pool30×50万lot×降档3%(部署口径); 全期2019-2026.8。
指标: 10路径年化极差/均值; path0的±0.02扰动离散度; 年化/夏普/回撤。
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
BASE = {"pool_size": 30, "lot_size": 100, "initial_capital": 500_000.0}
VARIANTS = [
    ("基线MA200", {}),
    ("集成mean{150,200,250}", {"ma_ensemble": (150, 200, 250),
                               "ma_ensemble_mode": "mean"}),
    ("集成vote{150,200,250}", {"ma_ensemble": (150, 200, 250),
                               "ma_ensemble_mode": "vote"}),
]


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
            amt = pd.to_numeric(d.get("amount", pd.Series(dtype=float)),
                                errors="coerce")
            if len(cl) >= 250:
                prices[code] = cl
                if len(amt) >= 250:
                    amounts[code] = amt
        except Exception:
            pass
    panel = pd.DataFrame(prices).sort_index()
    ap = pd.DataFrame(amounts).sort_index()
    ic = load_meta("csi800_index").set_index("date")["close"].sort_index()
    ic.index = pd.to_datetime(ic.index)
    sh = load_daily("000001", "2014-06-01", END)
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))
    base = [d for d in make_rebal_dates(cal, "biweekly") if START <= d <= END]
    idx = {d: i for i, d in enumerate(cal)}

    def path(off):
        shifted = [cal[idx.get(d, 0) + off] for d in base
                   if idx.get(d, 0) + off < len(cal)]
        return [d for d in shifted if START <= d <= END]

    for name, ov in VARIANTS:
        anns = []
        for off in range(10):
            cfg = BacktestConfig(**{**DEFAULT_CONFIG.to_dict(), **BASE, **ov})
            nav, _ = run_backtest(panel, ap, path(off), cfg, ic)
            anns.append(float(calc_metrics(nav)["年化_float"]))
        spread = (max(anns) - min(anns)) * 100
        # path0 ±0.02 扰动离散度(抗噪标准)
        pert = []
        for delta in (-0.02, 0.0, 0.02):
            cfg = BacktestConfig(**{**DEFAULT_CONFIG.to_dict(), **BASE, **ov,
                                    "ma200_thresh_shift":
                                    DEFAULT_CONFIG.ma200_thresh_shift + delta})
            nav, _ = run_backtest(panel, ap, path(0), cfg, ic)
            pert.append(float(calc_metrics(nav)["年化_float"]))
        p_spread = (max(pert) - min(pert)) * 100
        cfg0 = BacktestConfig(**{**DEFAULT_CONFIG.to_dict(), **BASE, **ov})
        nav0, _ = run_backtest(panel, ap, path(0), cfg0, ic)
        cm = calc_metrics(nav0)
        print(f"{name:<22}: 路径均值{np.mean(anns)*100:+.2f}% "
              f"极差{spread:.2f}pp | path0年化{cm['年化_float']*100:+.2f}% "
              f"夏普{cm['夏普_float']:.2f} 回撤{cm['回撤_float']*100:.2f}% "
              f"| ±0.02扰动离散度{p_spread:.2f}pp", flush=True)


if __name__ == "__main__":
    main()
