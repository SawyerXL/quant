"""美股腿 Part B（部署口径）：股票腿按 100 万划拨后每组分资金重跑引擎。

部署口径（用户定案）：CB 固定 60 万；美股腿从股票腿 100 万划拨。
  u=0    → 股票 100 万 → 50 万/组
  u=10%  → 股票 84 万  → 42 万/组
  u=12.5%→ 股票 80 万  → 40 万/组
  u=15%  → 股票 76 万  → 38 万/组
输出每日 NAV 供组合层（CB+US）复算；主窗 2019-01~2026-10-19；实盘日程。
用法：python scripts/us_leg_partb_deployment.py
"""
import json
import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from data.storage import load_daily, load_meta
from backtest_config import DEFAULT_CONFIG
from backtest_engine import run_backtest, make_rebal_dates, calc_metrics

END = "2026-10-19"
START = "2019-01-01"
CAPS = {0.0: 500_000.0, 0.10: 420_000.0, 0.125: 400_000.0, 0.15: 380_000.0}
OUT = Path("logs/us_leg_partb_deployment_20261020.json")


def main():
    sh = load_daily("SH000001", "2014-06-01", END)
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))
    rebal = [d for d in make_rebal_dates(cal, "biweekly") if START <= d <= END]
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
    idx = load_meta("csi800_index")
    idx_c = idx.set_index("date")["close"].sort_index()
    idx_c.index = pd.to_datetime(idx_c.index)
    print(f"panel {panel.shape[0]}天×{panel.shape[1]}只, rebal {len(rebal)}")

    out = {"as_of": END, "funding": "deployment: CB 60w fixed, US carved from stock",
           "arms": {}}
    cfg0 = replace(DEFAULT_CONFIG, lot_size=0, initial_capital=500_000.0)
    nav0, m0 = run_backtest(panel, ap, rebal, cfg0, idx_c)
    nav0.rename("nav").to_csv("logs/us_leg_partb_nav_uncon.csv")
    cm = calc_metrics(nav0)
    out["arms"]["uncon"] = {"ann": cm["年化收益率"], "sharpe": cm["夏普比率"],
                            "dd": cm["最大回撤"], "skips": 0}
    print("uncon", out["arms"]["uncon"], flush=True)
    for u, cap in CAPS.items():
        cfg = replace(DEFAULT_CONFIG, lot_size=100, initial_capital=cap)
        nav, m = run_backtest(panel, ap, rebal, cfg, idx_c)
        nav.rename("nav").to_csv(f"logs/us_leg_partb_nav_u{int(u*1000)}.csv")
        cm = calc_metrics(nav)
        sk = m.get("lot_skips", m.get("skips", 0))
        key = f"u{int(u*1000)}"
        out["arms"][key] = {"cap_per_group": cap, "stock_total": cap * 2,
                            "ann": cm["年化收益率"], "sharpe": cm["夏普比率"],
                            "dd": cm["最大回撤"], "skips": sk}
        print(key, out["arms"][key], flush=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    print("->", OUT)


if __name__ == "__main__":
    main()
