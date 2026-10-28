"""重建 2019-2026 全 A 日度广度面板（H-BREADTH 数据底座）。

口径：每个交易日，用全部有 pct_chg 的 A 股日线文件计算
n / 上涨占比 / 中位 / 均值 / 跌超1%占比。包含停牌/ST/新股（与
`market_daily_study` 一致），不做行业/市值过滤。

用法: python scripts/breadth_panel_build.py [--start 2019-01-01] [--end 2026-09-30]
"""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from loguru import logger

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "logs" / "breadth_panel_2019_2026.parquet"
META = ROOT / "logs" / "breadth_panel_2019_2026_meta.json"


def build_year(year: int, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    frames = []
    for p in sorted(Path(f"/data/quant_data_store/daily/{year}").glob("*.parquet")):
        code = p.stem
        if not (len(code) == 6 and code.isdigit()):
            continue
        try:
            d = pd.read_parquet(p, columns=["date", "pct_chg"])
        except Exception:
            continue
        if "pct_chg" not in d.columns:
            continue
        d["date"] = pd.to_datetime(d["date"], errors="coerce")
        d["pct_chg"] = pd.to_numeric(d["pct_chg"], errors="coerce")
        d = d[(d["date"] >= start) & (d["date"] <= end)].dropna()
        if len(d):
            frames.append(d[["date", "pct_chg"]])
    if not frames:
        return pd.DataFrame()
    d = pd.concat(frames, ignore_index=True)
    g = d.groupby("date")["pct_chg"]
    out = pd.DataFrame({
        "n": g.size(),
        "up_pct": g.apply(lambda s: float((s > 0).mean() * 100)),
        "median_pct": g.median(),
        "mean_pct": g.mean(),
        "lt_neg1_pct": g.apply(lambda s: float((s < -1).mean() * 100)),
    }).reset_index()
    logger.info(f"{year}: {len(out)} 日, {len(d)} 行")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2019-01-01")
    ap.add_argument("--end", default="2026-09-30")
    args = ap.parse_args()
    start, end = pd.Timestamp(args.start), pd.Timestamp(args.end)
    parts = []
    for yr in range(start.year, end.year + 1):
        p = build_year(yr, start, end)
        if len(p):
            parts.append(p)
    if not parts:
        raise SystemExit("no breadth data")
    out = pd.concat(parts, ignore_index=True).sort_values("date")
    out.to_parquet(OUT, index=False)
    meta = {"as_of": str(out.date.max().date()), "start": str(out.date.min().date()),
            "n_days": int(len(out)), "source": "data_store/daily pct_chg",
            "universe": "all 6-digit A-share files, incl. ST/suspended/new",
            "note": "broad breadth; no industry/size filter"}
    META.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info(f"breadth: {len(out)} 日 → {OUT}; as_of={meta['as_of']}")


if __name__ == "__main__":
    main()
