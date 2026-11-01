"""拉取东财股权质押比例（akshare），按周五缓存（2026-11-01）。

用途：高质押回避 overlay（预登记
logs/research_registration_pledge_overlay_20261101.md）。
只作研究缓存，不进生产信号链。
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

CACHE_DIR = ROOT / "data_store" / "meta" / "pledge_ratio_em"
META = ROOT / "data_store" / "meta" / "pledge_ratio_em.meta.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2019-06-01")
    ap.add_argument("--end", default="2026-10-30")
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    import akshare as ak

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cal = load_meta("trade_calendar")
    dates = pd.to_datetime(cal["trade_date"]).dt.strftime("%Y-%m-%d")
    # 只用周五（东财质押快照通常周五）；保留全部周五，overlay 取最新
    dates = sorted(d for d in dates
                   if args.start <= d <= args.end
                   and pd.Timestamp(d).weekday() == 4)
    logger.info(f"fridays={len(dates)} cache={CACHE_DIR}")
    ok = skip = fail = 0
    for i, d in enumerate(dates, 1):
        p = CACHE_DIR / f"{d}.parquet"
        if p.exists() and not args.refresh:
            skip += 1
            continue
        try:
            x = ak.stock_gpzy_pledge_ratio_em(d.replace("-", ""))
            if x is None or x.empty:
                skip += 1
                continue
            x = x.copy()
            x["fetch_date"] = d
            x.to_parquet(p, index=False)
            ok += 1
        except Exception as e:
            fail += 1
            logger.warning(f"{d} fail {type(e).__name__}: {str(e)[:100]}")
        if i % 50 == 0:
            logger.info(f"{i}/{len(dates)} ok={ok} skip={skip} fail={fail}")
    META.write_text(json.dumps({
        "source": "akshare.stock_gpzy_pledge_ratio_em (Eastmoney)",
        "start": args.start, "end": args.end,
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "n_fridays": len(dates), "n_ok": ok, "n_skip": skip, "n_fail": fail,
        "note": "research cache only; not signal chain",
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info(f"done ok={ok} skip={skip} fail={fail}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
