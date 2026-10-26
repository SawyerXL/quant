"""明日大盘简报：当前技术状态 + 历史条件分布（2026-10-25）。

只做描述性统计，不做方向预测（项目已证：次日方向≈抛硬币，只有波动聚集可预测）。
所有数字由本脚本输出；报告引用需带 N 与样本区间。
"""
import argparse
import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = Path("/data/quant_data_store")
OUT = ROOT / "logs"


def load_index(code: str, start_year: int = 2000) -> pd.DataFrame:
    frames = []
    for yr in range(start_year, 2027):
        p = DATA / "daily" / str(yr) / f"{code}.parquet"
        if p.exists():
            frames.append(pd.read_parquet(p))
    if not frames:
        raise SystemExit(f"no data {code}")
    d = pd.concat(frames, ignore_index=True).drop_duplicates("date")
    d["date"] = pd.to_datetime(d["date"])
    d = d.sort_values("date").reset_index(drop=True)
    for c in ["open", "high", "low", "close", "volume", "amount"]:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    return d


def features(d: pd.DataFrame) -> pd.DataFrame:
    c = d["close"]
    r = c.pct_change()
    d = d.copy()
    for n in (5, 10, 20, 60, 120, 200):
        d[f"ma{n}"] = c.rolling(n).mean()
    d["r1"] = r
    d["vol20"] = r.rolling(20).std() * np.sqrt(252) * 100
    d["ret5"] = c / c.shift(5) - 1
    d["ret20"] = c / c.shift(20) - 1
    # RSI14
    delta = c.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta).clip(lower=0).rolling(14).mean()
    d["rsi14"] = 100 - 100 / (1 + gain / loss)
    # MACD
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    d["dif"] = ema12 - ema26
    d["dea"] = d["dif"].ewm(span=9, adjust=False).mean()
    d["macd_hist"] = 2 * (d["dif"] - d["dea"])
    # ATR14
    tr = pd.concat([(d["high"] - d["low"]),
                    (d["high"] - c.shift()).abs(),
                    (d["low"] - c.shift()).abs()], axis=1).max(axis=1)
    d["atr14"] = tr.rolling(14).mean()
    d["fwd1"] = c.shift(-1) / c - 1
    d["fwd5"] = c.shift(-5) / c - 1
    return d


