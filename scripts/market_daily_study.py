"""A股每日研究快照(2026-10-16 立项, 长期研究学习轨道)。

产出 logs/market_daily_study.csv 一行/交易日, 全部为机器值:
  指数层: 上证/中证800/科创50 的 close/pct/MA20/60/200 距离/20日波动/5日20日收益
  宽度层: 全市场 close>MA20 占比、涨跌中位数/均值/上涨占比/跌超1%占比、
          涨停/跌停家数(板块感知)
  背离层: 上证-中证800 当日差、上证方向与中位股票方向是否背离
用途: 给每日研究/预测台账提供特征底稿; 不喂信号链(架构原则)。
运行: python scripts/market_daily_study.py [--date 2026-10-16] [--limit N]
"""
import argparse
import sys
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from data.storage import load_meta  # noqa

DATA = Path("/data/quant_data_store")
OUT = Path("/root/quant/logs/market_daily_study.csv")
INDICES = {"sh000001": "SH000001", "csi800": "SH000906", "star50": "SH000688"}

# 板块涨跌停幅度(与 trader._BOARD_BANDS 同源)
_BOARD_BANDS = [("60", 0.10), ("00", 0.10), ("30", 0.20), ("68", 0.20),
                ("8", 0.30), ("4", 0.30), ("92", 0.30)]


def _load_index(code: str) -> pd.DataFrame:
    frames = []
    for yr in range(2018, 2027):
        p = DATA / "daily" / str(yr) / f"{code}.parquet"
        if p.exists():
            frames.append(pd.read_parquet(p))
    if not frames:
        return pd.DataFrame()
    d = pd.concat(frames, ignore_index=True).drop_duplicates("date")
    d["date"] = pd.to_datetime(d["date"])
    return d.sort_values("date").reset_index(drop=True)


def _index_features(code: str, asof: str) -> dict:
    d = _load_index(code)
    if d.empty:
        return {}
    d = d[d["date"] <= pd.Timestamp(asof)]
    c = pd.to_numeric(d["close"], errors="coerce")
    out = {"close": round(float(c.iloc[-1]), 2),
           "pct": round(float(c.iloc[-1] / c.iloc[-2] - 1) * 100, 2) if len(c) > 1 else None}
    for n in (20, 60, 200):
        ma = c.rolling(n).mean().iloc[-1]
        out[f"ma{n}_dist_pct"] = round(float(c.iloc[-1] / ma - 1) * 100, 2) if pd.notna(ma) else None
    r = c.pct_change()
    out["vol20_ann"] = round(float(r.rolling(20).std().iloc[-1]) * np.sqrt(252) * 100, 1) if len(r) >= 21 else None
    for n in (5, 20):
        out[f"ret{n}"] = round(float(c.iloc[-1] / c.iloc[-1 - n] - 1) * 100, 2) if len(c) > n else None
    return out


def _breadth(asof: str, limit: int | None = None) -> dict:
    """全市场宽度: close>MA20 占比 + 板块感知涨跌停家数。"""
    info = load_meta("stock_info_full")
    if info.empty:
        return {}
    is_st = ({str(c) for c, s in zip(info["code"], info["is_st"]) if s}
             if "is_st" in info.columns else set())
    codes = [str(c) for c in info["code"].tolist()]
    if limit:
        codes = codes[:limit]
    above20 = total = 0
    up_limit = down_limit = 0
    pcts: list[float] = []
    for code in codes:
        p = DATA / "daily" / "2026" / f"{code}.parquet"
        if not p.exists():
            continue
        try:
            d = pd.read_parquet(p, columns=["date", "close", "pct_chg"])
        except Exception:
            continue
        if d.empty:
            continue
        d["date"] = pd.to_datetime(d["date"])
        d = d[d["date"] <= pd.Timestamp(asof)].sort_values("date")
        if len(d) < 21:
            continue
        c = pd.to_numeric(d["close"], errors="coerce")
        if pd.isna(c.iloc[-1]):
            continue
        total += 1
        if c.iloc[-1] > c.rolling(20).mean().iloc[-1]:
            above20 += 1
        pct = pd.to_numeric(d["pct_chg"], errors="coerce").iloc[-1] if "pct_chg" in d.columns else np.nan
        if pd.notna(pct):
            pcts.append(float(pct))
            band = 0.05 if code in is_st else next(
                (b for pfx, b in _BOARD_BANDS if code.startswith(pfx)), 0.10)
            if pct >= band * 100 - 0.6:
                up_limit += 1
            elif pct <= -band * 100 + 0.6:
                down_limit += 1
    n = len(pcts)
    return {"breadth_n": total,
            "breadth_ma20_pct": round(above20 / total * 100, 1) if total else None,
            "breadth_pct_n": n,
            "breadth_up_pct": round(sum(1 for x in pcts if x > 0) / n * 100, 1) if n else None,
            "breadth_median_pct": round(float(np.median(pcts)), 2) if n else None,
            "breadth_mean_pct": round(float(np.mean(pcts)), 2) if n else None,
            "breadth_lt_neg1_pct": round(sum(1 for x in pcts if x < -1) / n * 100, 1) if n else None,
            "up_limit": up_limit, "down_limit": down_limit}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=date.today().isoformat())
    ap.add_argument("--limit", type=int, help="仅扫描前 N 只(调试用)")
    ap.add_argument("--no-breadth", action="store_true")
    args = ap.parse_args()

    # 非交易日不落行(否则假日会把上一交易日的涨跌贴上假日日期)
    cal = load_meta("trade_calendar")
    if not cal.empty and args.date not in set(cal["trade_date"].astype(str)):
        print(f"{args.date} 非交易日, 跳过")
        return

    row = {"date": args.date, "captured_at": datetime.now().isoformat(timespec="seconds")}
    for label, code in INDICES.items():
        feats = _index_features(code, args.date)
        for k, v in feats.items():
            row[f"{label}_{k}"] = v
    if not args.no_breadth:
        row.update(_breadth(args.date, args.limit))

    # 指数层与广度层分家的显式标记；预测对账必须同时看两列(2026-10-26)
    sh_pct, cb_pct = row.get("sh000001_pct"), row.get("csi800_pct")
    if sh_pct is not None and cb_pct is not None:
        row["sh_minus_csi800_pct"] = round(sh_pct - cb_pct, 2)
    med = row.get("breadth_median_pct")
    if sh_pct is not None and med is not None:
        row["breadth_divergence"] = int((sh_pct > 0) != (med > 0))

    df = pd.DataFrame([row])
    if OUT.exists():
        old = pd.read_csv(OUT)
        old = old[old["date"] != args.date]
        df = pd.concat([old, df], ignore_index=True)
    df = df.sort_values("date")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT, index=False)
    print(df.tail(1).T.to_string(header=False))


if __name__ == "__main__":
    main()
