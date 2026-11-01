"""CB top25 评级下限 A/B：A- 及以上 vs AA- 及以上（2026-11-01）。

预登记 logs/research_registration_cb_rating_floor_20261101.md。
只改 CB 选券的评级下限；日程/成本/回收/股票腿固定。输出 CB 腿与
40/60 的 par/zero 指标；不改实盘。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from data.cb_select import _strip_rating_suffix, filter_universe, select_double_low
from three_piece_40_60 import combo, metrics_ret
from cb_same_universe_benchmark import (
    build_panel, metrics, run_arm, select_top25,
)

STOCK = ROOT / "logs" / "stock_leg_nav_20w_avg.csv"
OUT_JSON = ROOT / "logs" / "cb_rating_floor_20261101.json"
OUT_MD = ROOT / "logs" / "cb_rating_floor_20261101.md"
RATING = {"AAA": 0, "AA+": 1, "AA": 2, "AA-": 3,
          "A+": 4, "A": 5, "A-": 6}
TOP_N = 25


def select_aa_floor(snap: pd.DataFrame, avails: set[str], prices: dict) -> list:
    sub = filter_universe(snap)
    sub = sub.copy()
    sub["rn"] = sub["rating"].apply(_strip_rating_suffix).map(RATING)
    sub = sub[sub["rn"] <= 3]
    sub = sub[sub["code"].isin(avails)]
    codes = select_double_low(sub, top=TOP_N)
    return codes if len(codes) >= TOP_N else []


def combo_metrics(cb_nav: pd.Series, stock_nav: pd.Series) -> dict:
    rs = stock_nav.pct_change().dropna()
    rc = cb_nav.pct_change().dropna()
    legs = pd.concat([rs.rename("s"), rc.rename("cb")], axis=1,
                     join="inner").dropna()
    nav, trig, days = combo(legs)
    return {"metrics": metrics_ret(nav.pct_change().fillna(0)),
            "trig": int(trig), "days_halted": int(days), "n": int(len(legs))}


def main() -> int:
    snapshots, panel, all_dates, last_price = build_panel()
    stock = pd.read_csv(STOCK, index_col=0, parse_dates=True)["nav"]
    arms = {}
    for label, selector in (("current", select_top25),
                            ("aa_floor", select_aa_floor)):
        for rec in ("par", "zero"):
            nav, meta = run_arm(snapshots, panel, all_dates, selector, label, rec)
            arms[f"{label}_{rec}"] = {
                "cb": {**meta, **metrics(nav)},
                "combo": combo_metrics(nav, stock),
            }
    out = {
        "as_of": "2026-11-01",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "registration": "logs/research_registration_cb_rating_floor_20261101.md",
        "period": ["2019-02-01", "2026-09-30"],
        "cost": "10bp per side; par/zero recovery",
        "arms": arms,
    }
    c = arms["current_zero"]["combo"]["metrics"]
    a = arms["aa_floor_zero"]["combo"]["metrics"]
    cp = arms["current_par"]["combo"]["metrics"]
    ap = arms["aa_floor_par"]["combo"]["metrics"]
    out["delta"] = {
        "zero_ann_pp": round(a["ann_pct"] - c["ann_pct"], 3),
        "par_ann_pp": round(ap["ann_pct"] - cp["ann_pct"], 3),
        "zero_sharpe": round(a["sharpe"] - c["sharpe"], 3),
        "zero_dd_pp": round(a["dd_pct"] - c["dd_pct"], 3),
    }
    passed = (a["ann_pct"] > c["ann_pct"]
              and ap["ann_pct"] >= cp["ann_pct"] - 1.0
              and a["dd_pct"] >= c["dd_pct"] - 1e-9)
    out["verdict"] = "PASS" if passed else "FAIL"
    out["rule"] = ("aa_floor zero ann > current zero; par ann >= current par-1pp; "
                   "zero DD not deeper; else FAIL")
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    lines = [
        "# CB top25 评级下限 A/B（2026-11-01）",
        "",
        f"- 预登记：`{out['registration']}`；窗口 {out['period'][0]}→{out['period'][1]}；"
        "10bp/边；par/zero 两口径。",
        f"- **裁决：{out['verdict']}**（{out['rule']}）。",
        "",
        "| 臂 | 回收 | CB 年化 | CB 夏普 | CB 最大回撤 | 40/60 年化 | 40/60 夏普 | 40/60 回撤 | pop |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for label, rec in (("current", "par"), ("aa_floor", "par"),
                       ("current", "zero"), ("aa_floor", "zero")):
        x = arms[f"{label}_{rec}"]
        cb, cm = x["cb"], x["combo"]["metrics"]
        lines.append(
            f"| {label} | {rec} | {cb['ann_pct']}% | {cb['sharpe']} | "
            f"{cb['maxdd_pct']}% | {cm['ann_pct']}% | {cm['sharpe']} | "
            f"{cm['dd_pct']}% | {cb['pops']} |"
        )
    lines += [
        "",
        f"Δ（aa_floor − current）：zero 年化 {out['delta']['zero_ann_pp']:+.3f}pp、"
        f"par 年化 {out['delta']['par_ann_pp']:+.3f}pp、"
        f"zero 夏普 {out['delta']['zero_sharpe']:+.3f}、"
        f"zero 回撤 {out['delta']['zero_dd_pp']:+.3f}pp。",
        "",
        "不改实盘；PASS 也只进入 A/B 提案。盲区：评级快照滞后、违约未逐日建模、"
        "par/zero 是上下界、单一路径、无真实成交/流动性。",
    ]
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
