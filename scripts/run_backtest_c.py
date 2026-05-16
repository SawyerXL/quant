"""
策略C v2：在策略A（Formula I）基础上的优化版本。

修正后的优化点：
  1. 最小换仓阈值（MIN_SWAP_EDGE=10%）：新股票得分需比被替换股高出10%才换
     → 减少无效交易，降低摩擦成本
  2. 最长持仓限制（MAX_HOLD_PERIODS=6）：任何股票最多持有6个调仓周期（约3个月）
     → 防止"冻结"效应，避免市场风格切换时抱着旧股不放
  3. 行业集中度上限（MAX_IND_STOCKS=7）：单一行业持仓上限约持仓数的25%
     → 解决高波动率问题，强制分散行业风险
  4. ROE过滤改为预加载（高效）：开始时一次性读取所有财务数据
     → 不再每次调仓都读800个文件

运行：
    python scripts/run_backtest_c.py          # 跑策略C
    python scripts/run_backtest_c.py --compare  # 与策略A对比
"""
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
from loguru import logger
from data.storage import load_daily, load_meta, load_financial

from run_backtest_a import (
    load_panels, build_regime_series, _zscore,
    calc_metrics,
    BACKTEST_START, COMMISSION, MIN_BARS, LIQUIDITY_THRESH,
    MA_PERIOD, REGIME_BEAR_THR, REGIME_BULL_THR,
    PERIOD_STOP, TRAILING_STOP, CASH_YIELD,
)

logger.add("logs/backtest_c.log", rotation="1 day", retention="30 days")

# ── 策略C 专属参数 ─────────────────────────────────────────────────────────
BACKTEST_END     = os.getenv("BACKTEST_END", "")
N_HOLDINGS       = int(os.getenv("N_HOLDINGS", "30"))
MIN_SWAP_EDGE    = float(os.getenv("MIN_SWAP_EDGE", "0.10"))  # 换仓阈值 10%
MAX_HOLD_PERIODS = int(os.getenv("MAX_HOLD_PERIODS", "6"))    # 最多持有6期（约3个月）
MAX_IND_STOCKS   = int(os.getenv("MAX_IND_STOCKS", "7"))      # 单行业最多7只（约25%）
USE_ROE          = os.getenv("USE_ROE", "1") == "1"
ROE_MIN          = float(os.getenv("ROE_MIN", "0.0"))
USE_REGIME       = os.getenv("USE_REGIME", "1") == "1"
REBAL_FREQ       = os.getenv("REBAL_FREQ", "biweekly")


