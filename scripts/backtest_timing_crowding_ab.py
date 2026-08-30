"""
择时降仓强度 × 拥挤度过滤 A/B（2026-08-30, 针对8/19型失血）。

背景: QMT 8/17峰值+7.05万 → 8/28 +3.78万, 8/19单日-3.1%(指数-0.24%)。
归因: 成交额TOP池=动量拥挤, 9只持仓跌停。两个候选解法:
  A. MA200择时更狠(timing_scale<1): 熊市档0.30/0.50再打折 → 8/19损失减半
  B. 拥挤度过滤(max_vol20): 剔除20日波动率超标的热门票
窗口: 全期2019-2026.7 + 2022-2026.8(近段, 含8月)
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
from loguru import logger
logger.remove()

from data.storage import load_daily, load_meta
from backtest_config import BacktestConfig, DEFAULT_CONFIG
from backtest_engine import run_backtest, make_rebal_dates, calc_metrics

START, END = "2019-01-01", "2026-08-28"
WINDOWS = [("全期2019-2026.8", "2019-01-01", "2026-08-28"),
           ("近段2022-2026.8", "2022-01-01", "2026-08-28"),
           ("近段2024.7-2026.8", "2024-07-01", "2026-08-28")]


def variant(name, **kw):
    return (name, BacktestConfig(**{**DEFAULT_CONFIG.to_dict(), **kw}))


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
    print(f"Panel: {len(prices)}只, {panel.shape[0]}天")

    idx = load_meta("csi800_index")
    idx_c = None
    if not idx.empty:
        idx_c = idx.set_index("date")["close"].sort_index()
        idx_c.index = pd.to_datetime(idx_c.index)

    cal = sorted(load_meta("trade_calendar")["trade_date"].astype(str).tolist())

    variants = [
        variant("基线(现网)", ),
        variant("择时×0.7", timing_scale=0.7),
        variant("择时×0.5", timing_scale=0.5),
        variant("波动率过滤6%", max_vol20=6.0),
        variant("波动率过滤5%", max_vol20=5.0),
        variant("择时×0.7+波动6%", timing_scale=0.7, max_vol20=6.0),
    ]

    for wname, lo, hi in WINDOWS:
        p = panel[(panel.index >= lo) & (panel.index <= hi)]
        a = ap[(ap.index >= lo) & (ap.index <= hi)]
        rebal = [d for d in make_rebal_dates(cal, "biweekly") if lo <= d <= hi]
        print(f"\n{'='*72}\n窗口 {wname}\n{'='*72}")
        print(f"{'配置':<22}{'年化':>9}{'夏普':>8}{'最大回撤':>10}")
        for name, cfg in variants:
            nav, _ = run_backtest(p, a, rebal, cfg, idx_c)
            cm = calc_metrics(nav)
            ar = float(str(cm["年化收益率"]).strip("%"))
            sr = float(cm["夏普比率"])
            dd = float(str(cm["最大回撤"]).strip("%"))
            print(f"{name:<22}{ar:>+8.2f}%{sr:>8.2f}{dd:>9.2f}%")


if __name__ == "__main__":
    main()
