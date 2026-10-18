"""H2-v2 数据下载: 2011-2025 日线(含退市), PIT 宇宙 ∪ 逐只落盘。

口径见 logs/study_registration_longhold_v2.md(先于下载登记)。
可重入: 已存在且非空的文件跳过(--force 覆盖)。
运行(低优先级):
  timeout 4h nice -n 10 ionice -c2 -n7 /root/quant/.venv/bin/python \
      scripts/fetch_baostock_long.py
"""
import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

import baostock as bs
import pandas as pd

OUT = Path("/data/quant_data_store/baostock_long/daily")
FORMATIONS = ["2012-12-31", "2013-12-31", "2014-12-31", "2015-12-31",
              "2016-12-31", "2017-12-29", "2018-12-28", "2019-12-31",
              "2020-12-31", "2021-12-31"]
FIELDS = "date,code,close,volume,amount,turn,pbMRQ,isST"
FALLBACK_DAYS = {"2017-12-29", "2018-12-28"}


def is_a_share(code: str) -> bool:
    if code.startswith(("sh.60", "sh.68", "sz.00", "sz.30")):
        return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    lg = bs.login()
    if lg.error_code != "0":
        print("登录失败:", lg.error_msg)
        sys.exit(1)

    # 1. PIT 宇宙并集(含后来退市的股票)
    codes = set()
    for day in FORMATIONS:
        got = None
        for d in (day, day.replace("-12-31", "-12-30"), day.replace("-12-31", "-12-29")):
            rs = bs.query_all_stock(day=d)
            rows = []
            while (rs.error_code == "0") and rs.next():
                rows.append(rs.get_row_data())
            if rows:
                got = rows
                print(f"  universe {day}: {len(rows)} (day={d})", flush=True)
                break
        if not got:
            print(f"  universe {day}: 空", flush=True)
            continue
        for r in got:
            c = r[0]
            if is_a_share(c):
                codes.add(c)
    codes = sorted(codes)
    print(f"PIT 并集: {len(codes)} 只", flush=True)

    # 2. 逐只下载
    t0 = datetime.now()
    done = skipped = failed = 0
    for i, c in enumerate(codes):
        if args.limit and i >= args.limit:
            break
        out = OUT / f"{c.split('.')[1]}.parquet"
        if out.exists() and out.stat().st_size > 0 and not args.force:
            skipped += 1
            continue
        for attempt in range(3):
            try:
                rs = bs.query_history_k_data_plus(
                    c, FIELDS, start_date="2011-01-01",
                    end_date="2025-12-31", frequency="d", adjustflag="3")
                rows = []
                while (rs.error_code == "0") and rs.next():
                    rows.append(rs.get_row_data())
                if rows:
                    df = pd.DataFrame(rows, columns=FIELDS.split(","))
                    df.to_parquet(out, index=False)
                    done += 1
                else:
                    failed += 1
                break
            except Exception as e:
                if attempt == 2:
                    failed += 1
                    print(f"  FAIL {c}: {e}", flush=True)
                else:
                    time.sleep(1 + attempt)
        if (i + 1) % 200 == 0:
            print(f"  {i+1}/{len(codes)} done={done} skip={skipped} fail={failed} "
                  f"{datetime.now() - t0}", flush=True)

    bs.logout()
    print(f"完成: 下载 {done} / 跳过 {skipped} / 失败 {failed} / 并集 {len(codes)} "
          f"| 耗时 {datetime.now() - t0}")


if __name__ == "__main__":
    main()
