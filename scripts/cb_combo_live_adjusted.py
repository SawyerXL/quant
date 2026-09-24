"""组合配比 活链口径定稿表 (2026-09-24 22:0x, CB 核对收尾件·挂账①)。

V3 组合的 CB 腿 = monthly_zero(快照日调仓+退市全损)。活链实际 =
双周调仓(月中+月末倒数第二, 月初快照) + 到期自动兑付(≈面值回收)。
用 cb_sched_compare 的 nav_biweekly_par 替换 CB 腿日收益, 重算全网格
(与 backtest_stock_cb_allocation.combo2 同语义: 月频回目标权重+日漂移+0成本;
0.13% 再平衡成本敏感性同附)。
"""
import pandas as pd

j = pd.read_csv("logs/stock_cb_joint_returns.csv", index_col=0, parse_dates=True)
ncb = pd.read_csv("logs/cb_sched_compare_nav.csv", index_col=0, parse_dates=True)
r_cb_live = ncb["nav_biweekly_par"].pct_change().dropna()
r_cb_v3 = ncb["nav_monthly_zero"].pct_change().dropna()

j2 = j.copy()
j2["cb_live"] = r_cb_live.reindex(j.index)
j2 = j2.dropna(subset=["cb_live"])


def combo2(jj, cbcol, fixed_w, rebal_cost=0.0):
    nav = 1.0
    w_s = fixed_w
    results = []
    month_cur = None
    for i, (dt, row) in enumerate(jj.iterrows()):
        ym = (dt.year, dt.month)
        if ym != month_cur:
            month_cur = ym
            nav *= (1 - abs(w_s - fixed_w) * rebal_cost)
            w_s = fixed_w
        r = row["s"] * w_s + row[cbcol] * (1 - w_s)
        nav *= (1 + r)
        w_s = (w_s * (1 + row["s"])) / (w_s * (1 + row["s"]) + (1 - w_s) * (1 + row[cbcol]))
        results.append(nav)
    return pd.Series(results, index=jj.index)


def m(nav):
    r = nav.pct_change().dropna()
    days = (nav.index[-1] - nav.index[0]).days
    ann = (nav.iloc[-1] ** (365 / days) - 1)
    sr = r.mean() / r.std() * np.sqrt(252) if r.std() > 0 else 0
    dd = (nav / nav.cummax() - 1).min()
    return ann, sr, dd


import numpy as np

print(f"{'方案':<34}{'年化':>9}{'夏普':>8}{'最大回撤':>10}  熔断")
print("─" * 78)
for name, nav in [
    ("股票单独(现金2%)", j2["s"].add(1).cumprod()),
    ("CB单独 V3月度全损", (1 + r_cb_v3.reindex(j2.index).dropna()).cumprod()),
    ("CB单独 活链双周+面值", (1 + j2["cb_live"]).cumprod()),
]:
    a, s_, d_ = m(nav)
    print(f"{name:<34}{a*100:>+8.2f}%{s_:>8.2f}{d_*100:>9.2f}%"
          f"  {'⚠破线' if d_ <= -0.15 else ''}")
print()
for label, cbcol in [("CB=V3月度全损(现引用)", "cb"),
                     ("CB=活链双周+面值", "cb_live")]:
    print(f"{label}:")
    for w in (0.3, 0.4, 0.5, 0.6):
        a, s_, d_ = m(combo2(j2, cbcol, w))
        print(f"  股{w:.0%}/CB{1-w:.0%}: {a*100:>+7.2f}%  夏普{s_:.2f}  回撤{d_*100:>+7.2f}%"
              f"{'  ⚠破线' if d_ <= -0.15 else ''}")
    a, s_, d_ = m(combo2(j2, cbcol, 0.4, rebal_cost=0.0013))
    print(f"  股40%/CB60%+0.13%再平衡成本: {a*100:>+7.2f}%  夏普{s_:.2f}  回撤{d_*100:>+7.2f}%")
    print()
print(f"窗口: {j2.index[0].date()} → {j2.index[-1].date()}, {len(j2)}天")
print("注: CB=活链口径沿用 biweekly_par(面值回收) 臂; 与 V3 口径差异已入账本 ③④ 节。")
