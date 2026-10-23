"""H1 估值前向纸面（2026-10-23 起，只读测量，不交易）。

背景：H1（ERP 分位→12M）历史检验按预注册判 FAIL（近失），但方向一致、
OOS t=2.88；为获得**新数据**，从 2026-09 起记录每月末的估值分位与未来 12M
收益（前向，不消耗历史 OOS）。

口径：
- 面板 `data_store/regime/panel_monthly.parquet` 由 `regime_data_build.py`
  月度刷新；本脚本取**上一个完整月**（不含当前月部分数据）。
- 信号只用当月末已知数据：`erp_pct_10y`（滚动 10 年分位，自适应——不用
  固定 IS 阈值）。
- 分桶：erp_pct >= 0.8 → cheap；<= 0.2 → expensive；其余 neutral。
- 记录字段：month/recorded_at/sh_close/erp/erp_pct_10y/bucket/
  m1_m2_gap_lag1/fwd_12m/scored_at/note。
- 评分：面板提供 12M 后收盘时回填 `fwd_12m`；`--report` 汇总分桶表现。
- 幂等：同一 month 只记一次；已存在则跳过（不覆盖历史记录）。
- 状态：`logs/regime_forward_paper_state.json`（供健康行检查月度是否落地）。

用法:
  python scripts/regime_forward_paper.py            # 记录上一个完整月
  python scripts/regime_forward_paper.py --score    # 回填已满 12M 的行
  python scripts/regime_forward_paper.py --report   # 分桶汇总
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent.parent
PANEL = ROOT / "data_store" / "regime" / "panel_monthly.parquet"
OUT = ROOT / "logs" / "regime_forward_paper.csv"
STATE = ROOT / "logs" / "regime_forward_paper_state.json"
COLS = ["month", "recorded_at", "sh_close", "erp", "erp_pct_10y", "bucket",
        "m1_m2_gap_lag1", "fwd_12m", "scored_at", "note"]


def _load_panel():
    d = pd.read_parquet(PANEL)
    d["month"] = pd.to_datetime(d["month"])
    return d.set_index("month").sort_index()


def _expected_last_complete(now=None):
    """上一日历月。**不能从面板自己的 max 推**：面板不刷新时会把陈旧月
    当成"最后完整月"，状态恒 ok、告警永不触发（闸门十二同型）。"""
    now = now or pd.Timestamp.today()
    return (now.to_period("M") - 1).to_timestamp()


def _bucket(pct):
    if pct is None or pd.isna(pct):
        return None
    if pct >= 0.8:
        return "cheap"
    if pct <= 0.2:
        return "expensive"
    return "neutral"


def record():
    d = _load_panel()
    month = _expected_last_complete()
    hist = pd.read_csv(OUT) if OUT.exists() else pd.DataFrame(columns=COLS)
    if month not in d.index:
        print(f"面板缺完整月 {month:%Y-%m}（数据未更新？）")
        _write_state(d, hist, month)
        return 1
    key = str(month)[:7]
    if not hist.empty and key in set(hist["month"].astype(str)):
        print(f"{key} 已记录，跳过")
    else:
        r = d.loc[month]
        row = {"month": key,
               "recorded_at": datetime.now().isoformat(timespec="seconds"),
               "sh_close": round(float(r["sh_close"]), 2),
               "erp": round(float(r["erp"]), 4) if pd.notna(r["erp"]) else None,
               "erp_pct_10y": (round(float(r["erp_pct_10y"]), 4)
                               if pd.notna(r["erp_pct_10y"]) else None),
               "bucket": _bucket(r.get("erp_pct_10y")),
               "m1_m2_gap_lag1": (round(float(r["m1_m2_gap_lag1"]), 2)
                                  if pd.notna(r.get("m1_m2_gap_lag1")) else None),
               "fwd_12m": None, "scored_at": "", "note": "monthly"}
        hist = pd.concat([hist, pd.DataFrame([row])], ignore_index=True)
        hist = hist[COLS]
        hist.to_csv(OUT, index=False)
        print(f"记录 {key}: ERP {row['erp']} pct {row['erp_pct_10y']} "
              f"bucket {row['bucket']}")
    _write_state(d, hist, month)
    return 0


def score():
    d = _load_panel()
    if not OUT.exists():
        print("无纸面记录")
        return 0
    hist = pd.read_csv(OUT)
    n = 0
    for i, row in hist.iterrows():
        if pd.notna(row.get("fwd_12m")):
            continue
        m = pd.Timestamp(str(row["month"]) + "-01")
        target = m + pd.DateOffset(months=12)
        if target not in d.index:
            continue
        v = d.loc[target, "sh_close"] / d.loc[m, "sh_close"] - 1
        hist.at[i, "fwd_12m"] = round(float(v), 6)
        hist.at[i, "scored_at"] = datetime.now().isoformat(timespec="seconds")
        n += 1
    hist.to_csv(OUT, index=False)
    print(f"回填 {n} 行")
    _write_state(d, hist, _expected_last_complete())
    return 0


def report():
    if not OUT.exists():
        print("无纸面记录")
        return 0
    hist = pd.read_csv(OUT)
    print(hist[["month", "erp_pct_10y", "bucket", "fwd_12m"]].to_string(index=False))
    s = hist.dropna(subset=["fwd_12m"])
    if not s.empty:
        print("\n分桶 12M 前向收益:")
        print(s.groupby("bucket")["fwd_12m"].agg(["count", "mean"]).to_string())
    else:
        print("\n尚无满 12M 的评分行")
    return 0


def _write_state(d, hist, expected):
    latest = (str(hist["month"].max()) if not hist.empty else None)
    state = {"as_of": datetime.now().isoformat(timespec="seconds"),
             "last_complete_month": str(expected)[:7],
             "latest_recorded": latest,
             "n_rows": int(len(hist)),
             "n_scored": int(hist["fwd_12m"].notna().sum()) if not hist.empty else 0,
             "ok": bool(latest is not None and latest >= str(expected)[:7])}
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                     encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    if a.score:
        return score()
    if a.report:
        return report()
    return record()


if __name__ == "__main__":
    sys.exit(main())
