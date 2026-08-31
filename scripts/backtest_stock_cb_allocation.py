"""
股票主策略 × 可转债轮动 配比回测（2026-08-31，年度实验预算第1个）。

背景: 主策略MA200择时平均50%+现金(赚2%), CB引擎已验证+7.4%/夏普0.90
(83个月point-in-time, 含退市)。配比问题=把闲置现金换成CB, 不是新信号。

方案:
  基线     = 股票主策略单独(现金2%)
  固定50/50、60/40、40/60 (月频再平衡)
  动态     = 股票权重=MA200五档pos_ratio, CB权重=1-pos_ratio (月频)
窗口: 两引擎交集(CB快照2019起)
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


def main():
    # ── 股票主策略日收益(全期) ──
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

    # ── CB引擎日收益 ──
    nav_cb = cb_run_bt("dblow")
    ret_cb = nav_cb.pct_change().dropna()

    # 交集
    j = pd.DataFrame({"s": ret_s, "cb": ret_cb}).dropna()
    print(f"组合窗口 {j.index[0].date()} → {j.index[-1].date()}, {len(j)}天")

    # MA200 pos_ratio 序列(动态配比用)
    pos = pd.Series(index=j.index, dtype=float)
    for dt in j.index:
        try:
            hist = ic[ic.index <= dt].dropna()
            ratio = float(hist.iloc[-1] / hist.rolling(200).mean().iloc[-1]) if len(hist) >= 200 else 0.7
            if ratio >= 1.05: pos[dt] = 1.00
            elif ratio >= 1.02: pos[dt] = 0.85
            elif ratio >= 0.98: pos[dt] = 0.70
            elif ratio >= 0.95: pos[dt] = 0.50
            else: pos[dt] = 0.30
        except Exception:
            pos[dt] = 0.7

    def combo(w_s_func):
        """w_s_func(date)->股票权重(0~1), CB=1-w_s。日频漂移, 月频目标权重再平衡。"""
        nav = 1.0
        prev = None
        out = []
        for dt in j.index:
            ws = w_s_func(dt)
            if prev is not None:
                r_s = (nav * (1 - prev[0] * 0))  # placeholder
            out.append(nav)
        return pd.Series(out, index=j.index)

    # 直接实现: 日频按上月设定的权重漂移, 月初再平衡
    def combo2(mode, fixed_w=None):
        nav = 1.0
        w_s = 0.5
        results = []
        month_cur = None
        for i, (dt, row) in enumerate(j.iterrows()):
            ym = (dt.year, dt.month)
            if ym != month_cur:
                month_cur = ym
                if mode == "fixed":
                    w_s = fixed_w
                else:
                    w_s = float(pos[dt])
                # 再平衡成本: 权重偏离调整支付佣金
                pass
            r = row["s"] * w_s + row["cb"] * (1 - w_s)
            nav *= (1 + r)
            # 权重漂移
            w_s = (w_s * (1 + row["s"])) / (w_s * (1 + row["s"]) + (1 - w_s) * (1 + row["cb"]))
            results.append(nav)
        return pd.Series(results, index=j.index)

    def m(nav):
        r = nav.pct_change().dropna()
        days = (nav.index[-1] - nav.index[0]).days
        ann = (nav.iloc[-1] ** (365 / days) - 1)
        sr = r.mean() / r.std() * np.sqrt(252) if r.std() > 0 else 0
        dd = (nav / nav.cummax() - 1).min()
        return ann, sr, dd

    print(f"{'方案':<28}{'年化':>9}{'夏普':>8}{'最大回撤':>10}")
    for name, nav in [
        ("股票单独(现金2%)", nav_s.reindex(j.index).ffill()),
        ("CB单独", nav_cb.reindex(j.index).ffill()),
        ("固定50/50", combo2("fixed", 0.5)),
        ("固定60/40(股/CB)", combo2("fixed", 0.6)),
        ("固定40/60", combo2("fixed", 0.4)),
        ("动态(股=pos_ratio)", combo2("dyn")),
    ]:
        ann, sr, dd = m(nav)
        print(f"{name:<28}{ann*100:>+8.2f}%{sr:>8.2f}{dd*100:>9.2f}%")


if __name__ == "__main__":
    main()
