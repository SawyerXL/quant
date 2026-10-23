"""融资杠杆 A/B（2026-10-23，用户提出融资融券）。

只回答一个问题：股票腿（A3，20万/组，40万）加融资杠杆 L 后，
组合 40/60 的年化/夏普/回撤是否改善、是否破 15% 线。

口径（锁）：
- 标的收益 = 股票腿日收益（A3 20万/组，引擎原始口径）；融资成本 =
  (L-1) × 年利率/252，按日计提；不做融券（A股券源稀缺，另案）。
- 组合 = 0.4×杠杆股票腿 + 0.6×CB（nav_snapdelay_par），月度回 40/60。
- 窗口 2019-01-03~2026-10-19；输出含 15% 熔断敏感性。
- 这是融资成本的算术叠加，不是保证；强平线/维持担保比例未建模（盲区）。
"""
import json
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from data.storage import load_daily, load_meta
from backtest_config import DEFAULT_CONFIG
from backtest_engine import run_backtest, make_rebal_dates

START, END = "2019-01-01", "2026-10-19"
LEVS = [1.0, 1.2, 1.5, 2.0]
RATES = [0.065, 0.080]
OUT = Path("logs/margin_leverage_ab_20261023.json")


def metrics(nav: pd.Series) -> dict:
    r = nav.pct_change().dropna()
    y = len(r) / 252
    ann = nav.iloc[-1] ** (1 / y) - 1
    sharpe = r.mean() / r.std() * np.sqrt(252) if r.std() > 0 else float("nan")
    dd = (nav / nav.cummax() - 1).min()
    return {"ann": round(float(ann) * 100, 2), "sharpe": round(float(sharpe), 3),
            "dd": round(float(dd) * 100, 2)}


def main() -> int:
    sh = load_daily("SH000001", "2014-06-01", END)
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))
    rebal = [d for d in make_rebal_dates(cal, "biweekly") if START <= d <= END]
    meta = load_meta("stock_info_full")
    codes = meta["code"].tolist()
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
    ic = idx.set_index("date")["close"].sort_index()
    ic.index = pd.to_datetime(ic.index)

    cfg = replace(DEFAULT_CONFIG, lot_size=100, initial_capital=200_000.0)
    nav_s, _ = run_backtest(panel, ap, rebal, cfg, ic)
    r_s = nav_s.pct_change().dropna()
    ncb = pd.read_csv("logs/cb_sched_compare_nav.csv", index_col=0, parse_dates=True)
    r_cb = ncb["nav_snapdelay_par"].pct_change().dropna()
    legs = pd.concat([r_s.rename("s"), r_cb.rename("cb")], axis=1,
                     join="inner").dropna()
    print(f"legs {legs.index.min().date()}~{legs.index.max().date()} n={len(legs)}", flush=True)

    out = {"as_of": END, "stock": metrics(nav_s), "results": {}}
    for rate in RATES:
        for L in LEVS:
            r_l = L * legs["s"] - (L - 1) * rate / 252
            # 40/60 月度回目标权重
            nav, w = 1.0, np.array([0.4, 0.6])
            navs = []
            last_m = None
            for dt, row in legs.iterrows():
                ym = (dt.year, dt.month)
                if ym != last_m:
                    last_m = ym
                    w = np.array([0.4, 0.6])
                vals = np.array([r_l.loc[dt], row["cb"]], float)
                nav *= (1 + float(np.dot(w, vals)))
                navs.append(nav)
                w = w * (1 + vals)
                w = w / w.sum()
            s = pd.Series(navs, index=legs.index)
            out["results"][f"L{L}_r{rate:.3f}"] = metrics(s)
            print(f"L={L} rate={rate:.1%} {out['results'][f'L{L}_r{rate:.3f}']}", flush=True)

    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("->", OUT, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
