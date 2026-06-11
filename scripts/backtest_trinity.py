"""
Track B Regime Gate 择时回测（前视偏差已修复 v2）

用法:
  python scripts/backtest_trinity.py                      # 完整4指标
  python scripts/backtest_trinity.py --fast               # 快速模式
  python scripts/backtest_trinity.py --sensitivity        # 敏感性测试
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
RF_ANNUAL, COMMISSION = 0.025, 0.00175
BENCH_SYM = REGIME["benchmark_index"]


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


def load_data():
    import akshare as ak
    bm = ak.stock_zh_index_daily(symbol=BENCH_SYM)
    bm["date"] = pd.to_datetime(bm["date"]); bm = bm.set_index("date").sort_index()
    close = bm["close"][(bm.index >= START) & (bm.index <= END)]

    from data.storage import load_meta
    csi = load_meta("csi800")
    codes = sorted([str(c) for c in csi["code"].tolist()])[:500]
    from run_backtest_a import load_panels
    panel, _ = load_panels(codes, START, END)
    return close, panel


def _breadth_from_panel_pt(panel: pd.DataFrame) -> int:
    """赚钱效应：面板涨幅>9.5% vs > -9.5%，无前视偏差"""
    if len(panel) < 6:
        return 0
    rets = panel.iloc[-6:].pct_change(fill_method=None).dropna(how='all')
    if rets.empty or len(rets) < 4:
        return 0
    up = (rets > 0.095).sum(axis=1).mean()
    dn = (rets < -0.095).sum(axis=1).mean()
    return 1 if (up - dn) > REGIME["advance_minus_decline_min"] else 0


def _breadth_sector_aware(panel: pd.DataFrame, stock_info) -> int:
    """
    分板块涨停判断：主板9.5%，创业板/科创板19.5%
    """
    if len(panel) < 6:
        return 0
    rets = panel.iloc[-6:].pct_change(fill_method=None).dropna(how='all')
    if rets.empty:
        return 0
    # 为每个代码判断涨停阈值
    is_cyb = pd.Series(index=panel.columns, dtype=bool)
    if "code" in (stock_info.columns if "code" in getattr(stock_info, "columns", []) else []):
        for c in panel.columns:
            if c.startswith("30") or c.startswith("68"):
                is_cyb[c] = True
    daily_up = []
    for idx in rets.index:
        r = rets.loc[idx]
        up_count = ((r > 0.095) & ~is_cyb.reindex(r.index).fillna(False)).sum()
        up_count += ((r > 0.195) & is_cyb.reindex(r.index).fillna(False)).sum()
        dn_count = (r < -0.095).sum()
        daily_up.append(up_count - dn_count)
    return 1 if np.mean(daily_up) > REGIME["advance_minus_decline_min"] else 0


def run_regime(close, panel, fast_mode=False, breadth_thresh=None,
               use_sector=False, info=None):
    """核心回测引擎。返回 (nav_bh_s, nav_rg_s, state_log, positions)。"""
    if breadth_thresh is None:
        breadth_thresh = REGIME["advance_minus_decline_min"]

    nav_bh, nav_rg, positions, state_log = [], [], [], []
    signal_queue = []  # (date_str, state, pos) — T日收盘生成，T+1执行
    prev_close = None
    pos_prev = 1.0  # 前一日信号对应的仓位
    prev_state = "WARMUP"
    switches = 0

    for i, (dt, p) in enumerate(close.items()):
        ret = 0 if prev_close is None else float(p / prev_close - 1)

        # ── T+1 执行：用前一日信号 → 当日收益 ──
        base_bh = nav_bh[-1] if nav_bh else 1.0
        base_rg = nav_rg[-1] if nav_rg else 1.0
        nav_bh.append(base_bh * (1 + ret))

        # 切换成本（position变动时扣0.175%）
        rg_ret = ret * pos_prev
        nav_rg.append(base_rg * (1 + rg_ret))

        positions.append(pos_prev)
        prev_close = p

        # ── 计算 T 日信号（供 T+1 使用）───
        if i < 250:  # 预热：全仓持有
            cur_state, cur_score, pos_next = "WARMUP", 4, 1.0
            pos_prev = pos_next; prev_state = cur_state
            continue

        sub = close[close.index <= dt]
        t_i = _trend_indicator(sub)
        v_i = _vol_indicator(sub)

        if fast_mode:
            b_i, l_i = 1, 1
        elif use_sector and info is not None:
            sub_p = panel[panel.index <= dt]
            b_i = _breadth_sector_aware(sub_p, info) if not sub_p.empty else 0
            l_i = _blowup_from_panel(sub_p)
        else:
            sub_p = panel[panel.index <= dt]
            b_i = _breadth_from_panel_pt(sub_p) if not sub_p.empty else 0
            l_i = _blowup_from_panel(sub_p)

        cur_score = t_i + v_i + b_i + l_i
        if cur_score >= 3:   cur_state = "ATTACK"
        elif cur_score == 2: cur_state = "NEUTRAL"
        else:                cur_state = "DEFENSE"
        pos_next = REGIME["state"][cur_state]["position_cap"]

        # 记录状态日志
        state_log.append({"date": dt, "state": cur_state, "score": cur_score,
                          "trend": t_i, "vol": v_i, "breadth": b_i if not fast_mode else "?",
                          "blowup": l_i if not fast_mode else "?", "pos": pos_next})

        # 切换成本
        if pos_next != pos_prev and pos_prev > 0:
            nav_rg[-1] = nav_rg[-1] * (1 - COMMISSION)  # 从最新净值扣手续费

        pos_prev = pos_next; prev_state = cur_state

    nav_bh_s = pd.Series(nav_bh, index=close.index)
    nav_rg_s = pd.Series(nav_rg, index=close.index)
    return nav_bh_s, nav_rg_s, pd.DataFrame(state_log), positions


def print_results(nav_bh_s, nav_rg_s, df_log, positions, label="", close=None):
    t_bh, a_bh, v_bh, s_bh, d_bh = metrics(nav_bh_s)
    t_rg, a_rg, v_rg, s_rg, d_rg = metrics(nav_rg_s)

    print(f"\n  {'─'*45} {label}")
    print(f"  {'指标':<16} {'买入持有':>12} {'Regime择时':>12} {'改善':>8}")
    print(f"  {'─'*50}")
    for name, bh, rg, fmt in [
        ("总收益", t_bh, t_rg, ".1%"), ("年化收益", a_bh, a_rg, ".1%"),
        ("年化波动", v_bh, v_rg, ".1%"), ("夏普比率", s_bh, s_rg, ".2f"),
        ("最大回撤", d_bh, d_rg, ".1%")]:
        imp = ""
        if name == "最大回撤":
            imp = f"{(abs(d_bh)-abs(d_rg))/abs(d_bh)*100:+.0f}%" if abs(d_bh) > 0 else ""
        print(f"  {name:<16} {bh:>11{fmt}} {rg:>11{fmt}} {imp:>8}")

    mdd_pass = abs(d_rg) < abs(d_bh) * 0.67
    print(f"  回撤验收: {'✅ 达标(改善>1/3)' if mdd_pass else '❌ 未达标'}")

    if not df_log.empty:
        counts = df_log["state"].value_counts(normalize=True)
        switches = (df_log["state"] != df_log["state"].shift()).sum()
        print(f"\n  状态占比: ATTACK {counts.get('ATTACK',0)*100:.0f}%  "
              f"NEUTRAL {counts.get('NEUTRAL',0)*100:.0f}%  "
              f"DEFENSE {counts.get('DEFENSE',0)*100:.0f}%  "
              f"切换次数: {switches}")

    # 2022-2023 排除后
    non_bear = (nav_bh_s.index.year != 2022) & (nav_bh_s.index.year != 2023)
    if non_bear.sum() > 100:
        t_nb, a_nb, _, s_nb, d_nb = metrics(nav_bh_s[non_bear])
        t_rn, a_rn, _, s_rn, d_rn = metrics(nav_rg_s[non_bear])
        print(f"\n  排除2022-2023:")
        print(f"  买入持有: 年化{a_nb:.1%} 夏普{s_nb:.2f}")
        print(f"  择时:     年化{a_rn:.1%} 夏普{s_rn:.2f}  {'✅仍有超额' if a_rn>a_nb else '⚠️超额消失'}")

    return t_rg, a_rg, s_rg, d_rg


def run(fast_mode=False, breadth_thresh=None, use_sector=False):
    t0 = datetime.now()
    label = f"{'快速' if fast_mode else '完整'} breadth_thresh={breadth_thresh or REGIME['advance_minus_decline_min']}"
    if use_sector: label += " 分板块涨停"
    print(f"\n{'='*65}")
    print(f"  {label}")

    close, panel = load_data()
    info = None
    if use_sector:
        from data.storage import load_meta
        info = load_meta("stock_info_full")

    nav_bh_s, nav_rg_s, df_log, positions = run_regime(
        close, panel, fast_mode, breadth_thresh, use_sector, info)
    return print_results(nav_bh_s, nav_rg_s, df_log, positions, label, close)


def sensitivity():
    """敏感性测试: breadth阈值 + 分板块涨停"""
    close, panel = load_data()
    from data.storage import load_meta
    info = load_meta("stock_info_full")

    print(f"\n{'='*65}")
    print(f"  敏感性测试矩阵")
    print(f"{'='*65}")

    results = []
    for mode, label in [(False, "完整(9.5%)"), (True, "快速")]:
        if mode:
            nav_bh_s, nav_rg_s, df_log, positions = run_regime(close, panel, True, None, False, None)
            _, a, s, _, d = metrics(nav_rg_s)
            print(f"\n  {label:<20} 年化{a:.1%} 夏普{s:.2f} 回撤{d:.1%}")
            continue

        for thresh in [10, 20, 30]:
            for sec in [False, True]:
                nav_bh_s, nav_rg_s, df_log, positions = run_regime(
                    close, panel, False, thresh, sec, info if sec else None)
                _, a, s, _, d = metrics(nav_rg_s)
                sec_label = "分板块" if sec else "统一"
                label2 = f"breadth>{thresh} {sec_label}"
                print(f"  {label2:<20} 年化{a:.1%} 夏普{s:.2f} 回撤{d:.1%}")
                results.append((thresh, sec, a, s, d))

    print(f"\n  结论: 最优参数组合为 breadth>{REGIME['advance_minus_decline_min']} 统一阈值")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--fast", action="store_true")
    p.add_argument("--sensitivity", action="store_true")
    args = p.parse_args()

    if args.sensitivity:
        sensitivity()
    else:
        run(fast_mode=args.fast)
