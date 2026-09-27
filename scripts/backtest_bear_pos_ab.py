"""熊档仓位语义 A/B (2026-09-27, P1二销案后用户点名"bear档34万闲置"的量化前置)。

回测 40/60 的股票腿=五档全程在跑(熊档 30%×pool30); 实盘 trader._execute_bear
= 熊档清仓 0。本 A/B 量化两语义的差: 引擎同一 config, 只改 ma200_bear_pos
(0.30=回测假设 / 0=实盘语义), 输出 年化/夏普/回撤——差值=现网偏离回测画像的量。
"""
import sys
from pathlib import Path
from dataclasses import replace
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
from loguru import logger
logger.remove()

from data.storage import load_daily, load_meta
from backtest_config import DEFAULT_CONFIG
from backtest_engine import run_backtest, make_rebal_dates, calc_metrics

END_20260924 = "2026-09-24"   # 工件 as_of 冻结(闸门十三)
START = "2019-01-01"


def main():
    sh = load_daily("SH000001", "2014-06-01", END_20260924)
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))
    rebal = [d for d in make_rebal_dates(cal, "biweekly")
             if START <= d <= END_20260924]

    meta = load_meta("stock_info_full")
    codes = meta["code"].tolist() if not meta.empty else []
    prices, amounts = {}, {}
    for code in codes:
        try:
            d = load_daily(code, START, END_20260924)
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
    idx = load_meta("csi800_index")
    idx_c = idx.set_index("date")["close"].sort_index()
    idx_c.index = pd.to_datetime(idx_c.index)
    print(f"面板 {panel.shape[0]}天×{panel.shape[1]}只")

    print(f"\n{'臂':<30}{'年化':>9}{'夏普':>7}{'回撤':>9}")
    print("─" * 60)
    for bear_pos, label in [(None, "基线(引擎规则: ≤0.3清仓)"),
                            (0.0, "bear_pos=0(显式清仓)"),
                            (0.5, "bear_pos=0.5(熊档持半仓替代案)")]:
        cfg = replace(DEFAULT_CONFIG, lot_size=100,
                      initial_capital=500_000.0, ma200_bear_pos=bear_pos)
        nav, m = run_backtest(panel, ap, rebal, cfg, idx_c)
        cm = calc_metrics(nav)
        ar = float(str(cm["年化收益率"]).strip("%"))
        sr = float(cm["夏普比率"])
        dd = float(str(cm["最大回撤"]).strip("%"))
        print(f"{label:<30}{ar:>+8.2f}%{sr:>8.2f}{dd:>9.2f}%")
    print(f"\nas_of={END_20260924}, pool30, 双周调仓, lot 50万/组, shift=-0.03")


if __name__ == "__main__":
    main()
