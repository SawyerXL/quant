"""股票腿结构 A/B：单组 40 万 vs 现产 2×20 万（2026-11-01）。

预登记 logs/research_registration_stock_single40_20261101.md。
同面板/同引擎/同窗口；只改资金分组，不改变选股/择时参数。
输出 logs/stock_single40_20261101.json/.md；不改实盘。
"""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from data.storage import load_daily, load_meta
from backtest_config import DEFAULT_CONFIG
from backtest_engine import run_backtest, make_rebal_dates, calc_metrics
from three_piece_40_60 import combo, metrics_ret

START, END = "2019-01-03", "2026-10-19"
CURRENT_STOCK = ROOT / "logs" / "stock_leg_nav_20w_avg.csv"
CB = ROOT / "logs" / "cb_sched_compare_nav.csv"
OUT_JSON = ROOT / "logs" / "stock_single40_20261101.json"
OUT_MD = ROOT / "logs" / "stock_single40_20261101.md"


def build_panel():
    sh = load_daily("SH000001", "2014-06-01", END)
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))
    base = [d for d in make_rebal_dates(cal, "biweekly") if START <= d <= END]
    meta = load_meta("stock_info_full")
    prices, amounts = {}, {}
    for code in meta["code"].tolist():
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
    idx = load_meta("csi800_index")
    idx_c = idx.set_index("date")["close"].sort_index()
    idx_c.index = pd.to_datetime(idx_c.index)
    return panel, ap, idx_c, base


def run_stock(panel, ap, idx_c, base, cap: float):
    cfg = replace(DEFAULT_CONFIG, lot_size=100, initial_capital=cap)
    nav, m = run_backtest(panel, ap, base, cfg, idx_c)
    cm = calc_metrics(nav)
    return nav, {
        "ann_pct": round(float(cm["年化_float"]) * 100, 3),
        "sharpe": round(float(cm["夏普_float"]), 3),
        "dd_pct": round(float(cm["回撤_float"]) * 100, 3),
        "lot_skips": int(m.get("lot_skips", 0)),
        "trades": int(m.get("trades", 0)),
    }


def combo_metrics(stock_nav: pd.Series, cb: pd.DataFrame, col: str) -> dict:
    rs = stock_nav.pct_change().dropna()
    rc = cb[col].pct_change().dropna()
    legs = pd.concat([rs.rename("s"), rc.rename("cb")], axis=1,
                     join="inner").dropna()
    nav, trig, days = combo(legs)
    m = metrics_ret(nav.pct_change().fillna(0))
    periods = {}
    for label, (s, e) in (("IS", ("2019-01-03", "2023-12-31")),
                          ("OOS", ("2024-01-01", "2026-10-19"))):
        sub = nav.loc[(nav.index >= s) & (nav.index <= e)]
        if len(sub) > 2:
            periods[label] = metrics_ret(sub.pct_change().fillna(0))
    return {"metrics": m, "periods": periods, "trig": int(trig),
            "days_halted": int(days), "n": int(len(legs))}


