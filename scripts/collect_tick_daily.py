"""每日 tick 快照采集（2026-10-20 建，只读，不下单）。

用途：为"盖板/托板/内外盘"前向检验积累五档盘口数据。
数据：QMT tick（约 3 秒快照），当日盘后采集。
落盘：H:\\quant\\data_store\\intraday\\tick\\{YYYYMMDD}.parquet
"""
import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from xtquant import xtdata

CODES = ['000001.SZ', '000027.SZ', '000050.SZ', '000088.SZ', '000166.SZ', '000400.SZ', '000425.SZ', '000528.SZ', '000559.SZ', '000596.SZ', '000625.SZ', '000657.SZ', '000708.SZ', '000728.SZ', '000738.SZ', '000776.SZ', '000792.SZ', '000830.SZ', '000883.SZ', '000898.SZ', '000938.SZ', '000963.SZ', '000983.SZ', '000999.SZ', '001286.SZ', '001391.SZ', '002001.SZ', '002027.SZ', '002049.SZ', '002065.SZ', '002120.SZ', '002138.SZ', '002155.SZ', '002195.SZ', '002230.SZ', '002252.SZ', '002266.SZ', '002299.SZ', '002318.SZ', '002353.SZ', '002407.SZ', '002415.SZ', '002430.SZ', '002460.SZ', '002466.SZ', '002493.SZ', '002532.SZ', '002594.SZ', '002603.SZ', '002648.SZ', '002709.SZ', '002739.SZ', '002812.SZ', '002841.SZ', '002920.SZ', '002945.SZ', '003022.SZ', '300001.SZ', '300014.SZ', '300033.SZ', '300059.SZ', '300122.SZ', '300142.SZ', '300223.SZ', '300308.SZ', '300373.SZ', '300395.SZ', '300432.SZ', '300454.SZ', '300476.SZ', '300502.SZ', '300570.SZ', '300627.SZ', '300676.SZ', '300718.SZ', '300750.SZ', '300760.SZ', '300857.SZ', '300919.SZ', '300999.SZ', '301269.SZ', '301358.SZ', '301536.SZ', '600000.SH', '600010.SH', '600018.SH', '600025.SH', '600029.SH', '600036.SH', '600050.SH', '600066.SH', '600098.SH', '600109.SH', '600126.SH', '600143.SH', '600160.SH', '600171.SH', '600188.SH', '600221.SH', '600282.SH', '600299.SH', '600329.SH', '600348.SH', '600369.SH', '600380.SH', '600406.SH', '600436.SH', '600483.SH', '600498.SH', '600516.SH', '600522.SH', '600547.SH', '600566.SH', '600583.SH', '600595.SH', '600602.SH', '600655.SH', '600685.SH', '600704.SH', '600741.SH', '600764.SH', '600803.SH', '600820.SH', '600863.SH', '600879.SH', '600887.SH', '600905.SH', '600919.SH', '600938.SH', '600968.SH', '600988.SH', '600999.SH', '601009.SH', '601019.SH', '601066.SH', '601099.SH', '601111.SH', '601127.SH', '601139.SH', '601166.SH', '601198.SH', '601225.SH', '601236.SH', '601318.SH', '601360.SH', '601399.SH', '601577.SH', '601607.SH', '601618.SH', '601665.SH', '601688.SH', '601699.SH', '601766.SH', '601808.SH', '601838.SH', '601868.SH', '601878.SH', '601898.SH', '601919.SH', '601966.SH', '601991.SH', '603000.SH', '603087.SH', '603156.SH', '603225.SH', '603260.SH', '603298.SH', '603345.SH', '603444.SH', '603565.SH', '603605.SH', '603659.SH', '603737.SH', '603806.SH', '603885.SH', '603939.SH', '605117.SH', '688002.SH', '688017.SH', '688036.SH', '688052.SH', '688099.SH', '688122.SH', '688172.SH', '688188.SH', '688220.SH', '688248.SH', '688278.SH', '688301.SH', '688322.SH', '688347.SH', '688375.SH', '688411.SH', '688475.SH', '688521.SH', '688568.SH', '688608.SH', '688676.SH', '688709.SH', '688778.SH']
OUT = Path(r"H:\\quant\\data_store\\intraday\\tick")
OUT.mkdir(parents=True, exist_ok=True)


def flat_list(v, n=5):
    a = list(v) if v is not None else []
    a = a + [np.nan] * (n - len(a))
    return a[:n]


def one(code, day):
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


def main():
    day = date.today().strftime("%Y%m%d")
    parts, errors = [], {}
    for i, code in enumerate(CODES, 1):
        try:
            x = one(code, day)
            if x is not None and len(x):
                parts.append(x)
        except Exception as e:
            errors[code] = str(e)[:120]
        if i % 20 == 0:
            print("progress", i, "ok", len(parts), flush=True)
    if not parts:
        print("NOTHING", json.dumps(errors)[:500])
        return
    allx = pd.concat(parts, ignore_index=True)
    path = OUT / (day + ".parquet")
    allx.to_parquet(path, index=False)
    print("DONE", day, "codes", len(parts), "rows", len(allx),
          "errors", len(errors), "->", path, flush=True)


if __name__ == "__main__":
    main()
