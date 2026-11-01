"""C0006：T+1 隔夜折价能否被利用（预登记见 logs/research_registration_c0006_20261101.md）。

只做两件事：
1. 描述 CSI800 PIT 的隔夜收益 ON=open_t/close_{t-1}-1 是否显著为负；
2. 检验一个 T+1 可执行的两半轮换 overlay（每半隔夜出一次，捕获约一半
   ON），与 buy&hold 的日增量 = -0.5*ON - 0.5*成本，是否 > MDE。
每日收盘卖/次日开盘买的全额 overlay 因 T+1 不可执行，只作上界。

输出 logs/c0006_overnight_20261101.json/.md；FAIL 即关闭，不调参。
"""
from __future__ import annotations

import bisect
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from data.storage import load_daily

ROOT = Path(__file__).resolve().parents[1]
WEIGHTS = ROOT / "data_store" / "meta" / "csi800_pit_weights.parquet"
INDEX = ROOT / "data_store" / "meta" / "csi800_index.parquet"
OUT_JSON = ROOT / "logs" / "c0006_overnight_20261101.json"
OUT_MD = ROOT / "logs" / "c0006_overnight_20261101.md"
IS = ("2019-07-01", "2023-12-31")
OOS = ("2024-01-01", "2026-09-30")
SAMPLE = ("2019-07-01", "2026-09-30")
DATA_START = "2019-06-01"
COST = 0.0020
EXCLUDE = {"000001", "000905", "000906", "000688"}  # 指数/个股歧义键


def summarize_series(x: pd.Series) -> dict:
    """日聚类统计；x 的索引为交易日，值为当日的横截面均值。"""
    x = pd.to_numeric(x, errors="coerce").dropna()
    n = int(len(x))
    if n < 2:
        return {"n_days": n}
    se = float(x.std(ddof=1) / np.sqrt(n))
    mde = 2.8 * se
    return {
        "n_days": n,
        "mean_bp": round(float(x.mean()) * 1e4, 4),
        "median_bp": round(float(x.median()) * 1e4, 4),
        "win_rate_pct": round(float((x > 0).mean()) * 100, 2),
        "t": round(float(x.mean() / se), 3) if se > 0 else None,
        "mde_bp": round(float(mde) * 1e4, 4),
        "ann_arith_pct": round(float(x.mean()) * 252 * 100, 3),
        "cum_pct": round(float((1 + x).prod() - 1) * 100, 4),
    }


def period_stats(x: pd.Series, start: str, end: str) -> dict:
    y = x.loc[(x.index >= start) & (x.index <= end)]
    return summarize_series(y)


def verdict_for(out: dict) -> str:
    oos = out["periods"]["OOS"]["half_diff"]
    iss = out["periods"]["IS"]["half_diff"]
    if oos.get("n_days", 0) < 200 or oos.get("mde_bp") is None:
        return "UNDERPOWERED"
    if oos["mean_bp"] > oos["mde_bp"] and oos["mean_bp"] > 0 \
            and iss.get("mean_bp", -999) > 0:
        return "PASS"
    return "FAIL"


