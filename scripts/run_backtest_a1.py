"""
策略A-1：在策略A（Formula I）基础上的实战优化版本。

核心改进：
  1. 持仓20只（vs A的30只）：聚焦前排，减少弱势股拖累
  2. 得分加权（vs 等权）：最强股权重约2倍最弱股，前排获更多分配
  3. 主线板块1.3倍权重：最强板块持仓额外放大，聚焦市场主线
  4. 成交额乘数 0.50-1.20（vs 0.80-1.00）：更强的流动性分化
  5. 止损收紧 -12%/-15%（vs -15%/-18%）：更快止损，减少磨损

设计思路：
  - 大厂拼的是胜率（算力+高频），我们在胜率上无法竞争
  - 优化赔率：前排强股重仓，主线板块集中，提高单笔盈利
  - 控回撤：更快止损，仓位不分散到弱势股

运行：
    python scripts/run_backtest_a1.py
    python scripts/run_backtest_a1.py --compare   # 与策略A全周期对比
"""
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
from loguru import logger
from data.storage import load_daily, load_meta

from run_backtest_a import (
    load_panels, build_regime_series, _zscore, calc_metrics,
    BACKTEST_START, COMMISSION, MIN_BARS, LIQUIDITY_THRESH,
    MA_PERIOD, REGIME_BEAR_THR, REGIME_BULL_THR, CASH_YIELD,
)

logger.add("logs/backtest_a1.log", rotation="1 day", retention="30 days")

# ── 策略A-1 专属参数 ───────────────────────────────────────────────────────
BACKTEST_END     = os.getenv("BACKTEST_END", "")
N_HOLDINGS       = int(os.getenv("N_HOLDINGS", "20"))       # 聚焦20只前排强股
PERIOD_STOP      = float(os.getenv("PERIOD_STOP", "-0.12")) # 收紧：-15%→-12%
TRAILING_STOP    = float(os.getenv("TRAILING_STOP", "-0.15"))# 收紧：-18%→-15%
SECTOR_BOOST     = float(os.getenv("SECTOR_BOOST", "1.3"))  # 主线板块1.3倍权重
USE_REGIME       = os.getenv("USE_REGIME", "1") == "1"
REBAL_FREQ       = os.getenv("REBAL_FREQ", "biweekly")