def _make_rebal_dates(calendar: list[str], freq: str = "biweekly") -> list[str]:
    dates = pd.DatetimeIndex(sorted(calendar))
    result = []
    for yr in range(dates[0].year, dates[-1].year + 1):
        for mo in range(1, 13):
            md = dates[(dates.year == yr) & (dates.month == mo)]
            if len(md) == 0:
                continue
            if freq == "biweekly" and len(md) >= 2:
                result.append(str(md[len(md) // 2].date()))
            result.append(str(md[-1].date()))
    return sorted(set(result))


# ── ROE 预加载（只读一次文件）─────────────────────────────────────────────
def preload_roe(codes: list[str]) -> dict:
    """
    返回 {code: pd.Series(index=report_date, values=roe_annualized)}
    在回测开始时一次性读取，后续 O(1) 查询。
    """
    roe_db = {}
    for code in codes:
        fin = load_financial(code)
        if fin.empty or "roe_annualized" not in fin.columns:
            continue
        fin["report_date"] = pd.to_datetime(fin["report_date"], errors="coerce")
        series = fin.dropna(subset=["report_date", "roe_annualized"]) \
                    .set_index("report_date")["roe_annualized"] \
                    .sort_index()
        if not series.empty:
            roe_db[code] = series
    logger.info(f"ROE 预加载完成：{len(roe_db)}/{len(codes)} 只有数据")
    return roe_db


def get_roe_snapshot(roe_db: dict, date: str) -> pd.Series:
    """获取截止 date 最新一期 ROE 的截面快照。"""
    dt = pd.Timestamp(date)
    result = {}
    for code, series in roe_db.items():
        valid = series[series.index <= dt]
        if not valid.empty:
            result[code] = float(valid.iloc[-1])
    return pd.Series(result)


# ── 得分计算（同A，加ROE过滤）──────────────────────────────────────────────
def compute_score_c(
    panel: pd.DataFrame,
    date: pd.Timestamp,
    amount_panel: pd.DataFrame | None = None,
    stock_info: pd.DataFrame | None = None,
    roe_snapshot: pd.Series | None = None,
) -> pd.Series:
    hist = panel[panel.index <= date]
    if len(hist) < MIN_BARS:
        return pd.Series(dtype=float)

    if amount_panel is not None:
        ha  = amount_panel[amount_panel.index <= date]
        ra  = ha.iloc[-20:].mean()
        liq = ra[ra > LIQUIDITY_THRESH].index
        hist = hist[hist.columns.intersection(liq)]
    if hist.empty:
        return pd.Series(dtype=float)

    p, p_126 = hist.iloc[-1], hist.iloc[-126]
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

    tm    = (0.80 + 0.20 * combined).fillna(0.90)
    score = (base_score * tm).dropna()

    # ROE 质量过滤：剔除最新 ROE < ROE_MIN 的亏损/低质量股
    if USE_ROE and roe_snapshot is not None and not roe_snapshot.empty:
        valid = roe_snapshot.reindex(score.index).fillna(ROE_MIN) >= ROE_MIN
        score = score[valid]

    return score


# ── 行业集中度约束 ─────────────────────────────────────────────────────────
def apply_industry_cap(
    holdings: list[str],
    score: pd.Series,
    stock_info: pd.DataFrame | None,
    max_per_industry: int,
) -> list[str]:
    """
    强制行业多样化：单一行业最多 max_per_industry 只。
    超出部分替换为同行业外、得分最高的候选股。
    """
    if stock_info is None or "industry_l1" not in stock_info.columns or max_per_industry <= 0:
        return holdings

    ind_map = stock_info.set_index("code")["industry_l1"].to_dict()
    result  = []
    ind_count: dict[str, int] = {}

    # 按得分降序，贪心填入
    for code in sorted(holdings, key=lambda c: score.get(c, -np.inf), reverse=True):
        ind = ind_map.get(code, "未知")
        if ind_count.get(ind, 0) < max_per_industry:
            result.append(code)
            ind_count[ind] = ind_count.get(ind, 0) + 1

    # 若因行业上限导致持仓不足 N，从候选池补充（跳过已超限行业）
    if len(result) < len(holdings):
        used = set(result)
        for code in score.sort_values(ascending=False).index:
            if len(result) >= len(holdings):
                break
            if code in used:
                continue
            ind = ind_map.get(code, "未知")
            if ind_count.get(ind, 0) < max_per_industry:
                result.append(code)
                ind_count[ind] = ind_count.get(ind, 0) + 1
                used.add(code)

    return result


# ── 最小换仓阈值 + 最长持仓限制 ───────────────────────────────────────────
def apply_swap_rules(
    score: pd.Series,
    current_holdings: list[str],
    hold_periods: dict[str, int],
    n: int,
    threshold: float,
    max_periods: int,
) -> list[str]:
    """
    懒惰再平衡：
    - 新股票得分超出被替换股 threshold 才换（减少无效交易）
    - 持仓超过 max_periods 个调仓周期的强制参与重新竞争（防止冻结）
    """
    if not current_holdings or threshold <= 0:
        return score.nlargest(n).index.tolist()

    cur_set = set(current_holdings)
    top_n   = score.nlargest(n).index.tolist()
    top_set = set(top_n)

    # 超期股强制归入"可被替换"候选，即使它还在 top_n
    expired = {c for c in current_holdings if hold_periods.get(c, 0) >= max_periods}
    # 要移除的候选：不在top_n 或 已超期
    remove_cands = sorted(
        [c for c in current_holdings if c not in top_set or c in expired],
        key=lambda c: score.get(c, -np.inf)   # 得分低的优先替换
    )
    # 要加入的候选：top_n 中当前没有的，或可替换超期股的更好选择
    add_cands = sorted(
        [c for c in top_n if c not in cur_set or c in expired],
        key=lambda c: score.get(c, -np.inf), reverse=True
    )

    result = [c for c in current_holdings if c not in expired or c in top_set]
    # 清除超期且不在top_n的
    result = [c for c in result if c in top_set or (c in cur_set and c not in expired)]

    for rm, add in zip(remove_cands, add_cands):
        if rm not in result:
            continue
        rm_score  = score.get(rm, -np.inf)
        add_score = score.get(add, -np.inf)

        # 强制替换：被替换股已超期
        if rm in expired:
            result.remove(rm)
            if add not in result:
                result.append(add)
            continue

        # 普通替换：新股得分超出阈值
        if rm_score > 0 and add not in result:
            if add_score / rm_score - 1 >= threshold:
                result.remove(rm)
                result.append(add)
        elif rm_score <= 0 and add_score > 0 and add not in result:
            result.remove(rm)
            result.append(add)

    # 补齐至 n 只
    for c in top_n:
        if len(result) >= n:
            break
        if c not in result:
            result.append(c)

    return result[:n]


# ── 主回测 ─────────────────────────────────────────────────────────────────
def run_backtest_c(
    panel: pd.DataFrame,
    rebalance_dates: list,
    amount_panel: pd.DataFrame | None = None,
    regime: pd.Series | None = None,
    stock_info: pd.DataFrame | None = None,
    roe_db: dict | None = None,
) -> pd.Series:
    all_dates   = panel.index
    port_rets   = pd.Series(0.0, index=all_dates)
    cur_hold:   list[str] = []
    hold_periods: dict[str, int] = {}   # {code: 持有了几个调仓周期}
    cumul_nav   = 1.0
    entry_hwm   = 1.0
    nav_since   = 1.0
    rebal_set   = set(str(d.date()) if hasattr(d, "date") else d for d in rebalance_dates)

    for i, date in enumerate(all_dates):
        date_str = str(date.date())
        in_bull  = True
        if regime is not None and date in regime.index:
            in_bull = bool(regime.loc[date])

        if date_str in rebal_set and i >= MIN_BARS:
            if not in_bull:
                cur_hold      = []
                hold_periods  = {}
                nav_since     = 1.0
                entry_hwm     = cumul_nav
            else:
                # ROE 截面快照（预加载，查询极快）
                roe_snap = get_roe_snapshot(roe_db, date_str) if roe_db else None

                score = compute_score_c(panel, date, amount_panel, stock_info, roe_snap)
                if len(score) >= N_HOLDINGS:
                    # 1. 换仓阈值 + 最长持仓限制
                    new_hold = apply_swap_rules(
                        score, cur_hold, hold_periods,
                        N_HOLDINGS, MIN_SWAP_EDGE, MAX_HOLD_PERIODS
                    )
                    # 2. 行业集中度约束
                    new_hold = apply_industry_cap(new_hold, score, stock_info, MAX_IND_STOCKS)

                    # 换仓摩擦成本
                    if cur_hold:
                        old_s, new_s = set(cur_hold), set(new_hold)
                        turnover = (len(old_s - new_s) + len(new_s - old_s)) / (2 * N_HOLDINGS)
                        port_rets.iloc[i] -= turnover * COMMISSION * 2

                    # 更新持仓周期计数
                    new_set = set(new_hold)
                    hold_periods = {
                        c: hold_periods.get(c, 0) + 1 if c in set(cur_hold) else 0
                        for c in new_hold
                    }
                    # 新买入的从0开始
                    for c in new_hold:
                        if c not in set(cur_hold):
                            hold_periods[c] = 0

                    if not cur_hold:
                        entry_hwm = cumul_nav
                    cur_hold = new_hold
                nav_since = 1.0

        # 止损
        if cur_hold and i > 0:
            if nav_since <= (1 + PERIOD_STOP) or (cumul_nav / entry_hwm - 1) <= TRAILING_STOP:
                cur_hold     = []
                hold_periods = {}
                nav_since    = 1.0
                entry_hwm    = cumul_nav

        # 日收益
        if cur_hold and i > 0:
            prev  = panel.iloc[i - 1][cur_hold].dropna()
            curr  = panel.iloc[i][cur_hold].dropna()
            comm  = prev.index.intersection(curr.index)
            if len(comm) > 0:
                port_rets.iloc[i] += (curr[comm] / prev[comm] - 1).mean()
        elif not cur_hold and CASH_YIELD > 0:
            port_rets.iloc[i] += CASH_YIELD / 252

        nav_since = nav_since * (1 + port_rets.iloc[i])
        cumul_nav = cumul_nav * (1 + port_rets.iloc[i])

    return (1 + port_rets).cumprod()


def compare(results: dict) -> pd.DataFrame:
    rows = []
    for name, nav in results.items():
        m = calc_metrics(nav)
        rows.append({
            "策略":    name,
            "总收益":  m["总收益率"],
            "年化":    m["年化收益率"],
            "波动率":  m["年化波动率"],
            "夏普":    m["夏普比率"],
            "最大回撤": m["最大回撤"],
            "月度胜率": m["月度胜率"],
        })
    return pd.DataFrame(rows).set_index("策略")


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--compare", action="store_true")
    args = parser.parse_args()

    global BACKTEST_END
    cal_df = load_meta("trade_calendar")
    if not BACKTEST_END:
        BACKTEST_END = sorted(cal_df["trade_date"].tolist())[-1]

    logger.info("=" * 60)
    logger.info(f"策略C v2  {BACKTEST_START}→{BACKTEST_END}")
    logger.info(f"持仓:{N_HOLDINGS}  换仓阈值:{MIN_SWAP_EDGE:.0%}  "
                f"最长持仓:{MAX_HOLD_PERIODS}期  行业上限:{MAX_IND_STOCKS}只  "
                f"ROE过滤:{USE_ROE}")
    logger.info("=" * 60)

    trade_calendar = [d for d in cal_df["trade_date"].tolist()
                      if BACKTEST_START <= d <= BACKTEST_END]
    rebal_dates    = _make_rebal_dates(trade_calendar, REBAL_FREQ)

    csi800 = load_meta("csi800")
    codes  = sorted(csi800["code"].tolist())

    logger.info("加载价格+成交额矩阵...")
    panel, amount_panel = load_panels(codes, BACKTEST_START, BACKTEST_END)

    stock_info = load_meta("stock_info_full")
    stock_info = None if stock_info.empty else stock_info

    regime = None
    if USE_REGIME:
        regime = build_regime_series(BACKTEST_START, BACKTEST_END)
        if regime.empty:
            regime = None

    # 预加载 ROE（一次性读取）
    roe_db = None
    if USE_ROE:
        logger.info("预加载 ROE 数据...")
        roe_db = preload_roe(codes)

    # ── 运行策略C ──
    logger.info("运行策略C v2...")
    nav_c = run_backtest_c(panel, rebal_dates, amount_panel, regime, stock_info, roe_db)

    logger.info("── 策略C 分年度 ──")
    year_end = int(BACKTEST_END[:4])
    for year in range(2019, year_end + 1):
        yn = nav_c[nav_c.index.year == year]
        if len(yn) < 2:
            continue
        yr = yn.iloc[-1] / yn.iloc[0] - 1
        yd = ((yn - yn.cummax()) / yn.cummax()).min()
        logger.info(f"  {year}  收益:{yr:.1%}  最大回撤:{yd:.1%}")

    mc = calc_metrics(nav_c)
    logger.info("── 策略C 总体 ──")
    for k, v in mc.items():
        logger.info(f"  {k}: {v}")
    nav_c.to_csv("logs/backtest_c_nav.csv", header=["nav"])

    if args.compare:
        nav_a_f = Path("logs/backtest_a_nav_I.csv")
        if nav_a_f.exists():
            nav_a = pd.read_csv(nav_a_f, index_col=0, parse_dates=True)["nav"]
            cmp = compare({"策略C v2": nav_c, "策略A": nav_a})
            print("\n" + "=" * 58)
            print("  策略A vs 策略C v2 对比")
            print("=" * 58)
            print(cmp.to_string())

            print("\n── 逐年收益对比 ──")
            for year in range(2019, year_end + 1):
                ya = nav_a[nav_a.index.year == year]
                yc = nav_c[nav_c.index.year == year]
                if len(ya) < 2 or len(yc) < 2:
                    continue
                ra = ya.iloc[-1] / ya.iloc[0] - 1
                rc = yc.iloc[-1] / yc.iloc[0] - 1
                d  = rc - ra
                mark = "↑C优" if d > 0.01 else ("↓A优" if d < -0.01 else "≈")
                print(f"  {year}: A={ra:+.1%}  C={rc:+.1%}  差={d:+.1%} {mark}")

    ar = float(mc["年化收益率"].strip("%")) / 100
    md = float(mc["最大回撤"].strip("%")) / 100
    sr = float(mc["夏普比率"])
    ok = ar >= 0.15 and md >= -0.25 and sr >= 1.0
    logger.info(f"\n结论: {'✅ 达标' if ok else '⚠️ 未达标'}  年化:{ar:.1%} 回撤:{md:.1%} 夏普:{sr:.2f}")


if __name__ == "__main__":
    main()
