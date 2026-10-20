"""抓取限售解禁 / 大宗交易事件并落盘(2026-10-19, #21)。

why: 事件研究必须先冻结原始数据(as_of), 否则重跑不可复现(闸门十三)。
本脚本只抓取/规范化/计数, 不算任何收益。
预注册: logs/prereg_unlock_block_20261019.md
用法:
  python scripts/fetch_unlock_block_events.py --start 2019-01-01 --end 2026-09-30
"""
import argparse
import time
from datetime import date
from pathlib import Path

import pandas as pd
from loguru import logger

logger.remove()

META = Path("/root/quant/data_store/meta")
SLEEP = 0.4
RETRIES = 3


def month_ranges(start: str, end: str):
    s = pd.Timestamp(start); e = pd.Timestamp(end)
    cur = pd.Timestamp(year=s.year, month=s.month, day=1)
    while cur <= e:
        nxt = cur + pd.offsets.MonthBegin(1)
        lo = max(cur, s).strftime("%Y%m%d")
        hi = min(nxt - pd.Timedelta(days=1), e).strftime("%Y%m%d")
        yield lo, hi
        cur = nxt


def fetch(fn, lo, hi, label):
    last = None
    for k in range(RETRIES):
        try:
            d = fn(start_date=lo, end_date=hi)
            if d is None:
                d = pd.DataFrame()
            return d
        except Exception as e:
            last = e
            time.sleep(1.5 * (k + 1))
    logger.warning(f"{label} {lo}-{hi} 抓取失败: {type(last).__name__}: {last}")
    return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--start", default="2019-01-01")
    p.add_argument("--end", default="2026-09-30")
    p.add_argument("--out-dir", default=str(META))
    args = p.parse_args()
    out = Path(args.out_dir); out.mkdir(parents=True, exist_ok=True)
    as_of = date.today().isoformat()

    import akshare as ak
    unlock_frames, block_frames = [], []
    failed = []
    for lo, hi in month_ranges(args.start, args.end):
        u = fetch(ak.stock_restricted_release_detail_em, lo, hi, "unlock")
        if u is None:
            failed.append(("unlock", lo, hi))
        elif not u.empty:
            unlock_frames.append(u)
        time.sleep(SLEEP)
        b = fetch(ak.stock_dzjy_mrtj, lo, hi, "block")
        if b is None:
            failed.append(("block", lo, hi))
        elif not b.empty:
            block_frames.append(b)
        time.sleep(SLEEP)
        logger.info(f"{lo}-{hi}: unlock={0 if u is None else len(u)} "
                    f"block={0 if b is None else len(b)}")

    if not unlock_frames or not block_frames:
        raise SystemExit("事件抓取为空, 终止(不落盘半成品)")

    u = pd.concat(unlock_frames, ignore_index=True)
    u["股票代码"] = u["股票代码"].astype(str).str.zfill(6)
    u["解禁时间"] = pd.to_datetime(u["解禁时间"], errors="coerce")
    for c in ["解禁数量", "实际解禁数量", "实际解禁市值",
              "占解禁前流通市值比例", "解禁前一交易日收盘价"]:
        u[c] = pd.to_numeric(u[c], errors="coerce")
    u = u.dropna(subset=["股票代码", "解禁时间", "占解禁前流通市值比例"])
    u["as_of"] = as_of
    u = u.sort_values(["解禁时间", "股票代码"]).reset_index(drop=True)

    b = pd.concat(block_frames, ignore_index=True)
    b["证券代码"] = b["证券代码"].astype(str).str.zfill(6)
    b["交易日期"] = pd.to_datetime(b["交易日期"], errors="coerce")
    for c in ["成交价", "成交总量", "成交总额", "折溢率", "成交总额/流通市值"]:
        b[c] = pd.to_numeric(b[c], errors="coerce")
    b = b.dropna(subset=["证券代码", "交易日期", "成交价", "折溢率"])
    b["as_of"] = as_of
    b = b.sort_values(["交易日期", "证券代码"]).reset_index(drop=True)

    up = out / "unlock_events_2019_2026.parquet"
    bp = out / "block_trade_events_2019_2026.parquet"
    u.to_parquet(up, index=False)
    b.to_parquet(bp, index=False)
    print(f"unlock {len(u)} rows {u['解禁时间'].min().date()}~{u['解禁时间'].max().date()} -> {up}")
    print(f"block  {len(b)} rows {b['交易日期'].min().date()}~{b['交易日期'].max().date()} -> {bp}")
    print(f"as_of={as_of} failed_months={failed}")
    print(f"dups unlock={u.duplicated(['股票代码','解禁时间']).sum()} "
          f"block={b.duplicated(['证券代码','交易日期']).sum()}")


if __name__ == "__main__":
    main()
