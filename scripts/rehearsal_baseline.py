"""预演/考试基线拷贝: 快照重建前另存快照+哈希+行数+逐月分布(10-12 06:25 cron 或手动)。

实现 runbook 已锁定条款"06:30 重建前先拷贝 cb_snapshots.parquet 为预演基线
(另存路径+哈希)——毛口径 diff 的对端"。产出:
  logs/cb_snapshots_baseline_{date}.parquet + logs/cb_snapshots_baseline_{date}.json
用法: python scripts/rehearsal_baseline.py --date 2026-10-12
"""
import argparse
import hashlib
import json
import shutil
from pathlib import Path

import pandas as pd

SNAP = Path("/data/quant_data_store/meta/cb_snapshots.parquet")
LOG_DIR = Path("/root/quant/logs")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="YYYY-MM-DD, 基线标签(写入文件名)")
    args = ap.parse_args()

    if not SNAP.exists():
        raise SystemExit(f"快照文件不存在: {SNAP}")
    df = pd.read_parquet(SNAP)
    df["sd"] = pd.to_datetime(df["snap_date"]).dt.strftime("%Y-%m-%d")
    meta = {
        "date": args.date,
        "source": str(SNAP),
        "sha256": hashlib.sha256(SNAP.read_bytes()).hexdigest(),
        "rows": int(len(df)),
        "max_snap_date": df["sd"].max(),
        "per_month": {k: int(v) for k, v in df.groupby("sd").size().items()},
    }
    out_meta = LOG_DIR / f"cb_snapshots_baseline_{args.date}.json"
    out_parquet = LOG_DIR / f"cb_snapshots_baseline_{args.date}.parquet"
    out_meta.write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    shutil.copy2(SNAP, out_parquet)
    print(json.dumps(meta, ensure_ascii=False, indent=1))
    print(f"基线已保存: {out_parquet} / {out_meta}")


if __name__ == "__main__":
    main()