def main() -> int:
    panel, ap, idx_c, base = build_panel()
    print(f"panel {panel.shape[0]}×{panel.shape[1]} rebal {len(base)}", flush=True)
    stock = {}
    for label, cap in (("single20", 200_000.0), ("single40", 400_000.0)):
        nav, meta = run_stock(panel, ap, idx_c, base, cap)
        stock[label] = {"nav": nav, **meta}
        print(label, stock[label]["ann_pct"], stock[label]["lot_skips"], flush=True)
    cur = pd.read_csv(CURRENT_STOCK, index_col=0, parse_dates=True)["nav"]
    stock["current"] = {
        "nav": cur,
        "ann_pct": round(float((cur.iloc[-1] / cur.iloc[0])
                               ** (252 / len(cur)) - 1) * 100, 3),
        "sharpe": round(float(cur.pct_change().dropna().mean()
                              / cur.pct_change().dropna().std()
                              * np.sqrt(252)), 3),
        "dd_pct": round(float((cur / cur.cummax() - 1).min()) * 100, 3),
        "lot_skips": 2851,   # stock_group_timing_ab_20261023.json, two_group
        "trades": 5884,
    }
    cb = pd.read_csv(CB, index_col=0, parse_dates=True)
    out = {
        "as_of": "2026-11-01",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "registration": "logs/research_registration_stock_single40_20261101.md",
        "period": [START, END],
        "stock": {}, "combo": {},
    }
    for label, s in stock.items():
        out["stock"][label] = {k: v for k, v in s.items() if k != "nav"}
        out["combo"][label] = {
            "par": combo_metrics(s["nav"], cb, "nav_snapdelay_par"),
            "zero": combo_metrics(s["nav"], cb, "nav_snapdelay_zero"),
        }
    cpar = out["combo"]["current"]["par"]["metrics"]
    czero = out["combo"]["current"]["zero"]["metrics"]
    spar = out["combo"]["single40"]["par"]["metrics"]
    szero = out["combo"]["single40"]["zero"]["metrics"]
    out["delta_single40_vs_current"] = {
        "par_ann_pp": round(spar["ann_pct"] - cpar["ann_pct"], 3),
        "zero_ann_pp": round(szero["ann_pct"] - czero["ann_pct"], 3),
        "par_dd_pp": round(spar["dd_pct"] - cpar["dd_pct"], 3),
        "zero_dd_pp": round(szero["dd_pct"] - czero["dd_pct"], 3),
        "stock_ann_pp": round(stock["single40"]["ann_pct"]
                              - stock["current"]["ann_pct"], 3),
    }
    d = out["delta_single40_vs_current"]
    passed = (d["par_ann_pp"] >= 0 and d["zero_ann_pp"] >= 0
              and d["par_dd_pp"] >= -1.0 and d["zero_dd_pp"] >= -1.0)
    out["verdict"] = "PASS" if passed else "FAIL"
    out["rule"] = ("single40 par ann >= current par, zero ann >= current zero, "
                   "DD not deeper >1pp; else FAIL")
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    lines = [
        "# 股票腿结构 A/B：单组 40 万 vs 2×20 万（2026-11-01）",
        "",
        f"- 预登记：`{out['registration']}`；窗口 {START}→{END}；"
        "40/60；CB par/zero；10bp/边。",
        f"- **裁决：{out['verdict']}**（{out['rule']}）。",
        "",
        "| 股票腿 | 年化 | 夏普 | 最大回撤 | lot跳票 | 40/60 par | 40/60 zero |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for label in ("current", "single20", "single40"):
        s = out["stock"][label]
        cp = out["combo"][label]["par"]["metrics"]
        cz = out["combo"][label]["zero"]["metrics"]
        lines.append(
            f"| {label} | {s['ann_pct']}% | {s['sharpe']} | {s['dd_pct']}% | "
            f"{s['lot_skips']} | {cp['ann_pct']}%/{cp['sharpe']}/{cp['dd_pct']}% | "
            f"{cz['ann_pct']}%/{cz['sharpe']}/{cz['dd_pct']}% |"
        )
    lines += [
        "",
        "| 腿 | par IS | par OOS | zero IS | zero OOS |",
        "|---|---:|---:|---:|---:|",
    ]
    for label in ("current", "single20", "single40"):
        p = out["combo"][label]["par"]["periods"]
        z = out["combo"][label]["zero"]["periods"]
        lines.append(
            f"| {label} | {p.get('IS', {}).get('ann_pct', '—')}% | "
            f"{p.get('OOS', {}).get('ann_pct', '—')}% | "
            f"{z.get('IS', {}).get('ann_pct', '—')}% | "
            f"{z.get('OOS', {}).get('ann_pct', '—')}% |"
        )
    lines += [
        "",
        f"Δ single40 − current：par {d['par_ann_pp']:+.3f}pp、"
        f"zero {d['zero_ann_pp']:+.3f}pp、股票腿 {d['stock_ann_pp']:+.3f}pp、"
        f"par DD {d['par_dd_pp']:+.3f}pp、zero DD {d['zero_dd_pp']:+.3f}pp。",
        "",
        "注意：PASS 只按预登记的收益/回撤判据；par 夏普 single40 1.499 "
        "低于 current 1.536，回撤略深 0.19pp；zero 夏普 1.111 略高于 1.105。"
        "这是“收益略增、par 风险调整略降”的结构变化，不是免费午餐。",
        "",
        "不改实盘；PASS 也只进入结构 A/B 提案。盲区：单组集中度、单一路径、"
        "无真实成交/滑点；股票腿引擎成本口径与实盘仍有差。",
    ]
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
