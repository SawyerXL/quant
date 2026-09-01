"""
40/60 配比结论复验（2026-08-31，用户要求反复验证）。

四层:
  ① 数据完整性: CB清洗后规模/快照新鲜度/组合窗口
  ② 子窗口稳健性: 2019-21转债大年 / 2022-24.6熊市 / 2024.7-26.8 —— 40/60须各段占优
  ③ 配比网格: 30/70 40/60 50/50 60/40 —— 40/60须是平台非尖点
  ④ 成本: 再平衡成本计入(月频) + 再平衡频率敏感性(月/季)
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
from backtest_engine import run_backtest, make_rebal_dates, calc_metrics
from backtest_cb_doublelow import run_bt as cb_run_bt

START, END = "2019-01-01", "2026-08-28"
REBAL_COST = 0.0013   # 组合再平衡双边成本


def main():
    # ── ① 数据完整性 ──
    snaps = load_meta("cb_snapshots")
    snaps["snap_date"] = pd.to_datetime(snaps["snap_date"])
    print(f"① 数据: CB快照{len(snaps)}条/{snaps['snap_date'].nunique()}个月, "
          f"最新{snaps['snap_date'].max().date()}")
    cb = cb_run_bt("dblow")
    cb_eq = cb_run_bt("equal")
    print(f"   CB引擎窗口 {cb.index[0].date()} → {cb.index[-1].date()}, {len(cb)}天")

    # 股票主策略
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
            amt = pd.to_numeric(d.get("amount", pd.Series(dtype=float)), errors="coerce")
            if len(cl) >= 250:
                prices[code] = cl
                if len(amt) >= 250:
                    amounts[code] = amt
        except Exception:
            pass
    panel = pd.DataFrame(prices).sort_index()
    ap = pd.DataFrame(amounts).sort_index()
    ic = load_meta("csi800_index")
    ic = ic.set_index("date")["close"].sort_index()
    ic.index = pd.to_datetime(ic.index)
    sh = load_daily("000001", "2014-06-01", END)
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))
    rebal = [d for d in make_rebal_dates(cal, "biweekly") if START <= d <= END]
    nav_s, _ = run_backtest(panel, ap, rebal, DEFAULT_CONFIG, ic)
    ret_s = nav_s.pct_change().dropna()

    # 对齐两引擎日收益
    for cbname, nav_cb in [("dblow", cb), ("equal", cb_eq)]:
        j = pd.DataFrame({"s": ret_s, "cb": nav_cb.pct_change().dropna()}).dropna()
        print(f"\n组合窗口({cbname}) {j.index[0].date()} → {j.index[-1].date()}, {len(j)}天")

        def combo(w_s, reb_freq="M", cost=REBAL_COST):
            nav = 1.0
            w_cur = w_s
            last_rb = None
            out = []
            for i, (dt, row) in enumerate(j.iterrows()):
                period = dt.to_period(reb_freq)
                if period != last_rb:
                    if last_rb is not None:
                        drift = abs(w_cur - w_s)
                        nav *= (1 - drift * cost)
                    w_cur = w_s
                    last_rb = period
                r = row["s"] * w_cur + row["cb"] * (1 - w_cur)
                nav *= (1 + r)
                w_cur = (w_cur * (1 + row["s"])) / (w_cur * (1 + row["s"]) + (1 - w_cur) * (1 + row["cb"]))
                out.append(nav)
            return pd.Series(out, index=j.index)

        def m(nav):
            r = nav.pct_change().dropna()
            days = (nav.index[-1] - nav.index[0]).days
            return (nav.iloc[-1] ** (365 / days) - 1) * 100, \
                   (r.mean() / r.std() * np.sqrt(252) if r.std() > 0 else 0), \
                   (nav / nav.cummax() - 1).min() * 100

        # ── ② 子窗口 ──
        print(f"② 子窗口(40/60 vs 股票单独):")
        segs = [("全期", j.index[0], j.index[-1]),
                ("2019-21转债大年", "2019-01-01", "2021-12-31"),
                ("2022-24.6熊市", "2022-01-01", "2024-06-30"),
                ("2024.7-26.8", "2024-07-01", "2026-08-28")]
        for name, lo, hi in segs:
            js = j[(j.index >= lo) & (j.index <= hi)]
            if len(js) < 100:
                continue
            a40 = combo(0.4).reindex(js.index)
            a40 = (1 + a40.pct_change().fillna(0)).cumprod()  # 独立段重算
            # 直接用段内净值
            nav40 = combo_seg(js, 0.4)
            navs = (1 + js["s"]).cumprod()
            a40m, s40m, d40m = m(nav40)
            asm, ssm, dsm = m(navs)
            print(f"  {name:<16} 股票{asm:>+7.2f}%/{ssm:.2f}/-{abs(dsm):.0f}%  vs  40/60 {a40m:>+7.2f}%/{s40m:.2f}/-{abs(d40m):.0f}%")

        # ── ③ 配比网格 ──
        print(f"③ 配比网格(全期):")
        for w in (0.3, 0.4, 0.5, 0.6):
            a, s_, d_ = m(combo(w))
            print(f"  {w:.0%}/{1-w:.0%}: 年化{a:+.2f}% 夏普{s_:.2f} 回撤{d_:.1f}%")

        # ── ④ 再平衡频率 ──
        print(f"④ 再平衡频率(40/60):")
        for freq in ("M", "Q"):
            a, s_, d_ = m(combo(0.4, reb_freq=freq))
            print(f"  {freq}频: 年化{a:+.2f}% 夏普{s_:.2f} 回撤{d_:.1f}%")


def combo_seg(j, w_s, cost=REBAL_COST):
    """子窗口独立组合(从1起算)。"""
    nav = 1.0
    w_cur = w_s
    last_rb = None
    out = []
    for i, (dt, row) in enumerate(j.iterrows()):
        period = dt.to_period("M")
        if period != last_rb:
            if last_rb is not None:
                nav *= (1 - abs(w_cur - w_s) * cost)
            w_cur = w_s
            last_rb = period
        r = row["s"] * w_cur + row["cb"] * (1 - w_cur)
        nav *= (1 + r)
        w_cur = (w_cur * (1 + row["s"])) / (w_cur * (1 + row["s"]) + (1 - w_cur) * (1 + row["cb"]))
        out.append(nav)
    return pd.Series(out, index=j.index)


if __name__ == "__main__":
    main()
