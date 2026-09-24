"""组合 40/60 活链口径重算 (2026-09-24, CB 核对收尾件)。

V3 组合的 CB 腿 = monthly_zero(快照日调仓+退市全损)。活链实际 =
双周调仓(月中+月末倒数第二, 月初快照) + 到期自动兑付(≈面值回收)。
用 cb_sched_compare 的 nav_biweekly_par 替换 CB 腿日收益, 重算 40/60
(与 backtest_stock_cb_allocation.combo2 同语义: 月频回目标权重+日漂移+0成本)。
"""
import pandas as pd
import numpy as np

j = pd.read_csv("logs/stock_cb_joint_returns.csv", index_col=0, parse_dates=True)
ncb = pd.read_csv("logs/cb_sched_compare_nav.csv", index_col=0, parse_dates=True)
r_cb_live = ncb["nav_biweekly_par"].pct_change().dropna()
r_cb_v3 = ncb["nav_monthly_zero"].pct_change().dropna()

j2 = j.copy()
# 双周臂覆盖全历史(91 快照全用), 与 V3 腿在日期上求交
j2["cb_live"] = r_cb_live.reindex(j.index)
j2 = j2.dropna(subset=["cb_live"])


def combo2(jj, cbcol, fixed_w):
    nav = 1.0
    w_s = fixed_w
    results = []
    month_cur = None
    for i, (dt, row) in enumerate(jj.iterrows()):
        ym = (dt.year, dt.month)
        if ym != month_cur:
            month_cur = ym
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


print(f"{'组合臂':<40}{'年化':>9}{'夏普':>8}{'最大回撤':>10}")
for name, cbcol in [("40/60 CB=V3月度全损(现引用)", "cb"),
                    ("40/60 CB=活链双周+面值回收", "cb_live")]:
    a, s_, d_ = m(combo2(j2, cbcol, 0.4))
    halt = " ⚠≥15%熔断线" if d_ <= -0.15 else ""
    print(f"{name:<40}{a*100:>+8.2f}%{s_:>8.2f}{d_*100:>9.2f}%{halt}")
a, s_, d_ = m(combo2(j2, "cb_live", 0.3))
halt = " ⚠≥15%熔断线" if d_ <= -0.15 else ""
print(f"{'30/70 CB=活链双周+面值回收':<40}{a*100:>+8.2f}%{s_:>8.2f}{d_*100:>9.2f}%{halt}")
print(f"\n窗口: {j2.index[0].date()} → {j2.index[-1].date()}, {len(j2)}天")
