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

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

LEDGER = Path("/root/quant/logs/prediction_ledger.csv")
INDEX = Path("/data/quant_data_store/daily/2026/SH000001.parquet")
COLS = ["registered_at", "market_date", "horizon_d", "p_up", "direction",
        "confidence", "rationale", "features", "features_hash",
        "outcome_pct", "outcome_up", "outcome_date", "scored_at", "valid",
        "outcome_csi800_pct", "outcome_csi800_up"]


def _index_series() -> pd.Series:
    frames = []
    for yr in range(2018, 2027):
        p = Path(f"/data/quant_data_store/daily/{yr}/SH000001.parquet")
        if p.exists():
            frames.append(pd.read_parquet(p))
    d = pd.concat(frames, ignore_index=True).drop_duplicates("date")
    d["date"] = pd.to_datetime(d["date"])
    return pd.to_numeric(d.set_index("date")["close"], errors="coerce").sort_index()


def _csi800_series() -> pd.Series:
    """同一预测在交易宇宙(中证800)上的结果；主目标仍是 SH000001。"""
    frames = []
    for yr in range(2018, 2027):
        p = Path(f"/data/quant_data_store/daily/{yr}/SH000906.parquet")
        if p.exists():
            frames.append(pd.read_parquet(p))
    if not frames:
        return pd.Series(dtype=float)
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
        "outcome_csi800_pct": "", "outcome_csi800_up": "",
    }
    df = pd.DataFrame([rec], columns=COLS)
    if LEDGER.exists():
        df = pd.concat([pd.read_csv(LEDGER), df], ignore_index=True)
    df.to_csv(LEDGER, index=False)
    print(f"已登记 #{ln}: {args.market_date} h={args.horizon}d p_up={args.p_up} "
          f"{args.direction}({args.confidence}) valid={valid}")


def base_rate(s: pd.Series, dist: pd.Series, h: int, below: bool):
    """按 MA200 位置条件化的 h 日上涨率（纯函数，供 auto-base/测试）。

    s=收盘序列；dist=s/MA200−1；h=1/5；below=True 用 dist<0 的历史日。
    结果只用 d 日及之前信息 + d+h 的收盘（与台账评分同口径）。
    """
    out = (s.shift(-h) / s - 1).dropna()
    mask = (dist < 0) if below else (dist >= 0)
    vals = out[mask.reindex(out.index).fillna(False)].dropna()
    if len(vals) == 0:
        return None, 0
    return float((vals > 0).mean()), int(len(vals))


def _next_trade_day(today: str):
    from data.storage import load_meta
    cal = load_meta("trade_calendar")
    if cal is None or cal.empty:
        return None
    ds = sorted(pd.to_datetime(cal["trade_date"]).dt.strftime("%Y-%m-%d").unique())
    return next((d for d in ds if d > today), None)


def cmd_auto_base(_args):
    """每日收盘后登记下一交易日的基准率预测（charter §3：无优势登记基准率）。

    只在最新 bar=今天时运行（非交易日/数据未更新→跳过，不写行）。
    同一 (market_date,horizon) 已有任何登记则跳过——人工盘前覆盖不会被覆盖。
    """
    s = _index_series()
    today = str(date.today())
    if s.empty or str(s.index[-1].date()) != today:
        print(f"最新bar {s.index[-1].date() if len(s) else '?'} != 今天 {today} → 跳过")
        return
    target = _next_trade_day(today)
    if not target:
        print("无下一交易日，跳过")
        return
    r = s.pct_change()
    ma200 = s.rolling(200).mean()
    dist = s / ma200 - 1
    vol20 = float(r.tail(20).std() * np.sqrt(252) * 100)
    below = bool(dist.iloc[-1] < 0)
    feats = (f"ma200_dist={float(dist.iloc[-1])*100:.2f};"
             f"vol20={vol20:.2f};auto=base")
    hist = pd.read_csv(LEDGER) if LEDGER.exists() else pd.DataFrame(columns=COLS)
    for h in (1, 5):
        has = hist[(hist["market_date"].astype(str) == target)
                   & (hist["horizon_d"].astype(int) == h)]
        if len(has):
            print(f"{target} h={h} 已有登记，跳过")
            continue
        p, n = base_rate(s, dist, h, below)
        if p is None:
            print(f"{target} h={h} 无历史样本，跳过")
            continue
        direction = "up" if p > 0.5 else ("down" if p < 0.5 else "flat")
        ns = argparse.Namespace(
            market_date=target, horizon=h, p_up=round(p, 3),
            direction=direction, confidence="low",
            rationale=(f"auto base rate: MA200{'下方' if below else '上方'} "
                       f"h={h} P(up)={p:.1%} (n={n})；无优势，登记基准率"
                       f"（人工可在 {target} 09:30 前覆盖）"),
            features=feats)
        cmd_register(ns)


