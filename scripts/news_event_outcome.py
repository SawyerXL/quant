"""消息事件结果台账（2026-10-27）：把 material=1 事件落成可复核的
T+1 开盘入场结果，不生成信号、不参与仓位。

口径（与 charter §6 / 闸门十九一致）：
- 事件日 = pub_time 的日期；入场 = 事件日之后的**首个交易日开盘**（T+1，
  不假设能交易事件日盘中）。
- gap = entry_open / prev_close - 1；intraday = entry_close / entry_open - 1；
  1d = entry_close / prev_close - 1；5d/20d = close[entry+h-1] / entry_open - 1。
- 只用事件时点之前已知的指数 bar；未来 bar 不足 → pending，不写 0。
- SH=000001、CSI800=000906；两列都记录，主目标仍是 SH。

用法：
  python scripts/news_event_outcome.py [--asof YYYY-MM-DD]
"""
import argparse
import json
import sys
from pathlib import Path

import pandas as pd
from loguru import logger

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

EVENT_GLOB = "logs/news_events_*.csv"
OUT_CSV = ROOT / "logs" / "news_event_outcomes.csv"
OUT_MD = ROOT / "logs" / "news_event_outcomes.md"
INDEX = {"sh": "SH000001", "csi800": "SH000906"}
HORIZONS = (1, 5, 20)


def load_index(code: str, asof: pd.Timestamp | None = None) -> pd.DataFrame:
    frames = []
    for yr in range(2019, pd.Timestamp.today().year + 2):
        p = Path(f"/data/quant_data_store/daily/{yr}/{code}.parquet")
        if p.exists():
            frames.append(pd.read_parquet(p))
    if not frames:
        raise FileNotFoundError(f"index data missing: {code}")
    d = pd.concat(frames, ignore_index=True).drop_duplicates("date")
    d["date"] = pd.to_datetime(d["date"]).dt.normalize()
    d = d.sort_values("date").reset_index(drop=True)
    if asof is not None:
        d = d[d["date"] <= asof].reset_index(drop=True)
    return d


def next_trading_day(event_date: pd.Timestamp, trading_days: list[pd.Timestamp]):
    """事件日之后的第一个交易日；事件日为非交易日也适用。"""
    for d in trading_days:
        if d > event_date:
            return d
    return None


def _ret(close: float, ref: float) -> float:
    return (float(close) / float(ref) - 1.0) * 100.0


def score_event(pub_time, bars: pd.DataFrame):
    """返回一个事件的 T+1 结果；bars 必须按日期升序且含 open/close。"""
    ts = pd.to_datetime(pub_time, errors="coerce")
    if pd.isna(ts):
        return {"status": "bad_time"}
    event_date = ts.normalize()
    days = bars["date"].tolist()
    entry_date = next_trading_day(event_date, days)
    if entry_date is None:
        return {"status": "pending_entry", "event_date": str(event_date.date())}
    i = days.index(entry_date)
    if i == 0:
        return {"status": "no_prev_close", "event_date": str(event_date.date()),
                "entry_date": str(entry_date.date())}
    o = float(bars.loc[i, "open"])
    c = float(bars.loc[i, "close"])
    prev = float(bars.loc[i - 1, "close"])
    if o <= 0 or prev <= 0:
        return {"status": "bad_price", "event_date": str(event_date.date()),
                "entry_date": str(entry_date.date())}
    out = {
        "event_date": str(event_date.date()),
        "entry_date": str(entry_date.date()),
        "entry_open": round(o, 4),
        "prev_close": round(prev, 4),
        "gap_pct": round(_ret(o, prev), 3),
        "intraday_pct": round(_ret(c, o), 3),
        "ret_1d_pct": round(_ret(c, prev), 3),
        "status": "scored_1d",
    }
    for h in (5, 20):
        j = i + h - 1
        if j < len(bars):
            out[f"ret_{h}d_pct"] = round(_ret(float(bars.loc[j, "close"]), o), 3)
            out["status"] = f"scored_{h}d"
        else:
            out[f"ret_{h}d_pct"] = None
    return out


