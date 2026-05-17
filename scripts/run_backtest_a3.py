"""
策略A-3：在A-2基础上加入"新仓保护期"。

问题诊断（来自持仓天数分析）：
  - 47% 的交易只持仓 2-4周（单笔期望 -3.15%，负收益）
  - >4周 持仓胜率50.5%，单笔期望 +5.98%
  - 问题根源：太多股票刚入仓就被下一期换出，形成"换手转盘"

优化方案（仅加一条规则，其他完全继承A-2）：
  GRACE_PERIODS = 2    持仓不足2期（约4周）的股票受保护
  GRACE_THRESHOLD = 0.15  替换方需得分高出15%才能换出新仓股

关键区别（vs 策略C的失败）：
  - 策略C：所有换仓都需要10%门槛 → 全局"冻结"，错过风格切换
  - 策略A-3：只保护"新入仓≤2期"的股票 → 老仓位自由替换，只防"旋转门"

运行：
    python scripts/run_backtest_a3.py
    python scripts/run_backtest_a3.py --compare
"""
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd
from loguru import logger
from data.storage import load_meta

from run_backtest_a2 import (
    compute_score_a2, select_industry_balanced, compute_weights,
    get_position_ratio, _make_rebal_dates,
    MAX_IND_SLOT, SECTOR_BOOST,
)
from run_backtest_a import (
    load_panels, calc_metrics,
    BACKTEST_START, COMMISSION, MIN_BARS, CASH_YIELD,
    PERIOD_STOP, TRAILING_STOP, MA_PERIOD,
)

logger.add("logs/backtest_a3.log", rotation="1 day", retention="30 days")

BACKTEST_END     = os.getenv("BACKTEST_END", "")
N_HOLDINGS       = int(os.getenv("N_HOLDINGS", "30"))
GRACE_PERIODS    = int(os.getenv("GRACE_PERIODS", "2"))      # 保护期：2个调仓周期
GRACE_THRESHOLD  = float(os.getenv("GRACE_THRESHOLD", "0.15")) # 替换门槛：15%
USE_REGIME       = os.getenv("USE_REGIME", "1") == "1"
REBAL_FREQ       = os.getenv("REBAL_FREQ", "biweekly")


def select_with_grace(
    score: pd.Series,
    current_holdings: list[str],
    hold_counts: dict[str, int],
    n: int,
    grace_periods: int,
    grace_threshold: float,
) -> list[str]:
    """
    行业均衡选股 + 新仓保护期：
    - 持仓 < grace_periods 期的股票，替换者需高出 grace_threshold 才能换出
    - 持仓 >= grace_periods 期的股票，正常替换（无门槛）
    """
    if not current_holdings:
        return select_industry_balanced(score, None, n, MAX_IND_SLOT)

    # 先做行业均衡选股，得到候选前N（用更宽的候选池 1.5N）
    wider_n   = min(int(n * 1.5), len(score))
    cands_wide = score.nlargest(wider_n).index.tolist()
    normal_top = set(score.nlargest(n).index.tolist())
    cur_set    = set(current_holdings)

    # 不在正常前N的当前持仓：候选"被换出"
    remove_cands = sorted(
        [c for c in current_holdings if c not in normal_top],
        key=lambda c: score.get(c, -np.inf)   # 得分最低的最先候选被换
    )
    # 正常前N里不在当前持仓的：候选"换入"
    add_cands = sorted(
        [c for c in cands_wide if c not in cur_set],
        key=lambda c: score.get(c, -np.inf), reverse=True
    )

    result = list(cur_set)

    for rm, add in zip(remove_cands, add_cands):
        if rm not in result:
            continue
        rm_score  = score.get(rm,  -np.inf)
        add_score = score.get(add, -np.inf)
        periods   = hold_counts.get(rm, 0)

        # 新仓保护：持仓期 < grace_periods 时，换入方需高出 grace_threshold
        if periods < grace_periods:
            needed = rm_score * (1 + grace_threshold) if rm_score > 0 else 0
            if add_score >= needed and add not in result:
                result.remove(rm)
                result.append(add)
        else:
            # 老仓：正常换出（无门槛）
            if add not in result:
                result.remove(rm)
                result.append(add)

    # 补足至 N
    for c in score.sort_values(ascending=False).index:
        if len(result) >= n:
            break
        if c not in result:
            result.append(c)

    return result[:n]


