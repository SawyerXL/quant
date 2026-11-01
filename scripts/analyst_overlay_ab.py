"""分析师“调低”回避 overlay A/B（2026-11-01）。

预登记 logs/research_registration_analyst_overlay_20261101.md。
现产 2×20 万股票腿；调仓日剔除过去 20 个交易日内有“调低”评级的股票；
其余同。40/60 用 CB par/zero；输出 logs/analyst_overlay_20261101.json/.md。
不改实盘。
"""
from __future__ import annotations

import bisect
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
from analyst_rating_event import classify_events, load_ratings

START, END = "2019-01-03", "2026-10-19"
CAP = 200_000.0
LOOKBACK = 20
CB = ROOT / "logs" / "cb_sched_compare_nav.csv"
OUT_JSON = ROOT / "logs" / "analyst_overlay_20261101.json"
OUT_MD = ROOT / "logs" / "analyst_overlay_20261101.md"


def build_exclude_map(cal: list[str], dates: list[str],
                      neg: pd.DataFrame) -> dict[str, set[str]]:
    """调仓日 D 排除事件日 ∈ (D−LOOKBACK 交易日, D) 的股票。"""
    idx = {d: i for i, d in enumerate(cal)}
    by_date: dict[str, set[str]] = {}
    for _, r in neg.iterrows():
        by_date.setdefault(r["date"], set()).add(r["code"])
    out = {}
    for d in dates:
        i = idx.get(d)
        if i is None:
            out[d] = set()
            continue
        lo = cal[max(0, i - LOOKBACK)]
        s: set[str] = set()
        for ed, codes in by_date.items():
            if lo <= ed < d:
                s |= codes
        out[d] = s
    return out


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
    return panel, ap, idx_c, base, cal


def shift_dates(cal: list[str], base: list[str], off: int) -> list[str]:
    idx = {d: i for i, d in enumerate(cal)}
    out = []
    for d in base:
        i = idx.get(d)
        if i is None or i + off >= len(cal):
            continue
        x = cal[i + off]
        if START <= x <= END:
            out.append(x)
    return sorted(set(out))


def run_group(panel, ap, idx_c, dates, exclude_map):
    cfg = replace(DEFAULT_CONFIG, lot_size=100, initial_capital=CAP)
    nav, m = run_backtest(panel, ap, dates, cfg, idx_c,
                          exclude_map=exclude_map)
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
    periods = {}
    for label, (s, e) in (("IS", ("2019-01-03", "2023-12-31")),
                          ("OOS", ("2024-01-01", "2026-10-19"))):
        sub = nav.loc[(nav.index >= s) & (nav.index <= e)]
        if len(sub) > 2:
            periods[label] = metrics_ret(sub.pct_change().fillna(0))
    return {"metrics": metrics_ret(nav.pct_change().fillna(0)),
            "periods": periods, "trig": int(trig),
            "days_halted": int(days), "n": int(len(legs))}


