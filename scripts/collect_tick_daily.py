"""每日 tick 快照采集（2026-10-20 建，只读，不下单）。

用途：为"盖板/托板/内外盘"前向检验积累五档盘口数据。
数据：QMT tick（约 3 秒快照），当日盘后采集。
落盘：H:\\quant\\data_store\\intraday\\tick\\{YYYYMMDD}.parquet
"""
import argparse
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

CODES = ['000001.SZ', '000027.SZ', '000050.SZ', '000088.SZ', '000166.SZ', '000400.SZ', '000425.SZ', '000528.SZ', '000559.SZ', '000596.SZ', '000625.SZ', '000657.SZ', '000708.SZ', '000728.SZ', '000738.SZ', '000776.SZ', '000792.SZ', '000830.SZ', '000883.SZ', '000898.SZ', '000938.SZ', '000963.SZ', '000983.SZ', '000999.SZ', '001286.SZ', '001391.SZ', '002001.SZ', '002027.SZ', '002049.SZ', '002065.SZ', '002120.SZ', '002138.SZ', '002155.SZ', '002195.SZ', '002230.SZ', '002252.SZ', '002266.SZ', '002299.SZ', '002318.SZ', '002353.SZ', '002407.SZ', '002415.SZ', '002430.SZ', '002460.SZ', '002466.SZ', '002493.SZ', '002532.SZ', '002594.SZ', '002603.SZ', '002648.SZ', '002709.SZ', '002739.SZ', '002812.SZ', '002841.SZ', '002920.SZ', '002945.SZ', '003022.SZ', '300001.SZ', '300014.SZ', '300033.SZ', '300059.SZ', '300122.SZ', '300142.SZ', '300223.SZ', '300308.SZ', '300373.SZ', '300395.SZ', '300432.SZ', '300454.SZ', '300476.SZ', '300502.SZ', '300570.SZ', '300627.SZ', '300676.SZ', '300718.SZ', '300750.SZ', '300760.SZ', '300857.SZ', '300919.SZ', '300999.SZ', '301269.SZ', '301358.SZ', '301536.SZ', '600000.SH', '600010.SH', '600018.SH', '600025.SH', '600029.SH', '600036.SH', '600050.SH', '600066.SH', '600098.SH', '600109.SH', '600126.SH', '600143.SH', '600160.SH', '600171.SH', '600188.SH', '600221.SH', '600282.SH', '600299.SH', '600329.SH', '600348.SH', '600369.SH', '600380.SH', '600406.SH', '600436.SH', '600483.SH', '600498.SH', '600516.SH', '600522.SH', '600547.SH', '600566.SH', '600583.SH', '600595.SH', '600602.SH', '600655.SH', '600685.SH', '600704.SH', '600741.SH', '600764.SH', '600803.SH', '600820.SH', '600863.SH', '600879.SH', '600887.SH', '600905.SH', '600919.SH', '600938.SH', '600968.SH', '600988.SH', '600999.SH', '601009.SH', '601019.SH', '601066.SH', '601099.SH', '601111.SH', '601127.SH', '601139.SH', '601166.SH', '601198.SH', '601225.SH', '601236.SH', '601318.SH', '601360.SH', '601399.SH', '601577.SH', '601607.SH', '601618.SH', '601665.SH', '601688.SH', '601699.SH', '601766.SH', '601808.SH', '601838.SH', '601868.SH', '601878.SH', '601898.SH', '601919.SH', '601966.SH', '601991.SH', '603000.SH', '603087.SH', '603156.SH', '603225.SH', '603260.SH', '603298.SH', '603345.SH', '603444.SH', '603565.SH', '603605.SH', '603659.SH', '603737.SH', '603806.SH', '603885.SH', '603939.SH', '605117.SH', '688002.SH', '688017.SH', '688036.SH', '688052.SH', '688099.SH', '688122.SH', '688172.SH', '688188.SH', '688220.SH', '688248.SH', '688278.SH', '688301.SH', '688322.SH', '688347.SH', '688375.SH', '688411.SH', '688475.SH', '688521.SH', '688568.SH', '688608.SH', '688676.SH', '688709.SH', '688778.SH']
OUT = Path(r"H:\\quant\\data_store\\intraday\\tick")
OUT.mkdir(parents=True, exist_ok=True)


def flat_list(v, n=5):
    a = list(v) if v is not None else []
    a = a + [np.nan] * (n - len(a))
    return a[:n]


def one(code, day):
    from xtquant import xtdata

    xtdata.download_history_data(code, period="tick", start_time=day)
    d = xtdata.get_market_data_ex([], [code], period="tick",
                                  start_time=day, count=-1)
    df = d.get(code)
    if df is None or len(df) == 0:
        return None
    df = df.reset_index()
    tm = df["time"].astype(str) if "time" in df.columns else df.iloc[:, 0].astype(str)
    out = pd.DataFrame({"code": code, "time": tm})
    for c in ("lastPrice", "lastClose", "open", "high", "low", "volume",
              "amount", "pvolume", "tickvol", "transactionNum"):
        out[c] = pd.to_numeric(df[c], errors="coerce") if c in df.columns else np.nan
    for i in range(5):
        out["a" + str(i + 1)] = [flat_list(x)[i] for x in df["askPrice"]] if "askPrice" in df.columns else np.nan
        out["av" + str(i + 1)] = [flat_list(x)[i] for x in df["askVol"]] if "askVol" in df.columns else np.nan
        out["b" + str(i + 1)] = [flat_list(x)[i] for x in df["bidPrice"]] if "bidPrice" in df.columns else np.nan
        out["bv" + str(i + 1)] = [flat_list(x)[i] for x in df["bidVol"]] if "bidVol" in df.columns else np.nan
    return out


