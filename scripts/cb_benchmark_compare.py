"""CB 腿多基准比较：CB 指数(价格) / 511380 / 511180 / 指数+票息代理。

为什么：此前只拿 `cb_index`（价格口径、全市场指数）算“相对 +10pp”，
但指数不可交易、宇宙不同。可交易 CB ETF 更接近实盘机会集；
同时给指数加票息代理，避免把“价格口径”误读成全收益。
本脚本只做比较，不改策略/执行。

口径：
- live: `logs/qmt_nav_history.parquet` 的 nav_cb（QMT 仿真官方快照）
- 511380/511180: akshare `fund_etf_hist_sina` 的 close（价格口径，非 NAV/分红）
- CB 指数: `data_store/meta/cb_index.parquet` 的 close（价格口径）
- 票息代理: 指数日收益 + COUPON/252（默认 1.5%/年，**假设值，非真值**）
- 共同交易日、起点归一；beta/alpha 用日收益 OLS（alpha 年化）
产出：logs/cb_benchmark_20261031.json / .md
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import akshare as ak
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
NAV = ROOT / "logs" / "qmt_nav_history.parquet"
CB_INDEX = ROOT / "data_store" / "meta" / "cb_index.parquet"
ETF_DIR = ROOT / "data_store" / "meta"
OUT_JSON = ROOT / "logs" / "cb_benchmark_20261031.json"
OUT_MD = ROOT / "logs" / "cb_benchmark_20261031.md"
COUPON = 0.015
START = "2026-07-20"


def fetch_etf(symbol: str) -> pd.Series:
    """akshare 新浪 ETF 日线 close；本地缓存避免每次联网。"""
    cache = ETF_DIR / f"cb_etf_{symbol}.parquet"
    meta = ETF_DIR / f"cb_etf_{symbol}_meta.json"
    if cache.exists():
        d = pd.read_parquet(cache)
    else:
        d = ak.fund_etf_hist_sina(symbol=symbol)
        d["date"] = pd.to_datetime(d["date"])
        d = d[["date", "close"]].sort_values("date")
        d.to_parquet(cache, index=False)
        meta.write_text(json.dumps({
            "source": "akshare.fund_etf_hist_sina",
            "symbol": symbol,
            "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "return_basis": "price_close",
            "note": "ETF market price, not NAV/total return",
        }, ensure_ascii=False, indent=1), encoding="utf-8")
    d["date"] = pd.to_datetime(d["date"])
    return pd.to_numeric(d.set_index("date")["close"], errors="coerce").dropna()


def total_return_proxy(close: pd.Series, coupon: float = COUPON) -> pd.Series:
    """价格指数 + 每日票息 accrual 的近似全收益；假设值，不是真值。"""
    c = pd.to_numeric(close, errors="coerce").dropna().astype(float)
    if c.empty:
        return c
    ret = c.pct_change().fillna(0.0) + coupon / 252.0
    if len(ret):
        ret.iloc[0] = 0.0  # 起点=1.0，票息从第二个观测开始累计
    return (1 + ret).cumprod()


def compare(live: pd.Series, bench: pd.Series, start: str = START) -> dict:
    j = pd.concat([live.rename("live"), bench.rename("bench")],
                  axis=1, join="inner").loc[start:].dropna()
    if len(j) < 2:
        return {"n": len(j), "error": "共同交易日不足"}
    j = j / j.iloc[0]
    lr = j["live"].pct_change().dropna()
    br = j["bench"].pct_change().dropna()
    beta = alpha = corr = None
    if len(lr) > 2 and float(br.var()) > 0:
        cov = np.cov(lr, br, ddof=1)
        beta = float(cov[0, 1] / cov[1, 1])
        alpha = float((lr.mean() - beta * br.mean()) * 252)
        corr = float(lr.corr(br))
    return {
        "n": len(j),
        "start": str(j.index[0].date()),
        "end": str(j.index[-1].date()),
        "live_cum_pct": round(float(j["live"].iloc[-1] - 1) * 100, 3),
        "bench_cum_pct": round(float(j["bench"].iloc[-1] - 1) * 100, 3),
        "relative_pp": round(float(j["live"].iloc[-1] - j["bench"].iloc[-1]) * 100, 3),
        "beta": round(beta, 3) if beta is not None else None,
        "alpha_ann_pct": round(alpha * 100, 3) if alpha is not None else None,
        "corr": round(corr, 3) if corr is not None else None,
    }


def main() -> int:
    live_df = pd.read_parquet(NAV)
    live_df["date"] = pd.to_datetime(live_df["date"])
    live = pd.to_numeric(live_df.set_index("date")["nav_cb"], errors="coerce").dropna()

    idx_raw = pd.read_parquet(CB_INDEX)
    idx_raw["date"] = pd.to_datetime(idx_raw["date"])
    idx = pd.to_numeric(idx_raw.set_index("date")["close"], errors="coerce").dropna()
    idx_tr = total_return_proxy(idx)

    etfs = {s: fetch_etf(s) for s in ("sh511380", "sh511180")}
    out = {
        "as_of": "2026-10-27",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "live_source": "logs/qmt_nav_history.parquet nav_cb",
        "coupon_assumption": COUPON,
        "benchmarks": {
            "cb_index_price": {"basis": "price_close", **compare(live, idx)},
            "cb_index_total_proxy": {
                "basis": f"price + coupon {COUPON:.1%}/yr proxy",
                **compare(live, idx_tr),
            },
            "etf_511380": {"basis": "ETF market price", **compare(live, etfs["sh511380"])},
            "etf_511180": {"basis": "ETF market price", **compare(live, etfs["sh511180"])},
        },
        "caveats": [
            "ETF close 是市场价，不是 NAV/总收益；分红未计入。",
            "CB 指数是价格口径、全市场宇宙；策略 25 只 double-low，宇宙不同。",
            "票息代理 1.5%/年 是假设值，不是真值；只用于量级。",
            "窗口 2026-07-20→10-27（指数到 10-23），单路径、短样本、无 OOS。",
            "PIT 同宇宙基准仍未建立；本表不能当 alpha 定论。",
        ],
    }
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    rows = []
    for name, b in out["benchmarks"].items():
        rows.append(f"| {name} | {b.get('bench_cum_pct')}% | {b.get('relative_pp')}pp | "
                    f"{b.get('beta')} | {b.get('alpha_ann_pct')}% | {b.get('corr')} |")
    md = (
        "# CB 腿多基准比较（2026-10-31）\n\n"
        f"- live CB 腿 as_of 2026-10-27；窗口 {START} 起；票息代理假设 {COUPON:.1%}/年。\n\n"
        "| 基准 | 基准累计 | 相对 live | beta | alpha(年化) | corr |\n"
        "|---|---:|---:|---:|---:|---:|\n" + "\n".join(rows) + "\n\n"
        + "\n".join(f"- {x}" for x in out["caveats"]) + "\n"
    )
    OUT_MD.write_text(md, encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
