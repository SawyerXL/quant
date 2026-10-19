"""R1 第二阶段: 次日涨停埋伏的收益检验(2026-10-19, 判据出数前锁).

**判据(锁定)**:
  - 入场: T 收盘价买入; T 日已封板 → 排除(涨停禁买红线).
  - 出场: T+1 收盘价卖出; 若 T+1 收盘跌停封死 → 顺延 T+2 开盘卖出.
  - 成本: 主口径 0.23%(双边 0.13% + 滑点 0.1%); 敏感性 0.13%.
  - 基准: 等权全宇宙同窗口收益(T收→T+1收); 超额=个股−基准−成本.
  - 判据1: 超额 >0 且 **日聚类 t ≥ 2.5**(同日事件取日均, 消除截面相关).
  - 判据2: **跳空拆分**——盘中超额(T+1开→收 − 基准盘中)必须同样为正;
    若超额全部在开盘跳空 → 判死(闸门十九).
  - 判据3: 命中率/赔率/最大回撤同时报.
  - 判据4: 分段 2019-2022(IS) / 2023-2026(OOS); 符号相反 → 不采信.
  - **OOS 只用一次**: 本次运行即最终 OOS 使用.
  - 两臂(第一阶段已锁定): A=T日炸板(触板未封); B=近5日涨停≥1次.
用法: python scripts/limitup_ambush_return.py
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from data.storage import load_daily, load_meta

START, END = "2019-01-01", "2026-09-30"
COST_MAIN, COST_SENS = 0.0023, 0.0013


def band_of(code, is_st):
    if is_st:
        return 0.05
    if code.startswith(("30", "68")):
        return 0.20
    if code.startswith(("8", "4", "92")):
        return 0.30
    return 0.10


def main():
    meta = load_meta("stock_info_full")
    meta["code"] = meta["code"].astype(str)
    st_map = dict(zip(meta["code"], meta.get("is_st", pd.Series(False, index=meta.index)).astype(bool)))
    list_map = dict(zip(meta["code"], meta.get("list_date", pd.Series("", index=meta.index)).astype(str).str[:10]))
    # 协议口径: 剔除 ST(5% 档单独登记); 剔除上市 <60 日
    codes = sorted(c for c in meta["code"] if not st_map.get(c, False))

    frames = []
    for code in codes:
        try:
            d = load_daily(code, START, END)
            if d is None or d.empty or len(d) < 250:
                continue
            d = d.copy()
            d["date"] = pd.to_datetime(d["date"])
            d = d.sort_values("date")
            close = pd.to_numeric(d["close"], errors="coerce")
            opn = pd.to_numeric(d["open"], errors="coerce")
            high = pd.to_numeric(d["high"], errors="coerce")
            prev = close.shift(1)
            band = band_of(code, st_map.get(code, False))
            lim_up = (prev * (1 + band)).round(2)
            lim_dn = (prev * (1 - band)).round(2)
            frames.append(pd.DataFrame({
                "date": d["date"].values, "code": code,
                "open": opn.values, "close": close.values,
                "prev": prev.values,
                "touch": ((high >= lim_up - 1e-6) & prev.notna()).values,
                "seal": ((close >= lim_up - 1e-6) & prev.notna()).values,
                "seal_dn": ((close <= lim_dn + 1e-6) & prev.notna()).values,
                "pct": (close / prev - 1).values,
            }))
        except Exception:
            continue

    df = pd.concat(frames, ignore_index=True).sort_values(["code", "date"])
    df["code"] = df["code"].astype("category")
    # 数据卫生: 非正价格一律剔除(否则 inf 污染均值)
    n0 = len(df)
    df = df[(df["open"] > 0) & (df["close"] > 0) & (df["prev"] > 0)]
    # 上市 <60 日剔除(IPO 涨跌幅规则不同)
    ld = df["code"].astype(str).map(list_map)
    age = (pd.to_datetime(df["date"]) -
           pd.to_datetime(ld, errors="coerce")).dt.days
    df = df[age.fillna(9999) >= 60]
    print(f"[data] {n0:,} → {len(df):,} 行(剔非正价/上市<60日)")

    # ── 基准: 等权全宇宙 收盘→收盘 / 开盘→收盘 ──
    df["ret_cc"] = df["close"] / df["prev"] - 1
    df["gap"] = df["open"] / df["prev"] - 1
    df["intra"] = df["close"] / df["open"] - 1
    bench = df.groupby("date")[["ret_cc", "gap", "intra"]].mean()
    bench.columns = ["b_cc", "b_gap", "b_intra"]
    bench.index = pd.to_datetime(bench.index).strftime("%Y-%m-%d")

    g = df.groupby("code", sort=False)
    df["lu5"] = g["seal"].rolling(5, min_periods=1).sum().reset_index(level=0, drop=True)
    df["touch_next"] = g["touch"].shift(-1)
    df["seal_dn_next"] = g["seal_dn"].shift(-1)
    df["close_next"] = g["close"].shift(-1)
    # T+1 开盘 / T+2 开盘(跌停顺延)
    df["open_next"] = g["open"].shift(-1)
    df["open_next2"] = g["open"].shift(-2)
    df["date_next"] = g["date"].shift(-1)

    # 事件臂
    df["armA"] = (df["touch"] & ~df["seal"])          # T日炸板
    df["armB"] = (df["lu5"] >= 1) & (~df["seal"])     # 近5日涨停≥1且今日未封

    df["gap_next"] = g["gap"].shift(-1)
    df["intra_next"] = g["intra"].shift(-1)
    # 重新跑一遍(带 T+1 gap/intra)
    out = {}
    for arm in ("armA", "armB"):
        sub = df[df[arm] & df["close_next"].notna()].copy()
        delay = sub["seal_dn_next"].fillna(False).astype(bool)
        exit_px = sub["close_next"].where(~delay, sub["open_next2"])
        ok = exit_px.notna()
        sub, exit_px, delay = sub[ok], exit_px[ok], delay[ok]
        sub["stock_ret"] = exit_px / sub["close"] - 1
        n_delay = int(delay.sum())
        sub["mdate"] = pd.to_datetime(sub["date_next"]).dt.strftime("%Y-%m-%d")
        sub = sub.merge(bench, left_on="mdate", right_index=True, how="left")
        sub["excess"] = sub["stock_ret"] - sub["b_cc"] - COST_MAIN
        sub["excess_sens"] = sub["stock_ret"] - sub["b_cc"] - COST_SENS
        sub["gap_ex"] = sub["gap_next"] - sub["b_gap"]
        sub["intra_ex"] = sub["intra_next"] - sub["b_intra"]
        sub["year"] = pd.to_datetime(sub["date"]).dt.year
        sub["seg"] = np.where(sub["year"] <= 2022, "IS_2019_2022", "OOS_2023_2026")

        def stats(x):
            daily = x.groupby("mdate")["excess"].mean()
            n = len(x); n_days = len(daily)
            mean = float(x["excess"].mean())
            t_day = float(daily.mean() / (daily.std() / np.sqrt(n_days))) if n_days > 1 else None
            t_naive = float(mean / (x["excess"].std() / np.sqrt(n))) if n > 1 else None
            eq = (1 + daily).cumprod()
            dd = float((eq / eq.cummax() - 1).min()) if len(eq) else None
            return {
                "n_events": n, "n_days": n_days,
                "mean_excess_pct": mean * 100,
                "median_excess_pct": float(x["excess"].median()) * 100,
                "win_rate_pct": float((x["excess"] > 0).mean()) * 100,
                "t_day_cluster": t_day, "t_naive": t_naive,
                "mean_gap_ex_pct": float(x["gap_ex"].mean()) * 100,
                "mean_intra_ex_pct": float(x["intra_ex"].mean()) * 100,
                "max_dd_pct": dd * 100 if dd is not None else None,
                "mean_excess_sens013_pct": float(x["excess_sens"].mean()) * 100,
                "n_delayed_exit": n_delay,
            }

        out[arm] = {"all": stats(sub)}
        for seg, ss in sub.groupby("seg"):
            out[arm][seg] = stats(ss)

    print(pd.DataFrame({f"{a}_{k}": v for a, d in out.items() for k, v in d.items()})
          .T.to_string())
    import json
    Path("logs/limitup_ambush_return.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n已保存 logs/limitup_ambush_return.json")


if __name__ == "__main__":
    main()
