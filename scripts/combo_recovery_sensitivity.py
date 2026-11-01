"""40/60 组合对 CB 退市回收口径的敏感性（2026-11-01）。

同一股票腿（live {2,7} 两组 20 万，`stock_leg_nav_20w_avg.csv`）与
同一 CB 日程（snapdelay），只替换 CB 的退市回收：
par（面值 100）/ last（最后价）/ zero（全损）。
目的：把 11.97% 的 par 依赖量化到组合层，不改变策略。

输出 logs/combo_recovery_20261101.json/.md；只读研究。
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

from three_piece_40_60 import combo, metrics_ret

STOCK = ROOT / "logs" / "stock_leg_nav_20w_avg.csv"
CB = ROOT / "logs" / "cb_sched_compare_nav.csv"
OLD = ROOT / "logs" / "combo_nav_20w_two_group.csv"
OUT_JSON = ROOT / "logs" / "combo_recovery_20261101.json"
OUT_MD = ROOT / "logs" / "combo_recovery_20261101.md"
COLS = {
    "par": "nav_snapdelay_par",
    "last": "nav_snapdelay_last",
    "zero": "nav_snapdelay_zero",
}


def run(stock_nav: pd.Series, cb_ret: pd.Series) -> tuple[pd.Series, dict]:
    rs = stock_nav.pct_change().dropna()
    legs = pd.concat([rs.rename("s"), cb_ret.rename("cb")], axis=1,
                     join="inner").dropna()
    nav, trig, days = combo(legs)
    return nav, {"metrics": metrics_ret(nav.pct_change().fillna(0)),
                 "trig": int(trig), "days_halted": int(days),
                 "n": int(len(legs))}


def main() -> int:
    stock = pd.read_csv(STOCK, index_col=0, parse_dates=True)["nav"]
    cb = pd.read_csv(CB, index_col=0, parse_dates=True)
    out = {
        "as_of": "2026-11-01",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "stock_leg": str(STOCK),
        "stock_leg_note": "live {2,7} two groups, 20万/组, equal-weight",
        "weights": [0.4, 0.6],
        "cb_schedule": "snapdelay",
        "arms": {},
    }
    for label, col in COLS.items():
        nav, meta = run(stock, cb[col].pct_change().dropna())
        out["arms"][label] = meta
        nav.to_csv(ROOT / "logs" / f"combo_nav_20w_{label}.csv", header=["nav"])
    if OLD.exists():
        old = pd.read_csv(OLD, index_col=0, parse_dates=True)["nav"]
        new = pd.read_csv(ROOT / "logs" / "combo_nav_20w_par.csv",
                          index_col=0, parse_dates=True)["nav"]
        j = pd.concat([new.rename("new"), old.rename("old")], axis=1,
                      join="inner").dropna()
        out["reconciliation"] = {
            "n": int(len(j)),
            "end_rel_diff_pct": round(float(
                (j["new"].iloc[-1] / j["new"].iloc[0])
                / (j["old"].iloc[-1] / j["old"].iloc[0]) - 1) * 100, 4),
        }
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    lines = [
        "# 40/60 组合对 CB 退市回收口径的敏感性（2026-11-01）",
        "",
        "股票腿：live {2,7} 两组 20 万/组等权；CB：snapdelay；权重 40/60。",
        "只替换 CB 退市回收，其它同。",
        "",
        "| CB 回收 | 年化 | 夏普 | 最大回撤 | 熔断触发 | 熔断天数 | n |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for label, a in out["arms"].items():
        m = a["metrics"]
        lines.append(f"| {label} | {m['ann_pct']}% | {m['sharpe']} | "
                     f"{m['dd_pct']}% | {a['trig']} | {a['days_halted']} | {a['n']} |")
    if "reconciliation" in out:
        lines += ["", f"par 复刻校验（对 2026-10-23 工件）："
                      f"n={out['reconciliation']['n']}，"
                      f"末值相对差 {out['reconciliation']['end_rel_diff_pct']}%。"]
    lines += [
        "",
        "解读：par 是 11.97% 的来源；zero 是保守下界。两者之差就是"
        "“退市按面值回收”这一假设对组合年化的贡献，不能当作确定收益。",
        "本表不改策略/仓位；实盘预期应按 par/zero 区间报。",
    ]
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
