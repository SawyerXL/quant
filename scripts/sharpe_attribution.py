"""组合夏普 1.14 归因拆解 (2026-09-21, 任务#11, 纯拆解不耗预算)。

回答: 40/60 组合的夏普 1.14 是谁挣的——股票腿 / CB 腿 / 择时 /
交互项(分散化)。与 backtest_stock_cb_allocation 完全同口径重建
两腿(该脚本是 1.14 的来源), 复现 1.14 为 sanity 锚, 然后拆。
用法: python scripts/sharpe_attribution.py > logs/sharpe_attribution_20260921.log 2>&1
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd
from loguru import logger
logger.remove()

from data.storage import load_daily, load_meta
from backtest_config import DEFAULT_CONFIG
from backtest_engine import run_backtest, make_rebal_dates
from backtest_cb_doublelow import run_bt as cb_run_bt

START, END = "2019-01-01", "2026-08-28"


def metrics(r):
    ann = (1 + r).prod() ** (252 / len(r)) - 1
    sr = r.mean() / r.std(ddof=1) * np.sqrt(252) if r.std() > 0 else 0
    vol = r.std(ddof=1) * np.sqrt(252)
    dd = ((1 + r).cumprod() / (1 + r).cumprod().cummax() - 1).min()
    return ann, sr, vol, dd


def combo(r_s, r_cb, w_s=0.4, dyn=None):
    """月频再平衡(与 allocation combo2 同构), dyn=pos_ratio 序列时启用动态。"""
    j = pd.DataFrame({"s": r_s, "cb": r_cb}).dropna()
    nav = 1.0
    ws = w_s
    cur = None
    out = []
    for dt, row in j.iterrows():
        ym = (dt.year, dt.month)
        if ym != cur:
            cur = ym
            ws = w_s if dyn is None else float(dyn[dt])
        r = row["s"] * ws + row["cb"] * (1 - ws)
        nav *= (1 + r)
        ws = (ws * (1 + row["s"])) / (ws * (1 + row["s"]) + (1 - ws) * (1 + row["cb"]))
        out.append(nav)
    return pd.Series(out, index=j.index)


def main():
    # ── 股票腿(与 allocation 同构) ──
    meta = load_meta("stock_info_full")
    codes = meta["code"].tolist() if not meta.empty else []
    prices, amounts = {}, {}
    for code in codes:
        try:
            d = load_daily(code, START, END)
            if d.empty:
                continue
            d["date"] = pd.to_datetime(d["date"])
            d = d.set_index("date").sort_index()
            cl = pd.to_numeric(d["close"], errors="coerce").dropna()
            amt = pd.to_numeric(d.get("amount", pd.Series(dtype=float)),
                                errors="coerce")
            if len(cl) >= 250:
                prices[code] = cl
                if len(amt) >= 250:
                    amounts[code] = amt
        except Exception:
            pass
    panel = pd.DataFrame(prices).sort_index()
    ap = pd.DataFrame(amounts).sort_index()
    ic = load_meta("csi800_index").set_index("date")["close"].sort_index()
    ic.index = pd.to_datetime(ic.index)
    sh = load_daily("SH000001", "2014-06-01", END)
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))
    rebal = [d for d in make_rebal_dates(cal, "biweekly") if START <= d <= END]
    nav_s, _ = run_backtest(panel, ap, rebal, DEFAULT_CONFIG, ic)
    r_s = nav_s.pct_change().dropna()

    # ── CB 腿 ──
    nav_cb = cb_run_bt("dblow")
    r_cb = nav_cb.pct_change().dropna()

    j = pd.DataFrame({"s": r_s, "cb": r_cb}).dropna()
    print(f"交集窗口 {j.index[0].date()} → {j.index[-1].date()}, {len(j)} 天")

    a_s = metrics(j["s"])
    a_cb = metrics(j["cb"])
    rho = float(j["s"].corr(j["cb"]))
    print(f"\n===== 腿级 =====")
    print(f"股票腿: 年化{a_s[0]*100:+.2f}% 夏普{a_s[1]:.2f} "
          f"波动{a_s[2]*100:.1f}% 回撤{a_s[3]*100:.1f}%")
    print(f"CB 腿:  年化{a_cb[0]*100:+.2f}% 夏普{a_cb[1]:.2f} "
          f"波动{a_cb[2]*100:.1f}% 回撤{a_cb[3]*100:.1f}%")
    print(f"日收益相关 ρ = {rho:+.3f}")

    # ── 组合复现(sanity 锚: 40/60 M频 应为 1.14) ──
    nav4060 = combo(j["s"], j["cb"], 0.4)
    r_p = nav4060.pct_change().dropna()
    a_p = metrics(r_p)
    print(f"\n===== 组合 =====")
    print(f"40/60 M频: 年化{a_p[0]*100:+.2f}% 夏普{a_p[1]:.2f} "
          f"波动{a_p[2]*100:.1f}% 回撤{a_p[3]*100:.1f}% "
          f"(锚: +10.62%/1.14/-8.9%)")

    # ── 分解 ──
    print(f"\n===== 归因拆解 =====")
    w_s, w_cb = 0.4, 0.6
    wa = w_s * a_s[1] + w_cb * a_cb[1]          # 资本加权平均夏普(无分散化)
    print(f"资本加权平均夏普(无分散化假设): {wa:.2f}")
    # 波动恒等式: 组合波动 vs 加权波动
    var_p = (w_s * a_s[2])**2 + (w_cb * a_cb[2])**2 + 2 * w_s * w_cb * rho * a_s[2] * a_cb[2]
    vol_p_no_corr = np.sqrt((w_s * a_s[2])**2 + (w_cb * a_cb[2])**2)
    print(f"加权波动(ρ=0 假设) {vol_p_no_corr*100:.1f}% | "
          f"实际组合波动 {a_p[2]*100:.1f}% (ρ={rho:+.2f} 的压低)")
    # 超额收益分解
    ret_p = w_s * a_s[0] + w_cb * a_cb[0]
    print(f"加权年化收益 {ret_p*100:+.2f}% vs 组合实际 {a_p[0]*100:+.2f}% "
          f"(差 {100*(a_p[0]-ret_p):+.2f}pp = 再平衡交互项)")
    div = a_p[1] - wa
    print(f"** 分散化贡献 = 组合夏普 {a_p[1]:.2f} − 加权均值 {wa:.2f} "
          f"= {div:+.2f} **")

    # ── 择时对照 ──
    pos = pd.Series(index=j.index, dtype=float)
    for dt in j.index:
        try:
            hist = ic[ic.index <= dt].dropna()
            ratio = float(hist.iloc[-1] / hist.rolling(200).mean().iloc[-1]) \
                if len(hist) >= 200 else 0.7
            pos[dt] = 1.0 if ratio >= 1.05 else 0.85 if ratio >= 1.02 \
                else 0.7 if ratio >= 0.98 else 0.5 if ratio >= 0.95 else 0.3
        except Exception:
            pos[dt] = 0.7
    nav_dyn = combo(j["s"], j["cb"], 0.4, dyn=pos)
    a_dyn = metrics(nav_dyn.pct_change().dropna())
    print(f"\n===== 择时对照(动态配比 vs 固定40/60) =====")
    print(f"动态(股=pos_ratio): 年化{a_dyn[0]*100:+.2f}% 夏普{a_dyn[1]:.2f} "
          f"回撤{a_dyn[3]*100:.1f}% → 择时贡献 {a_dyn[1]-a_p[1]:+.2f} 夏普")
    print("(附: 股票腿内部择时 A3−A1c 已测=diff夏普+0.27, σ=0.11 不可分辨)")


if __name__ == "__main__":
    main()