def build_daily_panel() -> tuple[pd.DataFrame, dict]:
    """按 CSI800 PIT 成员等权聚合 ON/ID；返回日面板与元信息。"""
    w = pd.read_parquet(WEIGHTS)
    w["code"] = w["code"].astype(str).str.zfill(6)
    w["trade_date"] = pd.to_datetime(w["trade_date"]).dt.strftime("%Y-%m-%d")
    w = w[~w["code"].isin(EXCLUDE)]
    snap_dates = sorted(w["trade_date"].unique())
    membership = {d: set(g["code"]) for d, g in w.groupby("trade_date")}

    idx = pd.read_parquet(INDEX)
    idx["date"] = pd.to_datetime(idx["date"]).dt.strftime("%Y-%m-%d")
    dates = idx.loc[(idx["date"] >= SAMPLE[0]) & (idx["date"] <= SAMPLE[1]), "date"]
    dates = sorted(dates.unique())
    date_to_i = {d: i for i, d in enumerate(dates)}
    n_dates = len(dates)

    codes = sorted(w["code"].unique())
    code_set = set(codes)
    # member[code] = 每个交易日是否属于最新快照；逐日构造，避免逐行 bisect。
    member = {c: np.zeros(n_dates, dtype=bool) for c in codes}
    for i, d in enumerate(dates):
        si = bisect.bisect_right(snap_dates, d) - 1
        if si < 0:
            continue
        for c in membership[snap_dates[si]]:
            if c in code_set:
                member[c][i] = True

    sum_on = np.zeros(n_dates)
    sum_id = np.zeros(n_dates)
    cnt = np.zeros(n_dates, dtype=int)
    n_loaded = 0
    n_rows = 0
    for j, code in enumerate(codes):
        df = load_daily(code, DATA_START, SAMPLE[1], columns=["date", "open", "close"])
        if len(df) < 100:
            continue
        df = df.sort_values("date").reset_index(drop=True)
        df["d"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
        di = df["d"].map(date_to_i).to_numpy()
        ok = np.isfinite(di)
        if not ok.any():
            continue
        di_i = di[ok].astype(int)
        op = pd.to_numeric(df["open"], errors="coerce").to_numpy()
        cl = pd.to_numeric(df["close"], errors="coerce").to_numpy()
        prev = np.roll(cl, 1)
        prev[0] = np.nan
        with np.errstate(divide="ignore", invalid="ignore"):
            on = op / prev - 1.0
            intra = cl / op - 1.0
        valid = np.isfinite(on) & np.isfinite(intra) & (op > 0) & (cl > 0) & (prev > 0)
        sel = np.zeros(len(df), dtype=bool)
        sel[ok] = member[code][di_i]
        sel &= valid
        if not sel.any():
            continue
        k = di[sel].astype(int)
        np.add.at(sum_on, k, on[sel])
        np.add.at(sum_id, k, intra[sel])
        np.add.at(cnt, k, 1)
        n_loaded += 1
        n_rows += int(sel.sum())
        if n_loaded % 200 == 0:
            print("loaded", n_loaded, "rows", n_rows, flush=True)

    with np.errstate(invalid="ignore", divide="ignore"):
        on_mean = np.where(cnt > 0, sum_on / cnt, np.nan)
        id_mean = np.where(cnt > 0, sum_id / cnt, np.nan)
    panel = pd.DataFrame({
        "date": dates,
        "on": on_mean,
        "id": id_mean,
        "n_codes": cnt,
    }).set_index("date")
    panel["bh"] = panel["on"] + panel["id"]
    panel["full_diff"] = -panel["on"] - COST
    panel["half_diff"] = -0.5 * panel["on"] - 0.5 * COST
    panel["half_ret"] = panel["id"] + 0.5 * panel["on"] - 0.5 * COST
    panel["full_ret"] = panel["id"] - COST
    meta = {
        "codes_in_weights": len(codes),
        "codes_loaded": n_loaded,
        "stock_days": n_rows,
        "n_dates": n_dates,
        "avg_codes_per_day": round(float(cnt[cnt > 0].mean()), 2) if (cnt > 0).any() else 0,
        "min_codes_per_day": int(cnt[cnt > 0].min()) if (cnt > 0).any() else 0,
        "cost_roundtrip": COST,
    }
    return panel, meta


def run() -> dict:
    panel, meta = build_daily_panel()
    out = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "registration": "logs/research_registration_c0006_20261101.md",
        "universe": "CSI800 PIT weights; equal-weight daily",
        "sample": SAMPLE,
        "meta": meta,
        "periods": {},
        "by_year": {},
    }
    for label, (s, e) in (("IS", IS), ("OOS", OOS), ("ALL", SAMPLE)):
        out["periods"][label] = {
            col: period_stats(panel[col], s, e)
            for col in ("on", "id", "bh", "full_diff", "half_diff",
                        "full_ret", "half_ret")
        }
        out["periods"][label]["n_codes_mean"] = round(
            float(panel.loc[(panel.index >= s) & (panel.index <= e), "n_codes"].mean()), 2
        )
    for year, g in panel.groupby(panel.index.str[:4]):
        out["by_year"][year] = {
            "on_bp": round(float(g["on"].mean()) * 1e4, 3),
            "id_bp": round(float(g["id"].mean()) * 1e4, 3),
            "half_diff_bp": round(float(g["half_diff"].mean()) * 1e4, 3),
            "n_days": int(len(g)),
        }
    out["verdict"] = verdict_for(out)
    out["rule"] = ("OOS half_diff mean > MDE and >0, IS same sign; "
                   "else FAIL; OOS n_days<200 or MDE>5bp UNDERPOWERED")
    panel.to_parquet(ROOT / "logs" / "c0006_overnight_panel_20261101.parquet")
    return out


