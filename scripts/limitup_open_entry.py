"""R1b: T+1 开盘买入、T+1 收盘卖出(2026-10-19, 判据出数前锁).

背景: R1（T收盘买）判死, 亏损主体在隔夜跳空(跳空超额−1.42%), 而盘中超额
+0.52%。R1b 检验"避开隔夜、只吃盘中"的可执行形式。

**判据(出数前锁)**:
  - 信号(沿用 R1 第一阶段已验证条件): A=T日炸板; B=近5日涨停≥1且T日未封。
  - 入场: **T+1 开盘价**; 若 T+1 开盘 ≥ T+1 涨停价(一字板/开盘即涨停) →
    **不可买入, 剔除并单独统计其收益**(不可交易的机会成本, 不得计入收益)。
  - 出场: T+1 收盘价; 若 T+1 收盘跌停封死(无法卖出) → 剔除并计数。
  - 成本: 主 0.23%(双边0.13%+滑点0.1%); 敏感性 0.13%。
  - 基准: 等权全宇宙 T+1 盘中(开→收)收益; 超额=个股盘中−基准盘中−成本。
  - 判据1: 超额 >0 且 **日聚类 t ≥ 2.5**。
  - 判据2: 分段 2019-2022 / 2023-2026 同号。
  - 判据3: 命中率/中位/最大回撤同时报。
  - **多重检验声明**: 这是同一信号上的第二个变体(R1 已用一次 OOS);
    本变体 OOS 段再次使用, 增加假阳风险, 结论按更严格标准解读(报告两段)。
用法: python scripts/limitup_open_entry.py
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from data.storage import load_daily, load_meta

START, END = "2019-01-01", "2026-09-30"
COST_MAIN, COST_SENS = 0.0023, 0.0013


def band_of(code):
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
            band = band_of(code)
            lim_up = (prev * (1 + band)).round(2)
            lim_dn = (prev * (1 - band)).round(2)
            frames.append(pd.DataFrame({
                "date": d["date"].values, "code": code,
                "open": opn.values, "close": close.values, "prev": prev.values,
                "touch": ((high >= lim_up - 1e-6) & prev.notna()).values,
                "seal": ((close >= lim_up - 1e-6) & prev.notna()).values,
                "seal_dn": ((close <= lim_dn + 1e-6) & prev.notna()).values,
            }))
        except Exception:
            continue

    df = pd.concat(frames, ignore_index=True).sort_values(["code", "date"])
    df["code"] = df["code"].astype("category")
    df = df[(df["open"] > 0) & (df["close"] > 0) & (df["prev"] > 0)]
    ld = df["code"].astype(str).map(list_map)
    age = (pd.to_datetime(df["date"]) - pd.to_datetime(ld, errors="coerce")).dt.days
    df = df[age.fillna(9999) >= 60]

    g = df.groupby("code", sort=False)
    df["lu5"] = g["seal"].rolling(5, min_periods=1).sum().reset_index(level=0, drop=True)
    df["open_next"] = g["open"].shift(-1)
    df["open_next2"] = g["open"].shift(-2)
    df["close_next"] = g["close"].shift(-1)
    df["seal_dn_next"] = g["seal_dn"].shift(-1)
    df["band"] = df["code"].astype(str).map(lambda c: band_of(c))
    df["lim_up_next"] = (df["close"] * (1 + df["band"])).round(2)
    df["date_next"] = g["date"].shift(-1)
    df["intra_next"] = df["close_next"] / df["open_next"] - 1

    # 基准: 等权盘中
    bench = df.groupby("date", observed=True)["open"].transform("mean")  # noop
    df["intra"] = df["close"] / df["open"] - 1
    b_intra = df.groupby("date", observed=True)["intra"].mean()
    b_intra.index = pd.to_datetime(b_intra.index).strftime("%Y-%m-%d")
    # 两日窗口基准(T+1开→T+2开), 供跌停顺延样本(可执行口径, 不剔除亏损)
    df["r2_open"] = df["open_next2"] / df["open_next"] - 1
    df["date_next_s"] = pd.to_datetime(df["date_next"]).dt.strftime("%Y-%m-%d")
    b_2d = df.groupby("date_next_s", observed=True)["r2_open"].mean()

    df["armA"] = df["touch"] & ~df["seal"]
    df["armB"] = (df["lu5"] >= 1) & (~df["seal"])

    out = {}
    for arm in ("armA", "armB"):
        base = df[df[arm] & df["open_next"].notna() & df["close_next"].notna()].copy()
        one_word = base["open_next"] >= base["lim_up_next"] - 1e-6
        # 不可买入样本: 记录其盘中收益(若可买)作为机会成本参考
        excluded = base[one_word].copy()
        excluded_ret = (excluded["close_next"] / excluded["open_next"] - 1)
        trade = base[~one_word].copy()
        # 跌停封死无法当日卖出 → **顺延到 T+2 开盘卖出**(可执行口径,
        # 不得剔除: 剔除会把大亏样本隐藏, 美化结果)。需 T+2 开盘存在。
        cannot_exit = trade["seal_dn_next"].fillna(False).astype(bool)
        n_cannot = int(cannot_exit.sum())
        trade["delayed"] = cannot_exit
        trade = trade[trade["open_next"].notna() &
                      (~cannot_exit | trade["open_next2"].notna())]
        trade["entry"] = trade["open_next"]
        trade["stock_ret"] = np.where(
            trade["delayed"],
            trade["open_next2"] / trade["open_next"] - 1,
            trade["close_next"] / trade["open_next"] - 1)
        trade["mdate"] = pd.to_datetime(trade["date_next"]).dt.strftime("%Y-%m-%d")
        trade = trade.merge(b_intra.rename("b_intra"), left_on="mdate",
                            right_index=True, how="left")
        trade = trade.merge(b_2d.rename("b_2d"), left_on="mdate",
                            right_index=True, how="left")
        trade["bench"] = np.where(trade["delayed"], trade["b_2d"], trade["b_intra"])
        trade = trade[trade["bench"].notna()]
        trade["excess"] = trade["stock_ret"] - trade["bench"] - COST_MAIN
        trade["excess_sens"] = trade["stock_ret"] - trade["bench"] - COST_SENS
        trade["year"] = pd.to_datetime(trade["date"]).dt.year
        trade["seg"] = np.where(trade["year"] <= 2022, "IS_2019_2022", "OOS_2023_2026")

        def stats(x):
            daily = x.groupby("mdate")["excess"].mean()
            n, nd = len(x), len(daily)
            mean = float(x["excess"].mean())
            t_day = float(daily.mean() / (daily.std() / np.sqrt(nd))) if nd > 1 else None
            eq = (1 + daily).cumprod()
            dd = float((eq / eq.cummax() - 1).min()) if len(eq) else None
            return {"n": n, "n_days": nd, "mean_excess_pct": mean * 100,
                    "median_pct": float(x["excess"].median()) * 100,
                    "win_rate_pct": float((x["excess"] > 0).mean()) * 100,
                    "t_day_cluster": t_day,
                    "mean_excess_sens013_pct": float(x["excess_sens"].mean()) * 100,
                    "max_dd_pct": dd * 100 if dd is not None else None}

        dl = trade[trade["delayed"]]
        out[arm] = {"all": stats(trade),
                    "excluded_one_word": {"n": int(one_word.sum()),
                                          "mean_intra_ret_pct": float(excluded_ret.mean() * 100)
                                          if len(excluded_ret) else None},
                    "delayed_exit": {"n": n_cannot,
                                     "mean_excess_pct": float(dl["excess"].mean() * 100)
                                     if len(dl) else None}}
        for seg, ss in trade.groupby("seg"):
            out[arm][seg] = stats(ss)

    print(pd.DataFrame({f"{a}_{k}": v for a, d in out.items() for k, v in d.items()
                        if isinstance(v, dict) and "mean_excess_pct" in v}).T.to_string())
    for arm, d in out.items():
        print(arm, "excluded_one_word:", d["excluded_one_word"],
              "| delayed_exit:", d["delayed_exit"])
    import json
    Path("logs/limitup_open_entry.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n已保存 logs/limitup_open_entry.json")


if __name__ == "__main__":
    main()