def _make_rebal_dates(calendar: list[str], freq: str = "biweekly") -> list[str]:
    dates = pd.DatetimeIndex(sorted(calendar))
    result = []
    for yr in range(dates[0].year, dates[-1].year + 1):
        for mo in range(1, 13):
            md = dates[(dates.year == yr) & (dates.month == mo)]
            if not len(md):
                continue
            if freq == "biweekly" and len(md) >= 2:
                result.append(str(md[len(md) // 2].date()))
            result.append(str(md[-1].date()))
    return sorted(set(result))


# ── 策略A-1 选股打分（在Formula I基础上拓宽乘数范围）────────────────────
def compute_score_a1(
    panel: pd.DataFrame,
    date: pd.Timestamp,
    amount_panel: pd.DataFrame | None = None,
    stock_info: pd.DataFrame | None = None,
) -> pd.Series:
    """
    与Formula I逻辑相同，但成交额乘数范围扩展至0.50~1.20。
    更大的范围让高流动性强势股与低流动性股之间的得分差距更显著。
    """
    hist = panel[panel.index <= date]
    if len(hist) < MIN_BARS:
        return pd.Series(dtype=float)

    # 流动性过滤
    if amount_panel is not None:
        ha  = amount_panel[amount_panel.index <= date]
        ra  = ha.iloc[-20:].mean()
        liq = ra[ra > LIQUIDITY_THRESH].index
        hist = hist[hist.columns.intersection(liq)]
    if hist.empty:
        return pd.Series(dtype=float)

    p, p_126  = hist.iloc[-1], hist.iloc[-126]
    high_250  = hist.iloc[-250:].max()
    mom       = p / p_126 - 1
    price_nh  = (p / high_250).clip(0.5, 1.2)

    if amount_panel is not None:
        ha = amount_panel[amount_panel.index <= date]
        vr = ha.iloc[-20:].mean()
        vb = ha.iloc[-250:].mean().replace(0, float("nan"))
        vol_ratio  = (vr / vb).clip(0.5, 3.0)
        vol_recent = vr
    else:
        vol_recent = pd.Series(1.0, index=p.index)
        vol_ratio  = pd.Series(1.0, index=p.index)

    boost      = ((price_nh - 0.9) * 2).clip(0, 1) * ((vol_ratio - 1) * 0.5).clip(0, 0.5)
    base_score = mom * (1 + boost)

    # 成交额排名：行业内30% + 截面70%（同Formula I）
    cross_rank = vol_recent.rank(pct=True).reindex(p.index)
    if stock_info is not None and "industry_l1" in stock_info.columns:
        ind_map  = stock_info.set_index("code")["industry_l1"]
        sec_rank = pd.Series(0.5, index=p.index)
        for ind in ind_map.unique():
            ic = [c for c in ind_map[ind_map == ind].index
                  if c in p.index and c in vol_recent.index]
            if len(ic) >= 3:
                sec_rank[ic] = vol_recent[ic].rank(pct=True)
        combined = 0.70 * cross_rank + 0.30 * sec_rank.reindex(p.index)
    else:
        combined = cross_rank

    # ★ 关键改进：乘数范围 0.50-1.20（原0.80-1.00），差距扩大到2.4倍
    turnover_mult = (0.50 + 0.70 * combined).fillna(0.65)
    return (base_score * turnover_mult).dropna()


# ── 得分加权（前排重仓）────────────────────────────────────────────────────
def score_weights(selected: list[str], scores: pd.Series) -> dict[str, float]:
    """
    线性加权：得分第1名权重 = 第N名的2倍。
    例如20只：第1名≈6.7%，第20名≈3.3%（等权为5%）。
    """
    n = len(selected)
    if n == 0:
        return {}
    if n == 1:
        return {selected[0]: 1.0}

    # 按得分降序排列
    ordered = sorted(selected, key=lambda c: scores.get(c, 0), reverse=True)
    # 线性权重：rank 1 得 n+1，rank n 得 2，然后归一化
    raw = {c: (n + 1 - rank) + 1 for rank, c in enumerate(ordered, start=1)}
    total = sum(raw.values())
    return {c: v / total for c, v in raw.items()}


# ── 主线板块加权（主线1.3倍）──────────────────────────────────────────────
def apply_sector_boost(
    weights: dict[str, float],
    stock_info: pd.DataFrame | None,
    boost: float = 1.3,
) -> dict[str, float]:
    """
    找到持仓中权重最大的行业（主线板块），该行业股票权重×boost，其余不变，再归一化。
    如果只有1个行业或无行业信息，直接返回原权重。
    """
    if stock_info is None or boost == 1.0 or not weights:
        return weights

    ind_map = stock_info.set_index("code")["industry_l1"].to_dict()

    # 统计各行业总权重
    sector_w: dict[str, float] = {}
    for c, w in weights.items():
        ind = ind_map.get(c, "其他")
        sector_w[ind] = sector_w.get(ind, 0) + w

    if len(sector_w) <= 1:
        return weights

    top_sector = max(sector_w, key=sector_w.get)
    top_cnt    = sum(1 for c in weights if ind_map.get(c) == top_sector)

    # 主线板块至少有2只才加权（防止单只股票扎堆）
    if top_cnt < 2:
        return weights

    new_w = {
        c: w * boost if ind_map.get(c) == top_sector else w
        for c, w in weights.items()
    }
    total = sum(new_w.values())
    return {c: v / total for c, v in new_w.items()}


# ── 换手成本（加权模式下按权重变化计算）─────────────────────────────────
def calc_turnover_cost(
    old_weights: dict[str, float],
    new_weights: dict[str, float],
) -> float:
    """
    换手成本 = sum(|new_w - old_w|) / 2 × 双边手续费
    等权时退化为：换仓数量/持仓数 × COMMISSION×2（与原策略A一致）
    """
    all_codes = set(old_weights) | set(new_weights)
    turnover  = sum(
        abs(new_weights.get(c, 0) - old_weights.get(c, 0))
        for c in all_codes
    ) / 2
    return turnover * COMMISSION * 2


# ── 主回测循环 ────────────────────────────────────────────────────────────
def run_backtest_a1(
    panel: pd.DataFrame,
    rebalance_dates: list,
    amount_panel: pd.DataFrame | None = None,
    regime: pd.Series | None = None,
    stock_info: pd.DataFrame | None = None,
) -> pd.Series:
    all_dates    = panel.index
    port_rets    = pd.Series(0.0, index=all_dates)
    cur_weights: dict[str, float] = {}   # {code: 当前权重}
    cur_score:   pd.Series        = pd.Series(dtype=float)
    cumul_nav    = 1.0
    entry_hwm    = 1.0
    nav_since    = 1.0
    rebal_set    = set(str(d.date()) if hasattr(d, "date") else d for d in rebalance_dates)

    for i, date in enumerate(all_dates):
        date_str = str(date.date())
        in_bull  = True
        if regime is not None and date in regime.index:
            in_bull = bool(regime.loc[date])

        if date_str in rebal_set and i >= MIN_BARS:
            if not in_bull:
                cur_weights = {}
                nav_since   = 1.0
                entry_hwm   = cumul_nav
            else:
                cur_score = compute_score_a1(panel, date, amount_panel, stock_info)
                if len(cur_score) >= N_HOLDINGS:
                    selected = cur_score.nlargest(N_HOLDINGS).index.tolist()

                    # 得分加权
                    new_w = score_weights(selected, cur_score)
                    # 主线板块加权
                    new_w = apply_sector_boost(new_w, stock_info, SECTOR_BOOST)

                    # 换手成本
                    cost = calc_turnover_cost(cur_weights, new_w)
                    port_rets.iloc[i] -= cost

                    if not cur_weights:
                        entry_hwm = cumul_nav
                    cur_weights = new_w
                nav_since = 1.0

        # 止损检查
        if cur_weights and i > 0:
            period_stop   = nav_since <= (1 + PERIOD_STOP)
            trailing_stop = (cumul_nav / entry_hwm - 1) <= TRAILING_STOP
            if period_stop or trailing_stop:
                cur_weights = {}
                nav_since   = 1.0
                entry_hwm   = cumul_nav

        # 加权日收益（非等权）
        if cur_weights and i > 0:
            daily_ret = 0.0
            for code, w in cur_weights.items():
                prev_p = panel.iloc[i - 1].get(code)
                curr_p = panel.iloc[i].get(code)
                if prev_p and curr_p and not pd.isna(prev_p) and not pd.isna(curr_p) and prev_p > 0:
                    daily_ret += w * (curr_p / prev_p - 1)
            port_rets.iloc[i] += daily_ret
        elif not cur_weights and CASH_YIELD > 0:
            port_rets.iloc[i] += CASH_YIELD / 252

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

    logger.info("=" * 65)
    logger.info(f"策略A-1  {BACKTEST_START} → {BACKTEST_END}")
    logger.info(f"持仓:{N_HOLDINGS}只  加权:得分线性  板块加权:{SECTOR_BOOST}x  "
                f"止损:{PERIOD_STOP:.0%}/{TRAILING_STOP:.0%}  "
                f"乘数:0.50-1.20")
    logger.info("=" * 65)

    trade_calendar = [d for d in cal_df["trade_date"].tolist()
                      if BACKTEST_START <= d <= BACKTEST_END]
    rebal_dates    = _make_rebal_dates(trade_calendar, REBAL_FREQ)
    logger.info(f"调仓日期：{len(rebal_dates)} 个")

    csi800 = load_meta("csi800")
    codes  = sorted(csi800["code"].tolist())

    logger.info("加载价格+成交额矩阵...")
    panel, amount_panel = load_panels(codes, BACKTEST_START, BACKTEST_END)
    logger.info(f"价格矩阵：{panel.shape[0]}天 × {panel.shape[1]}只")

    stock_info = load_meta("stock_info_full")
    stock_info = None if stock_info.empty else stock_info

    regime = None
    if USE_REGIME:
        regime = build_regime_series(BACKTEST_START, BACKTEST_END)
        if regime.empty:
            regime = None
        else:
            bull = int(regime.sum())
            logger.info(f"大势过滤：牛市 {bull}/{len(regime)} 天 ({bull/len(regime):.0%})")

    logger.info("运行策略A-1回测...")
    nav_a1 = run_backtest_a1(panel, rebal_dates, amount_panel, regime, stock_info)

    year_end = int(BACKTEST_END[:4])
    logger.info("── 策略A-1 分年度 ──")
    for year in range(2019, year_end + 1):
        yn = nav_a1[nav_a1.index.year == year]
        if len(yn) < 2:
            continue
        yr = yn.iloc[-1] / yn.iloc[0] - 1
        yd = ((yn - yn.cummax()) / yn.cummax()).min()
        logger.info(f"  {year}  收益:{yr:+.1%}  最大回撤:{yd:.1%}")

    m1 = calc_metrics(nav_a1)
    logger.info("── 策略A-1 总体 ──")
    for k, v in m1.items():
        logger.info(f"  {k}: {v}")

    nav_a1.to_csv("logs/backtest_a1_nav.csv", header=["nav"])

    if args.compare:
        nav_a_f = Path("logs/backtest_a_nav_I.csv")
        if not nav_a_f.exists():
            logger.warning("策略A净值文件不存在")
        else:
            nav_a = pd.read_csv(nav_a_f, index_col=0, parse_dates=True)["nav"]

            def _row(name, nav):
                m = calc_metrics(nav)
                return {"策略": name, "总收益": m["总收益率"],
                        "年化": m["年化收益率"], "波动率": m["年化波动率"],
                        "夏普": m["夏普比率"], "最大回撤": m["最大回撤"],
                        "月度胜率": m["月度胜率"]}

            cmp = pd.DataFrame([_row("策略A（基准）", nav_a),
                                 _row("策略A-1（优化）", nav_a1)]).set_index("策略")
            print("\n" + "=" * 62)
            print("  策略A vs 策略A-1 对比")
            print("=" * 62)
            print(cmp.to_string())

            print("\n── 逐年收益对比 ──")
            for year in range(2019, year_end + 1):
                ya  = nav_a[nav_a.index.year == year]
                ya1 = nav_a1[nav_a1.index.year == year]
                if len(ya) < 2 or len(ya1) < 2:
                    continue
                ra  = ya.iloc[-1] / ya.iloc[0] - 1
                ra1 = ya1.iloc[-1] / ya1.iloc[0] - 1
                d   = ra1 - ra
                mark = "↑A1优" if d > 0.01 else ("↓A优" if d < -0.01 else "≈")
                print(f"  {year}: A={ra:+.1%}  A-1={ra1:+.1%}  差={d:+.1%} {mark}")

    ar = float(m1["年化收益率"].strip("%")) / 100
    dd = float(m1["最大回撤"].strip("%")) / 100
    sr = float(m1["夏普比率"])
    ok = ar >= 0.15 and dd >= -0.25 and sr >= 1.0
    logger.info(f"\n结论: {'✅ 达标' if ok else '⚠️ 未达标'}  年化:{ar:.1%}  回撤:{dd:.1%}  夏普:{sr:.2f}")
    logger.info("=" * 65)


if __name__ == "__main__":
    main()