def _worker(start: int, end: int, day: str) -> int:
    """单进程跑一段；每码成功立即落 part，硬崩后由父进程从下一个码重启。"""
    parts_dir = OUT / ".parts" / day
    parts_dir.mkdir(parents=True, exist_ok=True)
    cur_file = parts_dir / "current.json"
    err_file = parts_dir / "errors.json"
    errors = {}
    if err_file.exists():
        try:
            errors = json.loads(err_file.read_text(encoding="utf-8"))
        except Exception:
            errors = {}
    for i in range(start, end):
        code = CODES[i]
        cur_file.write_text(json.dumps({"index": i, "code": code},
                                       ensure_ascii=False), encoding="utf-8")
        part = parts_dir / f"{code}.parquet"
        try:
            x = one(code, day)
            if x is not None and len(x):
                x.to_parquet(part, index=False)
                errors.pop(code, None)
                print("OK", i, code, len(x), flush=True)
            else:
                errors[code] = "empty"
                print("EMPTY", i, code, flush=True)
        except Exception as e:  # noqa: BLE001
            errors[code] = str(e)[:120]
            print("ERR", i, code, str(e)[:120], flush=True)
        err_file.write_text(json.dumps(errors, ensure_ascii=False, indent=1),
                            encoding="utf-8")
    cur_file.unlink(missing_ok=True)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--worker", action="store_true", help="内部：单进程段")
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--end", type=int)
    ap.add_argument("--limit", type=int, help="调试：只跑前 N 个 code，写 _test")
    ap.add_argument("--day", default=date.today().strftime("%Y%m%d"))
    args = ap.parse_args()
    end = args.end if args.end is not None else (args.limit or len(CODES))
    if args.worker:
        return _worker(args.start, min(end, len(CODES)), args.day)

    day = args.day
    end = min(end, len(CODES))
    parts_dir = OUT / ".parts" / day
    parts_dir.mkdir(parents=True, exist_ok=True)
    for f in ("current.json", "errors.json"):
        (parts_dir / f).unlink(missing_ok=True)
    start, attempts, consecutive = 0, 0, 0
    errors = {}
    while start < end:
        cmd = [sys.executable, str(Path(__file__).resolve()),
               "--worker", "--start", str(start), "--end", str(end), "--day", day]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=1800)
            rc = r.returncode
        except Exception as e:  # noqa: BLE001
            rc, r = -1, None
            print("WORKER_TIMEOUT", start, str(e)[:120], flush=True)
        ef = parts_dir / "errors.json"
        if ef.exists():
            try:
                errors.update(json.loads(ef.read_text(encoding="utf-8")))
            except Exception:
                pass
        if rc == 0:
            break
        cur = None
        cf = parts_dir / "current.json"
        if cf.exists():
            try:
                cur = json.loads(cf.read_text(encoding="utf-8"))
            except Exception:
                cur = None
        if cur and start <= int(cur.get("index", -1)) < end:
            bad = CODES[int(cur["index"])]
            errors.setdefault(bad, f"worker exit {rc}")
            print("CRASH_SKIP", cur["index"], bad, flush=True)
            start = int(cur["index"]) + 1
            consecutive += 1
        else:
            errors.setdefault(CODES[start], f"worker exit {rc}")
            start += 1
            consecutive += 1
        attempts += 1
        # 连续 3 个码硬崩：判为交易所级坏源，跳过剩余，保留已采部分（健康行可见）
        if consecutive >= 3:
            for c in CODES[start:end]:
                errors.setdefault(c, "skipped_after_3_consecutive_crashes")
            print("EXCHANGE_LEVEL_SKIP", start, flush=True)
            break
        if attempts > 20:
            print("TOO_MANY_ATTEMPTS", start, flush=True)
            break

    ok = [c for c in CODES[:end] if (parts_dir / f"{c}.parquet").exists()]
    if not ok:
        print("NOTHING", json.dumps(errors, ensure_ascii=False)[:500], flush=True)
        return 1
    allx = pd.concat([pd.read_parquet(parts_dir / f"{c}.parquet") for c in ok],
                     ignore_index=True)
    suffix = "_test" if args.limit and not args.worker else ""
    path = OUT / (day + suffix + ".parquet")
    allx.to_parquet(path, index=False)
    cov = {"date": day, "requested": end, "ok": len(ok),
           "skipped": [], "errors": errors, "rows": len(allx),
           "mode": "supervised_single_process"}
    (OUT / (day + suffix + "_coverage.json")).write_text(
        json.dumps(cov, ensure_ascii=False, indent=1), encoding="utf-8")
    print("DONE", json.dumps(cov, ensure_ascii=False), "->", path, flush=True)
    return 0


if __name__ == "__main__":
    main()
