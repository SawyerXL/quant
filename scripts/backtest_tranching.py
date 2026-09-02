"""
Tranching 验证（2026-09-01，时点运气检验的下一步）。

背景: 调仓锚点平移0~9日, 年化2.47%~19.11%, 均值11.07% —— 固定调仓日是抽签。
tranching: 资金分N组, 各组调仓日错开, 收益收敛到所有路径均值。
判读: 分组后收益接近均值(≈11%)且回撤不劣于基线(或更好) → 采纳部署;
     分组后反而更差 → 关闭。
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
from backtest_config import DEFAULT_CONFIG
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
    print(f"Panel: {len(prices)}只", flush=True)

    ic = load_meta("csi800_index")
    ic = ic.set_index("date")["close"].sort_index()
    ic.index = pd.to_datetime(ic.index)
    sh = load_daily("000001", "2014-06-01", END)
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))
    base = [d for d in make_rebal_dates(cal, "biweekly") if START <= d <= END]
    idx = {d: i for i, d in enumerate(cal)}

    def path(off):
        shifted = []
        for d in base:
            i = idx.get(d, 0) + off
            if i < len(cal):
                shifted.append(cal[i])
        return [d for d in shifted if START <= d <= END]

    def tranche(n_groups):
        rets = []
        for g in range(n_groups):
            off = int(g * 10 / n_groups)
            nav, _ = run_backtest(panel, ap, path(off), DEFAULT_CONFIG, ic)
            rets.append(nav.pct_change().dropna())
        j = pd.concat(rets, axis=1).dropna()
        return (1 + j.mean(axis=1)).cumprod()

    print(f"{'分组':<8}{'年化':>9}{'夏普':>8}{'回撤':>9}", flush=True)
    for n in (1, 2, 3, 5):
        if n == 1:
            nav, _ = run_backtest(panel, ap, path(0), DEFAULT_CONFIG, ic)
        else:
            nav = tranche(n)
        cm = calc_metrics(nav)
        ar = float(str(cm["年化收益率"]).strip("%"))
        sr = float(cm["夏普比率"])
        dd = float(str(cm["最大回撤"]).strip("%"))
        print(f"{n}组     {ar:>+8.2f}%{sr:>8.2f}{dd:>8.2f}%", flush=True)
    print("对照: 10路径均值+11.07% / 现网单组+6.92%", flush=True)


if __name__ == "__main__":
    main()