def main() -> int:
    panel, ap, idx_c, base, cal = build_panel()
    g0 = shift_dates(cal, base, 2)
    g1 = shift_dates(cal, base, 7)
    raw = load_ratings()
    ev = classify_events(raw)
    neg = ev[ev["kind"] == "negative"][["code", "date"]]
    ex0 = build_exclude_map(cal, g0, neg)
    ex1 = build_exclude_map(cal, g1, neg)
    print(f"panel {panel.shape} g0={len(g0)} g1={len(g1)} "
          f"neg={len(neg)} ex0={np.mean([len(v) for v in ex0.values()]):.1f} "
          f"ex1={np.mean([len(v) for v in ex1.values()]):.1f}", flush=True)

    arms = {}
    for name, dates, ex in (("g0_base", g0, None), ("g1_base", g1, None),
                            ("g0_overlay", g0, ex0), ("g1_overlay", g1, ex1)):
        nav, meta = run_group(panel, ap, idx_c, dates, ex)
        arms[name] = {"nav": nav, **meta}
        print(name, meta, flush=True)

    # 两组等权收益（不是净值平均）
    def clean_meta(m: dict) -> dict:
        return {k: v for k, v in m.items() if k != "nav"}

    def two_group(overlay: bool) -> tuple[pd.Series, dict]:
        tag = "overlay" if overlay else "base"
        r0 = arms[f"g0_{tag}"]["nav"].pct_change().dropna()
        r1 = arms[f"g1_{tag}"]["nav"].pct_change().dropna()
        r = pd.concat([r0.rename("g0"), r1.rename("g1")], axis=1,
                      join="inner").mean(axis=1)
        nav = (1 + r).cumprod()
        cm = metrics_ret(r)
        return nav, {"metrics": cm,
                     "g0": clean_meta(arms[f"g0_{tag}"]),
                     "g1": clean_meta(arms[f"g1_{tag}"]),
                     "n": int(len(r))}

    cb = pd.read_csv(CB, index_col=0, parse_dates=True)
    out = {
        "as_of": "2026-11-01",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "registration": "logs/research_registration_analyst_overlay_20261101.md",
        "lookback_trading_days": LOOKBACK,
        "period": [START, END],
        "n_negative_events": int(len(neg)),
        "arms": {},
    }
    for overlay in (False, True):
        label = "overlay" if overlay else "baseline"
        nav, meta = two_group(overlay)
        out["arms"][label] = {
            "stock": meta,
            "par": combo_metrics(nav, cb, "nav_snapdelay_par"),
            "zero": combo_metrics(nav, cb, "nav_snapdelay_zero"),
        }
    b = out["arms"]["baseline"]
    o = out["arms"]["overlay"]
    d = {
        "par_ann_pp": round(o["par"]["metrics"]["ann_pct"]
                            - b["par"]["metrics"]["ann_pct"], 3),
        "zero_ann_pp": round(o["zero"]["metrics"]["ann_pct"]
                             - b["zero"]["metrics"]["ann_pct"], 3),
        "par_dd_pp": round(o["par"]["metrics"]["dd_pct"]
                           - b["par"]["metrics"]["dd_pct"], 3),
        "zero_dd_pp": round(o["zero"]["metrics"]["dd_pct"]
                            - b["zero"]["metrics"]["dd_pct"], 3),
        "par_sharpe": round(o["par"]["metrics"]["sharpe"]
                            - b["par"]["metrics"]["sharpe"], 3),
        "zero_sharpe": round(o["zero"]["metrics"]["sharpe"]
                             - b["zero"]["metrics"]["sharpe"], 3),
    }
    out["delta"] = d
    passed = (d["par_ann_pp"] >= -0.5 and d["zero_ann_pp"] >= -0.5
              and d["par_dd_pp"] >= -1.0 and d["zero_dd_pp"] >= -1.0)
    out["verdict"] = "PASS" if passed else "FAIL"
    out["rule"] = ("overlay par/zero ann >= baseline-0.5pp and DD not worse "
                   ">1pp; else FAIL")
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    lines = [
        "# 分析师“调低”回避 overlay A/B（2026-11-01）",
        "",
        f"- 预登记：`{out['registration']}`；2×20 万；窗口 {START}→{END}；"
        f"调仓日前 {LOOKBACK} 交易日有调低即剔除；negative 事件 {len(neg)}。",
        f"- **裁决：{out['verdict']}**（{out['rule']}）。",
        "",
        "| 臂 | 股票腿 | 40/60 par | 40/60 zero |",
        "|---|---:|---:|---:|",
    ]
    for label in ("baseline", "overlay"):
        s = out["arms"][label]["stock"]["metrics"]
        p = out["arms"][label]["par"]["metrics"]
        z = out["arms"][label]["zero"]["metrics"]
        lines.append(
            f"| {label} | {s['ann_pct']}%/{s['sharpe']}/{s['dd_pct']}% | "
            f"{p['ann_pct']}%/{p['sharpe']}/{p['dd_pct']}% | "
            f"{z['ann_pct']}%/{z['sharpe']}/{z['dd_pct']}% |"
        )
    lines += [
        "",
        "| 臂 | par IS | par OOS | zero IS | zero OOS |",
        "|---|---:|---:|---:|---:|",
    ]
    for label in ("baseline", "overlay"):
        p = out["arms"][label]["par"]["periods"]
        z = out["arms"][label]["zero"]["periods"]
        lines.append(
            f"| {label} | {p.get('IS', {}).get('ann_pct', '—')}% | "
            f"{p.get('OOS', {}).get('ann_pct', '—')}% | "
            f"{z.get('IS', {}).get('ann_pct', '—')}% | "
            f"{z.get('OOS', {}).get('ann_pct', '—')}% |"
        )
    lines += [
        "",
        f"Δ overlay−baseline：par {d['par_ann_pp']:+.3f}pp / "
        f"zero {d['zero_ann_pp']:+.3f}pp；par DD {d['par_dd_pp']:+.3f}pp / "
        f"zero DD {d['zero_dd_pp']:+.3f}pp；par 夏普 {d['par_sharpe']:+.3f} / "
        f"zero 夏普 {d['zero_sharpe']:+.3f}。",
        "",
        "不改实盘；PASS 也只进入 A/B 提案。盲区：CNINFO 二手源、评级覆盖/"
        "重复、事件日滞后、只影响调仓、停牌/涨跌停/流动性未建模。",
    ]
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
