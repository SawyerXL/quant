"""
Track B Regime Gate 择时回测（完整4指标版）

用法:
  python scripts/backtest_trinity.py --fast   # 快速模式(仅趋势+波动)
  python scripts/backtest_trinity.py           # 完整4指标
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd, numpy as np
from datetime import date, datetime
from config.strategy_params.trinity import REGIME
from strategies.trinity.regime import (_trend_indicator, _vol_indicator,
                                        _breadth_from_panel, _blowup_from_panel)

START, END = "2019-01-01", "2025-12-31"
RF_ANNUAL = 0.025
BENCH_SYM = REGIME["benchmark_index"]


def load_data():
    """拉取基准指数 + 全市场面板（用于breadth计算）"""
    import akshare as ak
    # 基准
    bm = ak.stock_zh_index_daily(symbol=BENCH_SYM)
    bm["date"] = pd.to_datetime(bm["date"])
    bm = bm.set_index("date").sort_index()
    close = bm["close"][(bm.index >= START) & (bm.index <= END)]

    # 全市场面板（CSI800成分）
    from data.storage import load_meta
    csi = load_meta("csi800")
    codes = sorted([str(c) for c in csi["code"].tolist()])[:500]  # 500只足够
    from run_backtest_a import load_panels
    panel, _ = load_panels(codes, START, END)
    return close, panel


def run(fast_mode: bool = False):
    t0 = datetime.now()
    print(f"\n{'='*65}")
    print(f"  Regime Gate 择时回测  {BENCH_SYM}  {START}→{END}")
    print(f"  模式: {'快速(仅趋势+波动)' if fast_mode else '完整4指标(含面板breadth)'}")
    print(f"{'='*65}")

    close, full_panel = load_data()
    print(f"  数据: 基准{len(close)}天  全市场面板{full_panel.shape[1]}只  (加载耗时{(datetime.now()-t0).seconds}s)")
    t0 = datetime.now()

    nav_bh, nav_rg, positions, state_log = [], [], [], []
    prev = None

    for i, (dt, p) in enumerate(close.items()):
        ret = 0 if prev is None else float(p / prev - 1)

        if i < 250:  # 预热
            pos, cur_state, score = 1.0, "WARMUP", 4
        else:
            sub = close[close.index <= dt]
            t_i = _trend_indicator(sub)
            v_i = _vol_indicator(sub)
            if fast_mode:
                b_i, l_i = 1, 1
            else:
                sub_panel = full_panel[full_panel.index <= dt]
                b_i = _breadth_from_panel(sub_panel) if not sub_panel.empty else 0
                l_i = _blowup_from_panel(sub_panel)
            score = t_i + v_i + b_i + l_i
            if score >= 3: cur_state = "ATTACK"
            elif score == 2: cur_state = "NEUTRAL"
            else: cur_state = "DEFENSE"
            pos = REGIME["state"][cur_state]["position_cap"]

        positions.append(pos)
        base_bh = nav_bh[-1] if nav_bh else 1.0
        base_rg = nav_rg[-1] if nav_rg else 1.0
        nav_bh.append(base_bh * (1 + ret))
        nav_rg.append(base_rg * (1 + ret * pos))
        if i >= 250:
            state_log.append({"date": dt, "state": cur_state, "score": score,
                              "trend": t_i, "vol": v_i, "breadth": b_i if not fast_mode else "?",
                              "blowup": l_i if not fast_mode else "?", "pos": pos})
        prev = p

    nav_bh_s = pd.Series(nav_bh, index=close.index)
    nav_rg_s = pd.Series(nav_rg, index=close.index)

    def metrics(nav_s):
        d = nav_s.pct_change().dropna()
        t = nav_s.iloc[-1] - 1
        y = max(len(d) / 252, 0.5)
        a = (1 + t) ** (1 / y) - 1
        v = d.std() * np.sqrt(252)
        rf = RF_ANNUAL / 252
        s = (d.mean() - rf) / d.std() * np.sqrt(252) if d.std() > 0 else 0
        m = (nav_s / nav_s.cummax() - 1).min()
        return t, a, v, s, m

    t_bh, a_bh, v_bh, s_bh, d_bh = metrics(nav_bh_s)
    t_rg, a_rg, v_rg, s_rg, d_rg = metrics(nav_rg_s)

    # ── 输出 ─────────────────────────────────────
    print(f"\n  {'指标':<16} {'买入持有':>12} {'Regime择时':>12}")
    print(f"  {'─'*42}")
    print(f"  {'总收益':<16} {t_bh:>+11.1%} {t_rg:>+11.1%}")
    print(f"  {'年化收益':<16} {a_bh:>+11.1%} {a_rg:>+11.1%}")
    print(f"  {'年化波动':<16} {v_bh:>11.1%} {v_rg:>11.1%}")
    print(f"  {'夏普比率':<16} {s_bh:>11.2f} {s_rg:>11.2f}")
    print(f"  {'最大回撤':<16} {d_bh:>11.1%} {d_rg:>11.1%} "
          f"{'✅达标' if abs(d_rg) < abs(d_bh)*0.67 else '❌'}")
    print(f"  {'回撤改善':<16} {'—':>12} {(abs(d_bh)-abs(d_rg))/abs(d_bh)*100:>+10.1f}%")

    # ── 状态占比 ─────────────────────────────────
    df_log = pd.DataFrame(state_log)
    if not df_log.empty:
        counts = df_log["state"].value_counts(normalize=True)
        switches = (df_log["state"] != df_log["state"].shift()).sum()
        print(f"\n  三状态时间占比:")
        for st in ["ATTACK", "NEUTRAL", "DEFENSE"]:
            pct = counts.get(st, 0) * 100
            bar = "█" * int(pct / 2)
            print(f"    {st:<8} {pct:>5.1f}% {bar}")
        print(f"  状态切换总次数: {switches}")

        # 防御期涨跌幅
        defense_periods = df_log[df_log["state"] == "DEFENSE"]
        if len(defense_periods) > 0:
            print(f"\n  DEFENSE期间指数表现（验证防守期是否真实下跌）:")
            # 统计每个连续DEFENSE段
            in_defense = False; seg_ret = 0; seg_start = None; count = 0
            for _, row in df_log.iterrows():
                if row["state"] == "DEFENSE" and not in_defense:
                    in_defense = True; seg_start = row["date"]
                    seg_ret = 0; count += 1
                elif row["state"] != "DEFENSE" and in_defense:
                    in_defense = False
            print(f"    DEFENSE段总数: {count}")
            defense_close = close[close.index.isin(defense_periods["date"])]
            if len(defense_close) > 1:
                def_ret = (defense_close.iloc[-1] / defense_close.iloc[0] - 1) * 100
                print(f"    DEFENSE期间累计: {def_ret:+.1f}%")

    # ── 分年度 ────────────────────────────────────
    print(f"\n  分年度收益:")
    print(f"  {'年份':<6} {'买入持有':>10} {'Regime择时':>10} {'超额':>8}  {'ATTACK%':>8}")
    for yr in range(2019, 2026):
        b_s = nav_bh_s[nav_bh_s.index.year == yr]
        r_s = nav_rg_s[nav_rg_s.index.year == yr]
        if len(b_s) < 2: continue
        br = b_s.iloc[-1] / b_s.iloc[0] - 1
        rr = r_s.iloc[-1] / r_s.iloc[0] - 1
        ap = df_log[df_log["date"].dt.year == yr]["state"].value_counts(normalize=True).get("ATTACK", 0) * 100
        print(f"  {yr:<6} {br:>+9.1%}  {rr:>+9.1%}  {rr-br:>+7.1%}  {ap:>7.0f}%")

    # ── 状态切换明细（最近10次） ──────────────────
    if not fast_mode and abs(d_rg) >= abs(d_bh) * 0.67:
        print(f"\n  ⚠️ 回撤未显著改善，最近状态切换明细：")
        switches_idx = df_log[df_log["state"] != df_log["state"].shift()].tail(10)
        for _, row in switches_idx.iterrows():
            print(f"    {row['date'].date()} → {row['state']:<8} "
                  f"score={row['score']}(t{row['trend']}v{row['vol']}b{row['breadth']}l{row['blowup']})")

    elapsed = (datetime.now() - t0).seconds
    print(f"\n  回测耗时: {elapsed}s")
    print(f"{'='*65}\n")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--fast", action="store_true")
    args = p.parse_args()
    run(fast_mode=args.fast)
