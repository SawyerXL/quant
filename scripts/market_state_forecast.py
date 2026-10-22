"""次日“状态”预测台账（2026-10-22，每日研究轨道延伸）。

方向类预测已被大样本证明接近抛硬币（docs 记忆 daily-market-study-track）；
本脚本只登记**可验证的状态事件**，并给出历史条件基准率：
  E1: 次日 |SH000001 收益| >= 1.0%
  E2: 次日 |SH000001 收益| >= 1.5%
条件（按优先级取样本最充分者，n>=200）：
  C1 今日大波动(|ret|>=1%) → 波动聚集（历史最强结构）
  C2 当前 vol20 档位（±2.5pp 窗）
  C3 MA200 距离档位（±2pp 窗）
  C0 无条件
输出 logs/market_state_forecast.json + 追加 logs/state_forecast_ledger.csv。
评分（--score）：按次日实际 |ret| 结算，Brier vs 无条件基准率。

用法:
  python scripts/market_state_forecast.py            # 登记今日
  python scripts/market_state_forecast.py --score    # 结算并报告
"""
import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

LEDGER = Path("/root/quant/logs/state_forecast_ledger.csv")
OUT = Path("/root/quant/logs/market_state_forecast.json")
STUDY = Path("/root/quant/logs/market_daily_study.csv")
EVENTS = {"abs_ret_ge_1pct": 0.01, "abs_ret_ge_1.5pct": 0.015}
COLS = ["registered_at", "market_date", "event", "p_event", "base_rate",
        "n_cond", "condition", "features", "outcome_event", "scored_at", "valid"]


def _series() -> pd.Series:
    frames = []
    for yr in range(2018, 2027):
        p = Path(f"/data/quant_data_store/daily/{yr}/SH000001.parquet")
        if p.exists():
            frames.append(pd.read_parquet(p))
    d = pd.concat(frames, ignore_index=True).drop_duplicates("date")
    d["date"] = pd.to_datetime(d["date"])
    return pd.to_numeric(d.set_index("date")["close"], errors="coerce").sort_index()


def _features(s: pd.Series, t: pd.Timestamp) -> dict:
    hist = s[s.index <= t]
    r = hist.pct_change().dropna()
    vol20 = float(r.tail(20).std() * np.sqrt(252) * 100) if len(r) >= 20 else np.nan
    ret1 = float(hist.iloc[-1] / hist.iloc[-2] - 1) if len(hist) >= 2 else np.nan
    ma200 = float(hist.tail(200).mean()) if len(hist) >= 200 else np.nan
    ma200_dist = float(hist.iloc[-1] / ma200 - 1) * 100 if ma200 == ma200 else np.nan
    return {"vol20": round(vol20, 2) if vol20 == vol20 else None,
            "ret1": round(ret1 * 100, 2) if ret1 == ret1 else None,
            "ma200_dist": round(ma200_dist, 2) if ma200_dist == ma200_dist else None}


def _conditional(s: pd.Series, t: pd.Timestamp, ev: float):
    """返回 (p, n, 条件名)：按 C1→C2→C3 取第一个 n>=200 的条件。"""
    r = s.pct_change()
    nxt = r.shift(-1)
    ok = r.notna() & nxt.notna()
    hit = (nxt.abs() >= ev) & ok
    base = float(hit[ok].mean())
    v20 = r.rolling(20).std() * np.sqrt(252) * 100
    ma200 = s.rolling(200).mean()
    cur_v, cur_f = v20.loc[t], _features(s, t)
    cur_r1 = cur_f["ret1"] / 100 if cur_f["ret1"] is not None else np.nan
    cur_md = cur_f["ma200_dist"]
    cands = []
    # C1 波动聚集：今日 |ret|>=1%
    if cur_r1 == cur_r1:
        m = ok & (r.abs() >= 0.01)
        if int(m.sum()) >= 200:
            cands.append(("C1_big_move", float(hit[m].mean()), int(m.sum())))
    # C2 vol20 档位 ±2.5pp
    if cur_v == cur_v:
        m = ok & (v20.sub(cur_v).abs() <= 2.5)
        if int(m.sum()) >= 200:
            cands.append(("C2_vol20_bin", float(hit[m].mean()), int(m.sum())))
    # C3 MA200 距离 ±2pp
    if cur_md == cur_md:
        dist = (s / ma200 - 1) * 100
        m = ok & (dist.sub(cur_md).abs() <= 2.0)
        if int(m.sum()) >= 200:
            cands.append(("C3_ma200_bin", float(hit[m].mean()), int(m.sum())))
    if cands:
        name, p, n = cands[0]
        return p, n, name, base
    return base, int(ok.sum()), "C0_unconditional", base


def register():
    s = _series()
    t = s.index[-1]
    feats = _features(s, t)
    now = datetime.now()
    rows = []
    for ev_name, ev in EVENTS.items():
        p, n, cond, base = _conditional(s, t, ev)
        rows.append({"registered_at": now.isoformat(timespec="seconds"),
                     "market_date": str(t.date()), "event": ev_name,
                     "p_event": round(p, 4), "base_rate": round(base, 4),
                     "n_cond": n, "condition": cond,
                     "features": json.dumps(feats, ensure_ascii=False),
                     "outcome_event": "", "scored_at": "", "valid": "ok"})
    df = pd.DataFrame(rows, columns=COLS)
    if LEDGER.exists():
        old = pd.read_csv(LEDGER)
        df = pd.concat([old, df], ignore_index=True)
    df.to_csv(LEDGER, index=False)
    OUT.write_text(json.dumps({"market_date": str(t.date()), "features": feats,
                               "forecasts": rows}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    for r in rows:
        print(f"{r['market_date']} {r['event']}: p={r['p_event']:.3f} "
              f"base={r['base_rate']:.3f} n={r['n_cond']} ({r['condition']})")


def score():
    if not LEDGER.exists():
        print("台账为空")
        return
    s = _series()
    r = s.pct_change()
    df = pd.read_csv(LEDGER)
    for col in ("outcome_event", "scored_at", "valid"):
        df[col] = df[col].astype(object)
    for i, row in df.iterrows():
        if str(row.get("outcome_event", "")) not in ("", "nan"):
            continue
        t = pd.Timestamp(row["market_date"])
        if t not in s.index:
            continue
        pos = s.index.get_loc(t)
        if pos + 1 >= len(s):
            continue
        nxt = r.iloc[pos + 1]
        if pd.isna(nxt):
            continue
        ev = EVENTS[row["event"]]
        df.at[i, "outcome_event"] = int(abs(nxt) >= ev)
        df.at[i, "scored_at"] = datetime.now().isoformat(timespec="seconds")
    df.to_csv(LEDGER, index=False)
    d = df[df["outcome_event"].notna()].copy()
    if d.empty:
        print("暂无可结算记录")
        return
    d["p_event"] = pd.to_numeric(d["p_event"])
    d["outcome_event"] = pd.to_numeric(d["outcome_event"])
    d = d.sort_values("registered_at").drop_duplicates(["market_date", "event"], keep="last")
    for ev_name in EVENTS:
        e = d[d["event"] == ev_name]
        if e.empty:
            continue
        brier = float(((e["p_event"] - e["outcome_event"]) ** 2).mean())
        base = float(((e["base_rate"] - e["outcome_event"]) ** 2).mean())
        print(f"{ev_name}: n={len(e)} 实际发生率={e['outcome_event'].mean():.1%} "
              f"Brier={brier:.4f} vs 基准率={base:.4f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--score", action="store_true")
    a = ap.parse_args()
    score() if a.score else register()