def load_events(asof: pd.Timestamp | None = None) -> pd.DataFrame:
    frames = []
    for p in sorted((ROOT / "logs").glob("news_events_*.csv")):
        try:
            d = pd.read_csv(p)
        except Exception as e:
            logger.warning(f"读取事件台账失败 {p}: {e}")
            continue
        if "material" not in d.columns:
            continue
        d = d[pd.to_numeric(d["material"], errors="coerce").fillna(0).astype(int) == 1]
        frames.append(d)
    if not frames:
        return pd.DataFrame()
    ev = pd.concat(frames, ignore_index=True)
    ev["pub_time"] = pd.to_datetime(ev["pub_time"], errors="coerce")
    ev = ev.dropna(subset=["pub_time", "event_id"]).drop_duplicates("event_id")
    if asof is not None:
        ev = ev[ev["pub_time"] <= asof]
    return ev.sort_values("pub_time").reset_index(drop=True)


def build(asof: pd.Timestamp | None = None) -> pd.DataFrame:
    ev = load_events(asof)
    if ev.empty:
        logger.warning("无 material=1 事件")
        return pd.DataFrame()
    data = {k: load_index(v, asof) for k, v in INDEX.items()}
    rows = []
    for _, e in ev.iterrows():
        row = {
            "event_id": e["event_id"],
            "pub_time": str(e["pub_time"]),
            "category": e.get("category", ""),
            "scope": e.get("scope", ""),
            "title": e.get("title", ""),
        }
        for k, bars in data.items():
            r = score_event(e["pub_time"], bars)
            for kk, vv in r.items():
                if kk not in ("event_date", "entry_date"):
                    row[f"{k}_{kk}"] = vv
            row[f"{k}_event_date"] = r.get("event_date")
            row[f"{k}_entry_date"] = r.get("entry_date")
        # 事件日以 SH 为准；两指数交易日应一致
        row["event_date"] = row.get("sh_event_date")
        row["entry_date"] = row.get("sh_entry_date")
        row["status"] = row.get("sh_status")
        rows.append(row)
    out = pd.DataFrame(rows)
    out = out.sort_values(["pub_time", "event_id"]).reset_index(drop=True)
    asof_val = asof if asof is not None else max(
        d["date"].max() for d in data.values())
    out.insert(0, "as_of", str(asof_val.date()))
    out.insert(1, "return_basis", "price")
    out.insert(2, "benchmark_basis", "price")
    OUT_CSV.write_text(out.to_csv(index=False), encoding="utf-8")
    _write_md(out, asof, data)
    logger.info(f"事件结果台账: {len(out)} 条 → {OUT_CSV}")
    return out


def _write_md(out: pd.DataFrame, asof, data):
    asof_str = str(asof.date()) if asof is not None else str(max(
        d["date"].max() for d in data.values()).date())
    counts = out["status"].value_counts().to_dict()
    lines = [
        "# 消息事件结果台账（T+1 开盘入场，描述性）", "",
        f"- as_of: {asof_str}；事件数: {len(out)}；状态: {counts}",
        "- 入场=事件日之后首个交易日开盘；5d/20d 从入场开盘算；未来 bar 不足记 None（不写 0）。",
        "- return_basis=price（不含分红，绝对收益偏低约 2.3pp/年）；benchmark_basis=price。",
        "- 本文只做结果记录，不是信号；direction=unknown 的事件不因单次涨跌改判。",
        "", "| 事件时间 | 类别 | 事件日 | 入场日 | SH gap | SH 1d | SH 5d | SH 20d | 状态 |",
        "|---|---|---|---|---:|---:|---:|---:|---|",
    ]
    for _, r in out.iterrows():
        def f(k):
            v = r.get(k)
            return "—" if pd.isna(v) or v is None else f"{float(v):+.2f}%"
        lines.append(
            f"| {r['pub_time']} | {r.get('category','')} | {r.get('event_date','')} | "
            f"{r.get('entry_date','')} | {f('sh_gap_pct')} | {f('sh_ret_1d_pct')} | "
            f"{f('sh_ret_5d_pct')} | {f('sh_ret_20d_pct')} | {r.get('status','')} |")
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", default=None,
                    help="冻结数据截止日（YYYY-MM-DD）；缺省=指数最后一根 bar")
    args = ap.parse_args()
    asof = pd.Timestamp(args.asof) if args.asof else None
    out = build(asof)
    if out.empty:
        return
    cols = ["pub_time", "category", "event_date", "entry_date", "status"]
    cols += [c for c in ("sh_gap_pct", "sh_ret_1d_pct") if c in out.columns]
    print(out[cols].to_string(index=False))


if __name__ == "__main__":
    main()
