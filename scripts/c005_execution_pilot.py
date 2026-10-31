"""C0005 描述性试点：14:30 连续竞价价 vs 15:00 收盘价。

来源：媒体任务第一条视频（刻舟量化，BV1V7x16UEAZ）提出收盘集合竞价
14:57–15:00；由此产生 C0005：调仓执行从 14:30 移到收盘附近是否更好。
本脚本只做**描述性**价格漂移，不是执行冲击模型，也不是 C0005 裁决：
  - 14:30 bar close = 14:30 价格；15:00 bar close = 收盘价；
  - 对买入，r>0 表示收盘更贵（14:30 更优）；对卖出相反；
  - 没有 order side/size/盘口/滑点，不能直接决定改执行。
数据：data_store/intraday/5min/{code}.parquet（2026-08-26→10-30，25 只 CB）。
产出：logs/c005_execution_pilot_20261031.json / .md
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
FIVEMIN = ROOT / "data_store" / "intraday" / "5min"
CB_SIGNAL = ROOT / "data_store" / "meta" / "signal_cb_latest.json"
OUT_JSON = ROOT / "logs" / "c005_execution_pilot_20261031.json"
OUT_MD = ROOT / "logs" / "c005_execution_pilot_20261031.md"


def drift_frame(df: pd.DataFrame, code: str) -> pd.DataFrame:
    d = df.copy()
    d["day"] = pd.to_datetime(d["day"])
    d["date"] = d["day"].dt.strftime("%Y-%m-%d")
    d["hm"] = d["day"].dt.strftime("%H:%M")
    p = d[d["hm"] == "14:30"][["date", "close"]].rename(columns={"close": "p1430"})
    q = d[d["hm"] == "15:00"][["date", "close"]].rename(columns={"close": "p1500"})
    m = p.merge(q, on="date", how="inner")
    m = m[(m["p1430"] > 0) & (m["p1500"] > 0)].copy()
    m["ret"] = m["p1500"] / m["p1430"] - 1
    m["code"] = code
    return m[["date", "code", "p1430", "p1500", "ret"]]


def summarize(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"n": 0}
    daily = df.groupby("date")["ret"].mean()
    n_days = len(daily)
    t = (
        float(daily.mean() / (daily.std(ddof=1) / np.sqrt(n_days)))
        if n_days > 1 and daily.std(ddof=1) > 0
        else None
    )
    return {
        "n_obs": int(len(df)),
        "n_days": int(n_days),
        "n_codes": int(df["code"].nunique()),
        "mean_bp": round(float(df["ret"].mean()) * 1e4, 3),
        "median_bp": round(float(df["ret"].median()) * 1e4, 3),
        "win_rate_pct": round(float((df["ret"] > 0).mean()) * 100, 2),
        "day_mean_bp": round(float(daily.mean()) * 1e4, 3),
        "day_t": round(t, 3) if t is not None else None,
        "by_code_mean_bp": {
            str(k): round(float(v) * 1e4, 3)
            for k, v in df.groupby("code")["ret"].mean().items()
        },
    }


def main() -> int:
    holdings = json.loads(CB_SIGNAL.read_text(encoding="utf-8")).get("holdings", [])
    rows = []
    for code in holdings:
        p = FIVEMIN / f"{code}.parquet"
        if not p.exists():
            continue
        rows.append(drift_frame(pd.read_parquet(p), code))
    df = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    out = {
        "as_of": "2026-10-30",
        "source": "data_store/intraday/5min; signal_cb_latest holdings",
        "definition": "ret = close_15:00 / close_14:30 - 1; positive = close higher",
        "summary": summarize(df),
        "caveats": [
            "描述性，不是执行冲击模型；没有 order side/size/盘口/滑点。",
            "样本 2026-08-26→10-30，约 44 个交易日；重叠截面，日聚类 t 仅参考。",
            "对买入 r>0 表示 14:30 更优；对卖出方向相反，需按实际买卖单重算。",
            "未预登记、未做 OOS；不得据此改 14:30 执行。",
        ],
    }
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    s = out["summary"]
    md = (
        "# C0005 描述性试点：14:30 vs 15:00（CB）\n\n"
        f"- 样本：{s.get('n_obs')} 个观测 / {s.get('n_days')} 天 / "
        f"{s.get('n_codes')} 只 CB；as_of 2026-10-30。\n"
        f"- 平均漂移 **{s.get('mean_bp')}bp**，中位 {s.get('median_bp')}bp，"
        f"上涨占比 {s.get('win_rate_pct')}%；日聚类 t={s.get('day_t')}。\n"
        "- 正=收盘更高（对买入 14:30 更优，对卖出相反）；不是执行冲击模型。\n"
        "- 未预登记/未 OOS；**不得据此改 14:30 执行**。\n"
    )
    OUT_MD.write_text(md, encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