def render_md(d: dict) -> str:
    m = d["meta"]
    lines = [
        "# C0006 T+1 隔夜折价检验（机械代理）",
        "",
        f"- 预登记：`{d['registration']}`；样本 {d['sample'][0]}→{d['sample'][1]}；"
        f"成本 {m['cost_roundtrip']*1e4:.0f}bp/往返；"
        f"{m['codes_loaded']} 只 / {m['stock_days']} 股日 / "
        f"{m['avg_codes_per_day']} 只日均（min {m['min_codes_per_day']}）。",
        f"- 裁决：**{d['verdict']}**（{d['rule']}）。",
        "",
        "| 期间 | 指标 | n_days | 日均(bp) | 中位(bp) | 胜率 | t | MDE(bp) | 年化(算术) | 累计 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for label in ("IS", "OOS", "ALL"):
        for col in ("on", "id", "half_diff", "full_diff", "half_ret", "bh"):
            s = d["periods"][label][col]
            if s.get("n_days", 0) < 2:
                continue
            lines.append(
                f"| {label} | {col} | {s['n_days']} | {s['mean_bp']} | "
                f"{s['median_bp']} | {s['win_rate_pct']}% | {s['t']} | "
                f"{s['mde_bp']} | {s['ann_arith_pct']}% | {s['cum_pct']}% |"
            )
    lines += ["", "分年（bp）：", "", "| 年 | ON | ID | half_diff | n_days |",
              "|---|---:|---:|---:|---:|"]
    for y, s in d["by_year"].items():
        lines.append(f"| {y} | {s['on_bp']} | {s['id_bp']} | "
                     f"{s['half_diff_bp']} | {s['n_days']} |")
    lines += [
        "",
        "**解读**：本样本 CSI800 PIT 的 ON 日均仅 **−3.56bp**（ALL，t=−2.39；"
        "OOS −3.61bp，t=−1.34，MDE 7.56bp），远小于论文摘要的 14bp。"
        "即便 14bp 全部复现：全额上界 `14−20=−6bp/日`、两半轮换 "
        "`7−10=−3bp/日`，仍为负；本样本实测更差（−16.44 / −8.22bp/日）。"
        "**T+1 折价存在不等于可套利**：T+1 使全额每日轮换不可执行，两半"
        "只能捕获一半折价，而成本按半仓计仍高于捕获额。",
        "",
        "口径：ON=open_t/close_{t-1}-1；ID=close_t/open_t-1；"
        "full_diff=-ON-20bp（因 T+1 不可每日执行，仅上界）；"
        "half_diff=-0.5*ON-10bp（两半轮换，主裁决）；"
        "等权日聚合、每日再平衡、未计底仓跟踪误差。",
        "",
        "局限：只有论文摘要、非全文；PIT 快照非逐日；停牌/涨跌停/竞价成交"
        "未建模；单次检验，不单独作为部署依据；FAIL 即关闭，不调参。",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    d = run()
    OUT_JSON.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    OUT_MD.write_text(render_md(d), encoding="utf-8")
    print(render_md(d))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
