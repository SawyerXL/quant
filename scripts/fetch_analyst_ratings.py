"""拉取 CNINFO 分析师评级数据（akshare），按交易日缓存（2026-11-01）。

用途：分析师评级变化/首次覆盖事件研究（预登记见
logs/research_registration_analyst_rating_20261101.md）。
只作研究缓存，不进生产信号链。缓存目录 data_store/meta/analyst_ratings_cninfo/。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
from loguru import logger

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from data.storage import load_meta

CACHE_DIR = ROOT / "data_store" / "meta" / "analyst_ratings_cninfo"
META = ROOT / "data_store" / "meta" / "analyst_ratings_cninfo.meta.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2019-01-01")
    ap.add_argument("--end", default="2026-10-30")
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    import akshare as ak

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cal = load_meta("trade_calendar")
    dates = pd.to_datetime(cal["trade_date"]).dt.strftime("%Y-%m-%d")
    dates = sorted(d for d in dates if args.start <= d <= args.end)
    logger.info(f"dates={len(dates)} cache={CACHE_DIR}")
    n_ok = n_skip = n_fail = 0
    for i, d in enumerate(dates, 1):
        p = CACHE_DIR / f"{d}.parquet"
        if p.exists() and not args.refresh:
            n_skip += 1
            continue
        try:
            x = ak.stock_rank_forecast_cninfo(d.replace("-", ""))
            if x is None or x.empty:
                n_skip += 1
                continue
            x = x.copy()
            x["fetch_date"] = d
            x.to_parquet(p, index=False)
            n_ok += 1
        except Exception as e:
            n_fail += 1
            logger.warning(f"{d} fetch fail: {type(e).__name__}: {e}")
        if i % 100 == 0:
            logger.info(f"{i}/{len(dates)} ok={n_ok} skip={n_skip} fail={n_fail}")
    META.write_text(json.dumps({
        "source": "akshare.stock_rank_forecast_cninfo (CNINFO)",
        "start": args.start, "end": args.end,
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "n_dates": len(dates), "n_ok": n_ok, "n_skip": n_skip, "n_fail": n_fail,
        "note": "research cache only; not signal chain",
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info(f"done ok={n_ok} skip={n_skip} fail={n_fail}")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
