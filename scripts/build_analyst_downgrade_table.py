"""把 CNINFO 分析师缓存压成“调低”事件表（2026-11-01）。

信号链只需要 date/code；不在盘中重读 1,800 个 parquet。
输出 data_store/meta/analyst_downgrades.parquet；只读研究/可选 overlay。
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

from analyst_rating_event import classify_events, load_ratings

OUT = ROOT / "data_store" / "meta" / "analyst_downgrades.parquet"
META = ROOT / "data_store" / "meta" / "analyst_downgrades.meta.json"


def main() -> int:
    raw = load_ratings()
    ev = classify_events(raw)
    neg = ev[ev["kind"] == "negative"][["date", "code"]].drop_duplicates()
    neg = neg.sort_values(["date", "code"]).reset_index(drop=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    neg.to_parquet(OUT, index=False)
    META.write_text(json.dumps({
        "source": "CNINFO analyst ratings via akshare cache",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "rows": len(neg),
        "min_date": str(neg["date"].min()) if len(neg) else None,
        "max_date": str(neg["date"].max()) if len(neg) else None,
        "note": "research overlay input; default off in production",
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {OUT} rows={len(neg)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
