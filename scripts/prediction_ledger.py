"""A股走势预测台账(2026-10-16 立项): 事前登记 → 事后机评, 禁止事后改数。

登记规则(charter §3): 预测必须在目标交易日 09:30 前登记; registered_at
晚于目标日 09:30 的记录在评分时标 invalid(不计入技能评估)。
评分: Brier = mean((p_up − outcome)²); 基线 = 常值基准率 / 0.5 / MA200 regime。
结算基准: SH000001 收盘价; horizon=1 → close[t]/close[t-1]−1;
horizon=5 → close[t+4]/close[t-1]−1(自 t-1 收盘起 5 个交易日)。

用法:
  python scripts/prediction_ledger.py register --market-date 2026-10-19 \\
      --horizon 1 --p-up 0.45 --direction down --confidence low \\
      --rationale "..." --features "ma200_dist=-7.2;vol20=12.2;breadth=16.6"
  python scripts/prediction_ledger.py score
  python scripts/prediction_ledger.py report
"""
import argparse
import hashlib
import json
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

LEDGER = Path("/root/quant/logs/prediction_ledger.csv")
INDEX = Path("/data/quant_data_store/daily/2026/SH000001.parquet")
COLS = ["registered_at", "market_date", "horizon_d", "p_up", "direction",
        "confidence", "rationale", "features", "features_hash",
        "outcome_pct", "outcome_up", "outcome_date", "scored_at", "valid"]


def _index_series() -> pd.Series:
    frames = []
    for yr in range(2018, 2027):
        p = Path(f"/data/quant_data_store/daily/{yr}/SH000001.parquet")
        if p.exists():
            frames.append(pd.read_parquet(p))
    d = pd.concat(frames, ignore_index=True).drop_duplicates("date")
    d["date"] = pd.to_datetime(d["date"])
    return pd.to_numeric(d.set_index("date")["close"], errors="coerce").sort_index()


def cmd_register(args):
    now = datetime.now()
    target_open = datetime.fromisoformat(f"{args.market_date}T09:30:00")
    valid = "ok" if now < target_open else "late"
    ln = len(pd.read_csv(LEDGER)) + 1 if LEDGER.exists() else 1
    rec = {
        "registered_at": now.isoformat(timespec="seconds"),
        "market_date": args.market_date,
        "horizon_d": args.horizon,
        "p_up": round(float(args.p_up), 3),
        "direction": args.direction,
        "confidence": args.confidence,
        "rationale": args.rationale,
        "features": args.features,
        "features_hash": hashlib.sha256(args.features.encode()).hexdigest()[:12],
        "outcome_pct": "", "outcome_up": "", "outcome_date": "",
        "scored_at": "", "valid": valid,
    }
    df = pd.DataFrame([rec], columns=COLS)
    if LEDGER.exists():
        df = pd.concat([pd.read_csv(LEDGER), df], ignore_index=True)
    df.to_csv(LEDGER, index=False)
    print(f"已登记 #{ln}: {args.market_date} h={args.horizon}d p_up={args.p_up} "
          f"{args.direction}({args.confidence}) valid={valid}")


def cmd_score(_args):
    if not LEDGER.exists():
        print("台账为空")
        return
    s = _index_series()
    df = pd.read_csv(LEDGER)
    # 空结果列会被推断为 float64, 写入字符串会告警/未来报错——显式转 object
    for col in ("outcome_pct", "outcome_up", "outcome_date", "scored_at", "valid"):
        if col in df.columns:
            df[col] = df[col].astype(object)
    for i, r in df.iterrows():
        if str(r.get("outcome_up", "")) not in ("", "nan") and pd.notna(r["outcome_up"]):
            continue
        t = pd.Timestamp(r["market_date"])
        if t not in s.index:
            continue
        pos = s.index.get_loc(t)
        h = int(r["horizon_d"])
        end_pos = pos + h - 1
        if end_pos >= len(s):
            continue
        base_pos = pos - 1
        if base_pos < 0:
            continue
        ret = s.iloc[end_pos] / s.iloc[base_pos] - 1
        df.at[i, "outcome_pct"] = round(float(ret) * 100, 2)
        df.at[i, "outcome_up"] = int(ret > 0)
        df.at[i, "outcome_date"] = str(s.index[end_pos].date())
        df.at[i, "scored_at"] = datetime.now().isoformat(timespec="seconds")
    df.to_csv(LEDGER, index=False)
    print(f"已结算 {int(df['outcome_up'].notna().sum())}/{len(df)} 条")


def cmd_report(_args):
    if not LEDGER.exists():
        print("台账为空")
        return
    df = pd.read_csv(LEDGER)
    df["outcome_up"] = pd.to_numeric(df["outcome_up"], errors="coerce")
    d = df[(df["valid"] == "ok") & df["outcome_up"].notna()].copy()
    if d.empty:
        print("暂无可评分记录(有效且已结算)")
        return
    d["p_up"] = pd.to_numeric(d["p_up"])
    d["outcome_up"] = pd.to_numeric(d["outcome_up"])
    brier = ((d["p_up"] - d["outcome_up"]) ** 2).mean()
    base = d["outcome_up"].mean()
    brier_base = ((base - d["outcome_up"]) ** 2).mean()
    brier_5050 = ((0.5 - d["outcome_up"]) ** 2).mean()
    hit = ((d["p_up"] > 0.5).astype(int) == d["outcome_up"]).mean()
    print(f"有效样本 {len(d)} 条 | 实际上涨占比 {base:.1%}")
    print(f"Brier {brier:.4f} vs 基准率常值 {brier_base:.4f} vs 0.5常值 {brier_5050:.4f}")
    print(f"方向命中率 {hit:.1%}")
    print(d[["market_date", "horizon_d", "p_up", "direction",
             "outcome_pct", "outcome_up", "valid"]].tail(15).to_string(index=False))
    out = Path("/root/quant/logs/prediction_score.json")
    out.write_text(json.dumps({
        "n": len(d), "base_rate": round(float(base), 4),
        "brier": round(float(brier), 4),
        "brier_base_rate": round(float(brier_base), 4),
        "brier_5050": round(float(brier_5050), 4),
        "hit_rate": round(float(hit), 4),
        "computed_at": datetime.now().isoformat(timespec="seconds"),
    }, ensure_ascii=False, indent=1))
    print(f"评分已保存: {out}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("register")
    p.add_argument("--market-date", required=True)
    p.add_argument("--horizon", type=int, default=1, choices=[1, 5])
    p.add_argument("--p-up", type=float, required=True)
    p.add_argument("--direction", required=True, choices=["up", "down", "flat"])
    p.add_argument("--confidence", default="low", choices=["low", "mid", "high"])
    p.add_argument("--rationale", required=True)
    p.add_argument("--features", default="")
    p.set_defaults(func=cmd_register)
    p = sub.add_parser("score")
    p.set_defaults(func=cmd_score)
    p = sub.add_parser("report")
    p.set_defaults(func=cmd_report)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