def run_backtest_a3(
    panel: pd.DataFrame,
    rebalance_dates: list,
    amount_panel: pd.DataFrame | None,
    index_close: pd.Series | None,
    stock_info: pd.DataFrame | None,
) -> pd.Series:
    all_dates    = panel.index
    port_rets    = pd.Series(0.0, index=all_dates)
    cur_weights: dict[str, float] = {}
    hold_counts:  dict[str, int]  = {}   # {code: 已持仓调仓期数}
    cumul_nav    = 1.0
    entry_hwm    = 1.0
    nav_since    = 1.0
    pos_ratio    = 1.0
    rebal_set    = set(str(d.date()) if hasattr(d, "date") else d for d in rebalance_dates)

    for i, date in enumerate(all_dates):
        date_str = str(date.date())

        if date_str in rebal_set and i >= MIN_BARS:
            pos_ratio = get_position_ratio(index_close, date) if index_close is not None else 1.0

            if pos_ratio <= 0.30:
                # 极熊：清仓转货基，但仍计最低仓位收益
                cur_weights = {}
                hold_counts = {}
                nav_since   = 1.0
                entry_hwm   = cumul_nav
            else:
                score = compute_score_a2(panel, date, amount_panel, stock_info)
                if len(score) >= N_HOLDINGS:
                    old_hold = list(cur_weights.keys())

                    # ★ 核心改进：行业均衡选股 + 新仓保护期
                    new_hold = select_with_grace(
                        score, old_hold, hold_counts,
                        N_HOLDINGS, GRACE_PERIODS, GRACE_THRESHOLD
                    )

                    # 更新持仓期数
                    new_set = set(new_hold)
                    old_set = set(old_hold)
                    new_counts = {}
                    for c in new_hold:
                        new_counts[c] = hold_counts.get(c, 0) + 1 if c in old_set else 1
                    hold_counts = new_counts

                    # 权重：得分加权 + 主线板块
                    raw_w = compute_weights(new_hold, score, stock_info, SECTOR_BOOST)
                    new_w = {c: w * pos_ratio for c, w in raw_w.items()}

                    # 换手成本
                    enter = sum(new_w.get(c, 0) for c in new_set - old_set)
                    exit_ = sum(cur_weights.get(c, 0) for c in old_set - new_set)
                    port_rets.iloc[i] -= (enter + exit_) / 2 * COMMISSION * 2

                    if not cur_weights:
                        entry_hwm = cumul_nav
                    cur_weights = new_w
                nav_since = 1.0

        # 止损
        if cur_weights and i > 0:
            if nav_since <= (1 + PERIOD_STOP) or (cumul_nav / entry_hwm - 1) <= TRAILING_STOP:
                cur_weights = {}
                hold_counts = {}
                nav_since   = 1.0
                entry_hwm   = cumul_nav

        # 加权日收益
        if cur_weights and i > 0:
            ret = 0.0
            for code, w in cur_weights.items():
                pp = panel.iloc[i - 1].get(code)
                cp = panel.iloc[i].get(code)
                if pp and cp and not pd.isna(pp) and not pd.isna(cp) and pp > 0:
                    ret += w * (cp / pp - 1)
            port_rets.iloc[i] += ret
        # 现金/货基收益
        cash_r = max(0, 1.0 - sum(cur_weights.values())) if cur_weights else 1.0
        port_rets.iloc[i] += cash_r * CASH_YIELD / 252

        nav_since = nav_since * (1 + port_rets.iloc[i])
        cumul_nav = cumul_nav * (1 + port_rets.iloc[i])

    return (1 + port_rets).cumprod()


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--compare", action="store_true")
    args = parser.parse_args()

    global BACKTEST_END
    cal_df = load_meta("trade_calendar")
    if not BACKTEST_END:
        BACKTEST_END = sorted(cal_df["trade_date"].tolist())[-1]

    logger.info("=" * 68)
    logger.info(f"策略A-3  {BACKTEST_START} → {BACKTEST_END}")
    logger.info(f"A-2 全部优化 + 新仓保护期{GRACE_PERIODS}期({GRACE_THRESHOLD:.0%}门槛)")
    logger.info("=" * 68)

    calendar = [d for d in cal_df["trade_date"].tolist()
                if BACKTEST_START <= d <= BACKTEST_END]
    rebal_dates = _make_rebal_dates(calendar, REBAL_FREQ)
    logger.info(f"调仓日期：{len(rebal_dates)} 个")

    csi800 = load_meta("csi800")
    panel, amount_panel = load_panels(sorted(csi800["code"].tolist()),
                                      BACKTEST_START, BACKTEST_END)
    logger.info(f"价格矩阵：{panel.shape[0]}天 × {panel.shape[1]}只")

    stock_info = load_meta("stock_info_full")
    stock_info = None if stock_info.empty else stock_info

    idx_df = load_meta("csi800_index")
    if idx_df.empty:
        index_close = None
    else:
        idx_df["date"] = pd.to_datetime(idx_df["date"])
        index_close    = idx_df.set_index("date")["close"].sort_index()

    logger.info("运行策略A-3...")
    nav_a3 = run_backtest_a3(panel, rebal_dates, amount_panel, index_close, stock_info)

    year_end = int(BACKTEST_END[:4])
    logger.info("── 策略A-3 分年度 ──")
    for year in range(2019, year_end + 1):
        yn = nav_a3[nav_a3.index.year == year]
        if len(yn) < 2:
            continue
        yr = yn.iloc[-1] / yn.iloc[0] - 1
        yd = ((yn - yn.cummax()) / yn.cummax()).min()
        logger.info(f"  {year}  收益:{yr:+.1%}  最大回撤:{yd:.1%}")

    m3 = calc_metrics(nav_a3)
    logger.info("── 策略A-3 总体 ──")
    for k, v in m3.items():
        logger.info(f"  {k}: {v}")
    nav_a3.to_csv("logs/backtest_a3_nav.csv", header=["nav"])

    if args.compare:
        results = {"策略A-3": nav_a3}
        for fname, name in [("logs/backtest_a2_nav.csv", "策略A-2"),
                             ("logs/backtest_a_nav_I.csv", "策略A（基准）")]:
            f = Path(fname)
            if f.exists():
                results[name] = pd.read_csv(f, index_col=0, parse_dates=True)["nav"]

        def _row(nm, nav):
            m = calc_metrics(nav)
            return {"策略": nm, "总收益": m["总收益率"], "年化": m["年化收益率"],
                    "波动率": m["年化波动率"], "夏普": m["夏普比率"],
                    "最大回撤": m["最大回撤"], "月度胜率": m["月度胜率"]}

        cmp = pd.DataFrame([_row(n, v) for n, v in results.items()]).set_index("策略")
        print("\n" + "=" * 68)
        print("  策略A / A-2 / A-3 完整对比")
        print("=" * 68)
        print(cmp.to_string())

        print("\n── 逐年收益对比 ──")
        nav_a2_f = Path("logs/backtest_a2_nav.csv")
        nav_a_f  = Path("logs/backtest_a_nav_I.csv")
        if nav_a2_f.exists() and nav_a_f.exists():
            nav_a2 = pd.read_csv(nav_a2_f, index_col=0, parse_dates=True)["nav"]
            nav_a  = pd.read_csv(nav_a_f,  index_col=0, parse_dates=True)["nav"]
            for year in range(2019, year_end + 1):
                y2  = nav_a2[nav_a2.index.year == year]
                ya  = nav_a[nav_a.index.year == year]
                y3  = nav_a3[nav_a3.index.year == year]
                if len(y2) < 2 or len(y3) < 2:
                    continue
                r2 = y2.iloc[-1]/y2.iloc[0]-1
                ra = ya.iloc[-1]/ya.iloc[0]-1 if len(ya) >= 2 else float('nan')
                r3 = y3.iloc[-1]/y3.iloc[0]-1
                d  = r3 - r2
                mark = "↑A3优" if d > 0.01 else ("↓A2优" if d < -0.01 else "≈")
                print(f"  {year}: A={ra:+.1%}  A-2={r2:+.1%}  A-3={r3:+.1%}  差={d:+.1%} {mark}")

    ar = float(m3["年化收益率"].strip("%")) / 100
    dd = float(m3["最大回撤"].strip("%")) / 100
    sr = float(m3["夏普比率"])
    ok = ar >= 0.15 and dd >= -0.25 and sr >= 1.0
    logger.info(f"\n结论: {'✅ 达标' if ok else '⚠️ 未达标'}  年化:{ar:.1%}  回撤:{dd:.1%}  夏普:{sr:.2f}")
    logger.info("=" * 68)


if __name__ == "__main__":
    main()
