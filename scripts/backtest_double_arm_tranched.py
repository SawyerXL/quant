"""
双arm摊平复验（2026-09-01，时点运气判读的下一步——第五道闸门）。

why: 极差16.64pp说明单时点A/B的差值<1σ(5.38pp)无法与抽签区分。
     本脚本把待复验的A/B放在10路径摊平口径下重比:
       arm1: vol20≤5%  × pool60  (现网配置)
       arm2: vol20关闭  × pool60  (拥挤度过滤off)
       arm3: vol20≤5%  × pool30  (池子缩小)
     每arm跑10条偏移路径, 保存日收益序列, 之后所有组合分析离线做。
判读: 双arm摊平后差值仍>~2pp且跨路径同向 → 真; 摊平后消失 → 抽签。
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

START, END = "2019-01-01", "2026-08-28"
OUT = Path(__file__).parent.parent / "logs" / "tranched_arms"


def main():
    OUT.mkdir(exist_ok=True)
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
    print(f"Panel: {len(prices)}只, {panel.shape[0]}天", flush=True)

    ic = load_meta("csi800_index")
    ic = ic.set_index("date")["close"].sort_index()
    ic.index = pd.to_datetime(ic.index)
    sh = load_daily("000001", "2014-06-01", END)
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))
    base = [d for d in make_rebal_dates(cal, "biweekly") if START <= d <= END]
    idx = {d: i for i, d in enumerate(cal)}

    def path(off):
        shifted = [cal[idx.get(d, 0) + off] for d in base
                   if idx.get(d, 0) + off < len(cal)]
        return [d for d in shifted if START <= d <= END]

    arms = {
        "vol5_p60": DEFAULT_CONFIG,
        "vol999_p60": replace(DEFAULT_CONFIG, max_vol20=999.0),
        "vol5_p30": replace(DEFAULT_CONFIG, pool_size=30),
    }

    for name, cfg in arms.items():
        print(f"\n=== arm {name} ===", flush=True)
        for off in range(10):
            nav, _ = run_backtest(panel, ap, path(off), cfg, ic)
            rets = nav.pct_change().dropna()
            rets.to_frame("r").to_parquet(OUT / f"{name}_off{off}.parquet")
            cm = calc_metrics(nav)
            print(f"  off{off}: 年化{cm['年化_float']*100:+.2f}% "
                  f"夏普{cm['夏普_float']:.2f} 回撤{cm['回撤_float']*100:+.1f}% "
                  f"末NAV{nav.iloc[-1]:.3f}", flush=True)


if __name__ == "__main__":
    main()
