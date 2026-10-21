"""ETF 全收益序列抓取（累计净值口径），供被动化 A/B 使用。

来源：天天基金 pingzhongdata JS 的 Data_ACWorthTrend（累计净值，已含分红与
份额折算调整）。时间戳按 Asia/Shanghai 归一（直接按 UTC 读会整体错一天，
日收益相关从 0.985 掉到 0.03——2026-10-21 实测）。

口径声明：cum_nav 为基金累计净值（分红不再投资）；reinvested 总收益会略高，
差异量级 <0.05pp/年。二级市场溢价/折价变动不在序列内。

用法：python scripts/fetch_etf_total_return.py [--check]
输出：data_store/meta/etf_total_return.parquet + _meta.json
"""
import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

CODES = {"510300": "沪深300ETF", "510500": "中证500ETF"}
OUT = Path("data_store/meta/etf_total_return.parquet")
META = Path("data_store/meta/etf_total_return_meta.json")
HEADERS = {"User-Agent": "Mozilla/5.0"}


def fetch(code: str) -> pd.DataFrame:
    url = f"https://fund.eastmoney.com/pingzhongdata/{code}.js"
    txt = requests.get(url, headers=HEADERS, timeout=30).text
    m = re.search(r"Data_ACWorthTrend\s*=\s*(\[.*?\]);", txt)
    if not m:
        raise RuntimeError(f"{code}: Data_ACWorthTrend 未找到")
    arr = json.loads(m.group(1))
    s = pd.Series({pd.Timestamp(x[0], unit="ms"): x[1] for x in arr}).sort_index()
    # JS 时间戳是北京时间零点，pandas 按 UTC 解析会前移一天
    s.index = (s.index.tz_localize("UTC")
               .tz_convert("Asia/Shanghai").tz_localize(None).normalize())
    s = pd.to_numeric(s, errors="coerce").dropna()
    return pd.DataFrame({"date": s.index, "cum_nav": s.values,
                         "code": code, "source": "eastmoney_pingzhongdata",
                         "basis": "cum_nav_total_return"})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="与库内价格做对齐/分红校验")
    args = ap.parse_args()

    frames = []
    for code in CODES:
        df = fetch(code)
        df["fetched_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        frames.append(df)
        print(f"{code} {CODES[code]}: {len(df)} 行 "
              f"{df['date'].min().date()}~{df['date'].max().date()}", flush=True)
    out = pd.concat(frames, ignore_index=True)
    out.to_parquet(OUT, index=False)
    META.write_text(json.dumps({
        "source": "eastmoney_pingzhongdata.Data_ACWorthTrend",
        "basis": "cum_nav_total_return",
        "note": "累计净值含分红/份额折算调整; 分红不再投资; 不含二级溢价变动",
        "codes": CODES,
        "as_of": str(out["date"].max().date()),
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"-> {OUT} / {META}")

    if args.check:
        from data.storage import load_daily
        for code in CODES:
            c = out[out["code"] == code].set_index("date")["cum_nav"]
            d = load_daily(code, "2019-01-01", "2026-10-19")
            d["date"] = pd.to_datetime(d["date"])
            p = d.set_index("date")["close"].astype(float)
            x = pd.concat([c.pct_change().rename("tr"),
                           p.pct_change().rename("px")], axis=1).dropna()
            corr = x["tr"].corr(x["px"])
            tr = c.loc["2019-01-02":]
            px = p.loc["2019-01-02":]
            tr_ann = (tr.iloc[-1] / tr.iloc[0]) ** (252 / len(tr)) - 1
            px_ann = (px.iloc[-1] / px.iloc[0]) ** (252 / len(px)) - 1
            print(f"{code} corr={corr:.4f} 全收益年化={tr_ann*100:.2f}% "
                  f"价格年化={px_ann*100:.2f}% 差={(tr_ann-px_ann)*100:.2f}pp")
            if corr < 0.95:
                print(f"{code} ALIGN_FAIL corr={corr:.3f} (<0.95)")
                return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
