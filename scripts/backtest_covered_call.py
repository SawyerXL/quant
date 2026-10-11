"""期权备兑增强实验 N=45（2026-10-11 登记, 判据已锁）。

两阶段:
  --mde-only: 50ETF 裸持 vs 备兑的月收益差 σ → TE → SE → MDE(2×SE),
              打印可判读性
  (默认)全量: 臂A=50ETF 裸持; 臂B=备兑(每月卖1月ATM call 滚动, 权利金率
              =0.4×IV×sqrt(30/365), QVIX 市场真值, 欧式到期行权口径)
判据(锁): ①增强α(臂B−臂A, 月频TE方法)≥MDE且MDE先行 ②总收益不降
(踏空判据: 臂B年化≥臂A) ③臂B回撤≤臂A ④分段(IV高低分位+2015-18/
2019-22/2023-26三段)+权利金±50%敏感性。
简化模型未计期权买卖价差/行权指派(与 backtest_put_hedge.py 同口径),
实盘前须真实期权链校准。
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import akshare as ak

sys.path.insert(0, str(Path(__file__).parent.parent))


def load():
    q = ak.index_option_50etf_qvix()
    q["date"] = pd.to_datetime(q["date"])
    iv = q.set_index("date")["close"].astype(float) / 100.0
    d = ak.fund_etf_hist_sina(symbol="sh510050")
    d["date"] = pd.to_datetime(d["date"])
    etf = d.set_index("date")["close"].astype(float)
    return iv, etf


def premium(iv):
    return 0.4 * iv * np.sqrt(30 / 365)


def run_arms():
    iv, etf = load()
    # 每月初(首个交易日)开仓: ATM call, 到期=月末
    months = etf.resample("ME").last().index
    dates = etf.index
    # 逐月持有区间
    rec = []
    prev_end = None
    for m in months:
        seg = etf[(etf.index <= m) & (etf.index > (prev_end or etf.index.min()))]
        if len(seg) < 5:
            continue
        s0 = seg.iloc[0]
        st = seg.iloc[-1]
        iv_now = iv.reindex([seg.index[0]], method="ffill")
        p = premium(iv_now.iloc[0]) if not iv_now.isna().iloc[0] else premium(0.2)
        # 裸持月收益; 备兑=min(ST/S0,1)+权利金
        r_naked = st / s0 - 1
        r_cc = min(st / s0, 1.0) - 1 + p
        rec.append({"month": m, "naked": r_naked, "cc": r_cc, "iv": iv_now.iloc[0], "prem": p})
        prev_end = m
    df = pd.DataFrame(rec).set_index("month")
    return df


def mde_stage(df):
    d = df["cc"] - df["naked"]
    years = (df.index[-1] - df.index[0]).days / 365.25
    sig = d.std(ddof=1)
    te = sig * np.sqrt(12)
    se = te / np.sqrt(years)
    mde = 2 * se
    print(f"MDE 先行: 月增强差 σ={sig*100:.2f}% TE={te*100:.2f}% "
          f"SE={se*100:.2f}pp MDE(2×SE)={mde*100:.2f}pp (n={len(d)}月, T={years:.1f}年)")
    return mde


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mde-only", action="store_true")
    args = ap.parse_args()
    df = run_arms()
    mde = mde_stage(df)
    if args.mde_only:
        return
    print("\n=== 全量臂 (判据①②③④) ===")
    nav_a = (1 + df["naked"]).cumprod()
    nav_b = (1 + df["cc"]).cumprod()
    years = (df.index[-1] - df.index[0]).days / 365.25
    ann_a = nav_a.iloc[-1] ** (1 / years) - 1
    ann_b = nav_b.iloc[-1] ** (1 / years) - 1
    dd_a = (nav_a / nav_a.cummax() - 1).min()
    dd_b = (nav_b / nav_b.cummax() - 1).min()
    d = df["cc"] - df["naked"]
    alpha = d.mean() * 12
    print(f"臂A 裸持: 年化 {ann_a*100:+.2f}% 回撤 {dd_a*100:.1f}%")
    print(f"臂B 备兑: 年化 {ann_b*100:+.2f}% 回撤 {dd_b*100:.1f}%")
    print(f"增强 α = {alpha*100:+.2f}pp/年 (MDE={mde*100:.2f}pp)")
    c1 = alpha >= mde
    c2 = ann_b >= ann_a
    c3 = dd_b <= dd_a
    print(f"判据: ①α≥MDE {'✓' if c1 else '✗'} ②总收益不降 {'✓' if c2 else '✗'} "
          f"③回撤≤裸持 {'✓' if c3 else '✗'}")
    # ④ 分段 + 敏感性
    for name, a, b in [("2015-2018", "2015-01-01", "2019-01-01"),
                       ("2019-2022", "2019-01-01", "2023-01-01"),
                       ("2023-2026", "2023-01-01", "2030-01-01")]:
        w = df[(df.index >= a) & (df.index < b)]
        if len(w):
            a_seg = (w["cc"] - w["naked"]).mean() * 12
            print(f"  分段 {name}: 增强 α={a_seg*100:+.2f}pp/年 (n={len(w)})")
    lo = df["naked"] - df["cc"]  # 高价差方向
    for scale, tag in [(0.5, "权利金−50%"), (1.5, "权利金+50%")]:
        cc2 = df["cc"] + (scale - 1) * df["prem"]
        a2 = (cc2 - df["naked"]).mean() * 12
        print(f"  敏感 {tag}: 增强 α={a2*100:+.2f}pp/年")
    hi_iv = df[df["iv"] >= df["iv"].median()]
    lo_iv = df[df["iv"] < df["iv"].median()]
    print(f"  IV 分位: 高IV增强 {((hi_iv['cc']-hi_iv['naked']).mean()*12)*100:+.2f}pp/年 "
          f"低IV增强 {((lo_iv['cc']-lo_iv['naked']).mean()*12)*100:+.2f}pp/年")


if __name__ == "__main__":
    main()
