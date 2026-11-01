"""高质押比例回避 overlay A/B（2026-11-01）。

预登记 logs/research_registration_pledge_overlay_20261101.md。
调仓日 D 使用严格早于 D 的最近质押快照；排除质押比例>=50% 的股票；
2×20 万股票腿；40/60 CB par/zero。不改实盘。
"""
from __future__ import annotations

import bisect
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from analyst_overlay_ab import (
    build_panel, combo_metrics, run_group, shift_dates,
)

CACHE = ROOT / "data_store" / "meta" / "pledge_ratio_em"
CB = ROOT / "logs" / "cb_sched_compare_nav.csv"
OUT_JSON = ROOT / "logs" / "pledge_overlay_20261101.json"
OUT_MD = ROOT / "logs" / "pledge_overlay_20261101.md"
THRESHOLD = 50.0
START, END = "2019-01-03", "2026-10-19"


def load_pledge() -> tuple[list[str], dict[str, set[str]]]:
    files = sorted(CACHE.glob("*.parquet"))
    if not files:
        raise RuntimeError(f"no pledge cache in {CACHE}")
    by_date: dict[str, set[str]] = {}
    for p in files:
        d = p.stem
        try:
            x = pd.read_parquet(p)
            x["code"] = x["股票代码"].astype(str).str.zfill(6)
            x["ratio"] = pd.to_numeric(x["质押比例"], errors="coerce")
            hi = set(x.loc[x["ratio"] >= THRESHOLD, "code"])
            by_date[d] = hi
        except Exception:
            continue
    return sorted(by_date), by_date


def build_exclude_map(cal: list[str], dates: list[str],
                      snap_dates: list[str], by_date: dict[str, set[str]]) -> dict:
    out = {}
    for d in dates:
        i = bisect.bisect_left(snap_dates, d) - 1
        out[d] = set(by_date.get(snap_dates[i], set())) if i >= 0 else set()
    return out


def main() -> int:
    panel, ap, idx_c, base, cal = build_panel()
    g0 = shift_dates(cal, base, 2)
    g1 = shift_dates(cal, base, 7)
    snap_dates, by_date = load_pledge()
    ex0 = build_exclude_map(cal, g0, snap_dates, by_date)
    ex1 = build_exclude_map(cal, g1, snap_dates, by_date)
    print(f"panel {panel.shape} g0={len(g0)} g1={len(g1)} "
          f"snaps={len(snap_dates)} avg_hi0={np.mean([len(v) for v in ex0.values()]):.1f} "
          f"avg_hi1={np.mean([len(v) for v in ex1.values()]):.1f}", flush=True)

    arms = {}
    for name, dates, ex in (("g0_base", g0, None), ("g1_base", g1, None),
                            ("g0_overlay", g0, ex0), ("g1_overlay", g1, ex1)):
        nav, meta = run_group(panel, ap, idx_c, dates, ex)
        arms[name] = {"nav": nav, **meta}
        print(name, meta, flush=True)

    def clean(m):
        return {k: v for k, v in m.items() if k != "nav"}

    def two_group(overlay: bool):
        tag = "overlay" if overlay else "base"
        r0 = arms[f"g0_{tag}"]["nav"].pct_change().dropna()
        r1 = arms[f"g1_{tag}"]["nav"].pct_change().dropna()
        r = pd.concat([r0.rename("g0"), r1.rename("g1")], axis=1,
                      join="inner").mean(axis=1)
        nav = (1 + r).cumprod()
        from three_piece_40_60 import metrics_ret
        return nav, {"metrics": metrics_ret(r),
                     "g0": clean(arms[f"g0_{tag}"]),
                     "g1": clean(arms[f"g1_{tag}"]), "n": int(len(r))}

    cb = pd.read_csv(CB, index_col=0, parse_dates=True)
    out = {
        "as_of": "2026-11-01",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "registration": "logs/research_registration_pledge_overlay_20261101.md",
        "threshold_pct": THRESHOLD,
        "n_snapshots": len(snap_dates),
        "period": [START, END],
        "arms": {},
    }
    navs = {}
    for overlay in (False, True):
        label = "overlay" if overlay else "baseline"
        nav, meta = two_group(overlay)
        navs[label] = nav
        out["arms"][label] = {
            "stock": meta,
            "par": combo_metrics(nav, cb, "nav_snapdelay_par"),
            "zero": combo_metrics(nav, cb, "nav_snapdelay_zero"),
        }
    same = (len(navs["baseline"]) == len(navs["overlay"])
            and np.allclose(navs["baseline"].to_numpy(),
                            navs["overlay"].to_numpy(), atol=1e-12))
    out["no_effect"] = bool(same)
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
    if same:
        out["verdict"] = "NO_EFFECT"
        out["rule"] = ("overlay NAV identical to baseline (exclusion never "
                       "hits held/selected); NO_EFFECT, not PASS")
    else:
        out["verdict"] = "PASS" if passed else "FAIL"
        out["rule"] = ("overlay par/zero ann >= baseline-0.5pp and DD not worse "
                       ">1pp; else FAIL")
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    lines = [
        "# 高质押比例回避 overlay A/B（2026-11-01）",
        "",
        f"- 预登记：`{out['registration']}`；2×20 万；快照 {len(snap_dates)}；"
        f"阈值质押比例≥{THRESHOLD:.0f}%；严格早于调仓日。",
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
        "**注意**：若两臂 NAV 完全相同，判定 `NO_EFFECT`，不是 PASS；"
        "说明排除集从未命中持仓/入选池（高质押票不在 top30 成交额池）。",
        "",
        "不改实盘；PASS 也只进入 A/B 提案。盲区：东财二手源、质押快照频率低/"
        "披露滞后、只影响调仓、高质押样本可能很小、停牌/涨跌停/流动性未建模。",
    ]
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