def cmd_score(_args):
    if not LEDGER.exists():
        print("台账为空")
        return
    s = _index_series()
    cb = _csi800_series()
    df = pd.read_csv(LEDGER)
    # 空结果列会被推断为 float64, 写入字符串会告警/未来报错——显式转 object
    for col in ("outcome_pct", "outcome_up", "outcome_date", "scored_at", "valid",
                "outcome_csi800_pct", "outcome_csi800_up"):
        if col not in df.columns:
            df[col] = ""
        df[col] = df[col].astype(object)

    def _done(v):
        return str(v) not in ("", "nan") and pd.notna(v)

    for i, r in df.iterrows():
        sh_done = _done(r.get("outcome_up", ""))
        cb_done = _done(r.get("outcome_csi800_up", ""))
        if sh_done and cb_done:
            continue
        t = pd.Timestamp(r["market_date"])
        if t not in s.index:
            continue
        pos = s.index.get_loc(t)
        h = int(r["horizon_d"])
        end_pos = pos + h - 1
        base_pos = pos - 1
        if end_pos >= len(s) or base_pos < 0:
            continue
        if not sh_done:
            ret = s.iloc[end_pos] / s.iloc[base_pos] - 1
            df.at[i, "outcome_pct"] = round(float(ret) * 100, 2)
            df.at[i, "outcome_up"] = int(ret > 0)
            df.at[i, "outcome_date"] = str(s.index[end_pos].date())
            df.at[i, "scored_at"] = datetime.now().isoformat(timespec="seconds")
        if not cb_done and len(cb):
            d0, d1 = s.index[base_pos], s.index[end_pos]
            if d0 in cb.index and d1 in cb.index:
                cb_ret = cb.loc[d1] / cb.loc[d0] - 1
                df.at[i, "outcome_csi800_pct"] = round(float(cb_ret) * 100, 2)
                df.at[i, "outcome_csi800_up"] = int(cb_ret > 0)
                if not sh_done:
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
    # 同一 (market_date, horizon) 的盘前修订只保留最后一次登记——否则
    # 一次预测的多次修订会被当成多个独立样本，Brier/命中率被重复计数。
    n_raw = len(d)
    d = (d.sort_values("registered_at")
           .drop_duplicates(["market_date", "horizon_d"], keep="last"))
    d["p_up"] = pd.to_numeric(d["p_up"])
    d["outcome_up"] = pd.to_numeric(d["outcome_up"])
    print(f"评分样本: {len(d)} 个目标 (原始有效记录 {n_raw} 条, 已按目标去重)")
    brier = ((d["p_up"] - d["outcome_up"]) ** 2).mean()
    base = d["outcome_up"].mean()
    brier_base = ((base - d["outcome_up"]) ** 2).mean()
    brier_5050 = ((0.5 - d["outcome_up"]) ** 2).mean()
    hit = ((d["p_up"] > 0.5).astype(int) == d["outcome_up"]).mean()
    print(f"有效样本 {len(d)} 条 | 实际上涨占比 {base:.1%}")
    print(f"Brier {brier:.4f} vs 基准率常值 {brier_base:.4f} vs 0.5常值 {brier_5050:.4f}")
    print(f"方向命中率 {hit:.1%}")
    # 同一 p_up 在交易宇宙(中证800)上的反事实；主目标仍是 SH000001
    csi = {}
    if "outcome_csi800_up" in d.columns:
        d["outcome_csi800_up"] = pd.to_numeric(d["outcome_csi800_up"], errors="coerce")
        dc = d.dropna(subset=["outcome_csi800_up"])
        if len(dc):
            c_base = dc["outcome_csi800_up"].mean()
            c_brier = ((dc["p_up"] - dc["outcome_csi800_up"]) ** 2).mean()
            c_hit = ((dc["p_up"] > 0.5).astype(int) == dc["outcome_csi800_up"]).mean()
            csi = {"n": int(len(dc)), "base_rate": round(float(c_base), 4),
                   "brier": round(float(c_brier), 4),
                   "hit_rate": round(float(c_hit), 4)}
            print(f"中证800反事实: n={len(dc)} 上涨占比={c_base:.1%} "
                  f"Brier={c_brier:.4f} 命中率={c_hit:.1%}")
    print(d[["market_date", "horizon_d", "p_up", "direction",
             "outcome_pct", "outcome_up", "valid"]].tail(15).to_string(index=False))
    out = Path("/root/quant/logs/prediction_score.json")
    out.write_text(json.dumps({
        "n_targets": len(d), "n_raw_valid_records": n_raw,
        "method": "dedupe by (market_date,horizon_d) keep last registered_at",
        "base_rate": round(float(base), 4),
        "brier": round(float(brier), 4),
        "brier_base_rate": round(float(brier_base), 4),
        "brier_5050": round(float(brier_5050), 4),
        "hit_rate": round(float(hit), 4),
        "csi800_counterfactual": csi,
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
    p = sub.add_parser("auto-base")
    p.set_defaults(func=cmd_auto_base)
    p = sub.add_parser("score")
    p.set_defaults(func=cmd_score)
    p = sub.add_parser("report")
    p.set_defaults(func=cmd_report)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
