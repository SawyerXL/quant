"""
lot约束回测（2026-09-01，拍板1的前置——引擎语义 vs 实盘语义分叉修复）。

why: trader.py实盘用floor-to-lot(100股,688板200股), 买不起一手跳过;
     回测引擎此前按无限可分权重买。pool60×50万/组=8,333元/只, TOP60中位
     价117元, 半数票一手都买不起 —— 无约束回测描述的是物理上建不起来的
     组合。本脚本按实盘语义重跑 pool30/pool60 × 10偏移路径。
口径: 2组摊平形态, 每组50万, lot_size=100, 跳过不重归一(现金拖累)。
判读: ①各池lot约束下收益衰减多少 ②pool60是否退化成"隐性pool25"
     ③约束后30/60差距收窄还是反转。
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
OUT = Path(__file__).parent.parent / "logs" / "lot_arms"


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

    lot_cfg = replace(DEFAULT_CONFIG, lot_size=100, initial_capital=500_000.0)
    arms = {
        "lot_p60": replace(lot_cfg, pool_size=60),
        "lot_p30": replace(lot_cfg, pool_size=30),
    }
    for name, cfg in arms.items():
        print(f"\n=== arm {name} (lot约束, 50万/组) ===", flush=True)
        skips = []
        for off in range(10):
            nav, info = run_backtest(panel, ap, path(off), cfg, ic)
            rets = nav.pct_change().dropna()
            rets.to_frame("r").to_parquet(OUT / f"{name}_off{off}.parquet")
            cm = calc_metrics(nav)
            skips.append(info.get("lot_skips", 0))
            print(f"  off{off}: 年化{cm['年化_float']*100:+.2f}% "
                  f"夏普{cm['夏普_float']:.2f} 回撤{cm['回撤_float']*100:+.1f}% "
                  f"末NAV{nav.iloc[-1]:.3f} 跳票{skips[-1]}次", flush=True)
        print(f"  跳票合计: {sum(skips)} (约{sum(skips)//125}次/调仓, "
              f"共125个调仓日)", flush=True)


if __name__ == "__main__":
    main()