def stats(x: pd.Series) -> dict:
    x = x.dropna()
    if len(x) == 0:
        return {"n": 0}
    return {
        "n": int(len(x)),
        "mean_pct": round(float(x.mean()) * 100, 3),
        "median_pct": round(float(x.median()) * 100, 3),
        "sd_pct": round(float(x.std()) * 100, 3),
        "up_rate": round(float((x > 0).mean()), 4),
        "p_ge_1": round(float((x >= 0.01).mean()), 4),
        "p_le_neg1": round(float((x <= -0.01).mean()), 4),
        "p_ge_1_5": round(float((x >= 0.015).mean()), 4),
        "p_le_neg1_5": round(float((x <= -0.015).mean()), 4),
        "p10": round(float(x.quantile(0.10)) * 100, 3),
        "p90": round(float(x.quantile(0.90)) * 100, 3),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", default="2026-10-23")
    ap.add_argument("--start", default="2012-01-01")
    args = ap.parse_args()
    d = features(load_index("SH000001"))
    d = d[d["date"] <= pd.Timestamp(args.asof)].reset_index(drop=True)
    x = d.iloc[-1]
    prev = d.iloc[-2]
    y2026 = d[d["date"].dt.year == 2026]
    y2025 = d[d["date"].dt.year == 2025]
    y2024 = d[d["date"].dt.year == 2024]

    tech = {
        "asof": str(x["date"].date()), "close": round(float(x["close"]), 2),
        "pct": round(float(x["r1"]) * 100, 2),
        "ma5": round(float(x["ma5"]), 2), "ma10": round(float(x["ma10"]), 2),
        "ma20": round(float(x["ma20"]), 2), "ma60": round(float(x["ma60"]), 2),
        "ma120": round(float(x["ma120"]), 2), "ma200": round(float(x["ma200"]), 2),
        "dist_ma20": round(float(x["close"] / x["ma20"] - 1) * 100, 2),
        "dist_ma60": round(float(x["close"] / x["ma60"] - 1) * 100, 2),
        "dist_ma200": round(float(x["close"] / x["ma200"] - 1) * 100, 2),
        "rsi14": round(float(x["rsi14"]), 1),
        "dif": round(float(x["dif"]), 2), "dea": round(float(x["dea"]), 2),
        "macd_hist": round(float(x["macd_hist"]), 2),
        "atr14": round(float(x["atr14"]), 2),
        "vol20_ann": round(float(x["vol20"]), 1),
        "ret5": round(float(x["ret5"]) * 100, 2), "ret20": round(float(x["ret20"]) * 100, 2),
        "prev_pct": round(float(prev["r1"]) * 100, 2),
        "high_2026": round(float(y2026["high"].max()), 2),
        "low_2026": round(float(y2026["low"].min()), 2),
        "high_date_2026": str(y2026.loc[y2026["high"].idxmax(), "date"].date()),
        "low_date_2026": str(y2026.loc[y2026["low"].idxmin(), "date"].date()),
        "dd_from_2026_high": round(float(x["close"] / y2026["high"].max() - 1) * 100, 2),
        "high_2025": round(float(y2025["high"].max()), 2),
        "low_2025": round(float(y2025["low"].min()), 2),
        "high_2024": round(float(y2024["high"].max()), 2),
        "low_2024": round(float(y2024["low"].min()), 2),
        "level_3674_dist": round(float(x["close"] / 3674.41 - 1) * 100, 2),
        "above_ma200": bool(x["close"] > x["ma200"]),
    }

    s = d[d["date"] >= pd.Timestamp(args.start)].copy()
    s["below200"] = s["close"] < s["ma200"]
    s["vol_bin"] = s["vol20"].between(11, 15)
    s["ret20_bin"] = s["ret20"].between(-0.06, -0.03)
    # 前一日大跌 + 当日反弹（深V）
    s["prev"] = s["r1"].shift(1)
    s["deepv"] = (s["prev"] <= -0.012) & (s["r1"] >= 0.008)
    s["drop5_then_up"] = (s["ret5"].shift(1) <= -0.03) & (s["r1"] >= 0.008)
    # 当日跌停/涨停家数没有入本表；用广度代理从 daily study 另读

    cond = {
        "base_2012": stats(s["fwd1"]),
        "below_ma200": stats(s.loc[s["below200"], "fwd1"]),
        "below_ma200_vol11_15": stats(s.loc[s["below200"] & s["vol_bin"], "fwd1"]),
        "below_ma200_ret20_m6_m3": stats(s.loc[s["below200"] & s["ret20_bin"], "fwd1"]),
        "deepv_next1": stats(s.loc[s["deepv"], "fwd1"]),
        "deepv_next5": stats(s.loc[s["deepv"], "fwd5"]),
        "drop5_up_next1": stats(s.loc[s["drop5_then_up"], "fwd1"]),
        "drop5_up_next5": stats(s.loc[s["drop5_then_up"], "fwd5"]),
        "below200_next5": stats(s.loc[s["below200"], "fwd5"]),
        "current_state_next1": stats(s.loc[s["below200"] & s["vol_bin"] & s["ret20_bin"], "fwd1"]),
    }
    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "asof": args.asof, "sample_start": args.start,
        "tech": tech, "conditional": cond,
        "notes": [
            "次日方向历史基准≈53.5%（MA200下方），无可靠优势；本表只给分布，不给方向结论",
            "深V样本小、重叠，统计量仅描述；不可当规则",
            "vol20=13% 对应状态预测 C2：P(|ret|>=1%)≈21.6%, P(|ret|>=1.5%)≈7.8%",
        ],
    }
    OUT.mkdir(exist_ok=True)
    p = OUT / f"market_brief_{args.asof.replace('-', '')}.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print("->", p)


if __name__ == "__main__":
    main()
