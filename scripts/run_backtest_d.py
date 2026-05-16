"""
策略D：质量动量组合（Quality Momentum）

因子构成：
  0.55 × Z行业(6M价格动量)          ← 来自策略A Formula I
  0.25 × Z行业(EP = eps_ttm/price)  ← 方案一：当前估值合理性
  0.20 × Z行业(EPS同比增速加速度)   ← 方案三：业绩改善是否持续

核心改进：
  - 动量选出强势趋势，EP防止买入估值泡沫，EPS加速度验证动量有基本面支撑
  - 全部因子在行业内标准化（Z行业），消除行业天然差异
  - 财务数据一次性预加载，不在循环里重复读取文件

运行：
    python scripts/run_backtest_d.py
    python scripts/run_backtest_d.py --compare   # 与策略A全周期对比
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

logger.add("logs/backtest_d.log", rotation="1 day", retention="30 days")

BACKTEST_END  = os.getenv("BACKTEST_END", "")
N_HOLDINGS    = int(os.getenv("N_HOLDINGS", "30"))
USE_REGIME    = os.getenv("USE_REGIME", "1") == "1"
REBAL_FREQ    = os.getenv("REBAL_FREQ", "biweekly")

# ── 因子权重 ───────────────────────────────────────────────────────────────
W_MOM  = 0.55   # 6M价格动量
W_EP   = 0.25   # EP盈利率（行业内）
W_ACCEL = 0.20  # EPS增速加速度（行业内）


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


# ── 财务数据预加载（启动时一次性读取）────────────────────────────────────
def preload_financials(codes: list[str]) -> dict:
    """
    返回 {code: DataFrame(index=report_date, columns=[eps_ttm, bvps])}
    按 report_date 升序排列。
    """
    fin_db = {}
    for code in codes:
        df = load_financial(code)
        if df.empty:
            continue
        df["report_date"] = pd.to_datetime(df["report_date"], errors="coerce")
        df = df.dropna(subset=["report_date", "eps_ttm"]) \
               .sort_values("report_date") \
               .set_index("report_date")
        if not df.empty:
            fin_db[code] = df
    logger.info(f"财务数据预加载：{len(fin_db)}/{len(codes)} 只有效")
    return fin_db


# ── EP 因子：当前盈利率（反PE）──────────────────────────────────────────
def get_ep_snapshot(fin_db: dict, price_snapshot: pd.Series, as_of: str) -> pd.Series:
    """
    EP = eps_ttm / close_price
    - 过滤 eps_ttm <= 0（亏损股）
    - as_of: 截止日期，只用该日前最新一期季报
    """
    dt = pd.Timestamp(as_of)
    result = {}
    for code, df in fin_db.items():
        avail = df[df.index <= dt]["eps_ttm"].dropna()
        if avail.empty:
            continue
        eps = float(avail.iloc[-1])
        if eps <= 0:
            continue   # 亏损股排除
        price = price_snapshot.get(code)
        if price and not np.isnan(price) and price > 0:
            result[code] = eps / price
    return pd.Series(result)


# ── EPS 加速度：同比增速的提升量 ─────────────────────────────────────────
def get_eps_accel_snapshot(fin_db: dict, as_of: str) -> pd.Series:
    """
    EPS同比增速加速度 = yoy_growth_t1 - yoy_growth_t0
    yoy_growth_t1 = (eps_latest / eps_1year_ago) - 1
    yoy_growth_t0 = (eps_1q_ago / eps_1year_before_1q_ago) - 1

    正值 = 增速在加快，负值 = 增速在减慢
    """
    dt = pd.Timestamp(as_of)
    result = {}

    for code, df in fin_db.items():
        avail = df[df.index <= dt]["eps_ttm"].dropna()
        if len(avail) < 5:   # 至少需要5个季度才能算两期同比
            continue

        # t1：最新一期
        t1_eps  = float(avail.iloc[-1])
        t1_date = avail.index[-1]

        # t0：上一期（往前一个季度）
        t0_series = avail.iloc[:-1]
        if t0_series.empty:
            continue
        t0_eps  = float(t0_series.iloc[-1])
        t0_date = t0_series.index[-1]

        # t1 的同比基准（约一年前）：找 t1_date 往前9~15个月内最近一期
        t1_1y_cutoff = t1_date - pd.DateOffset(months=9)
        t1_1y_upper  = t1_date - pd.DateOffset(months=3)
        t1_1y = avail[(avail.index >= t1_1y_cutoff) & (avail.index < t1_1y_upper)]
        if t1_1y.empty:
            continue
        t1_1y_eps = float(t1_1y.iloc[-1])

        # t0 的同比基准
        t0_1y_cutoff = t0_date - pd.DateOffset(months=9)
        t0_1y_upper  = t0_date - pd.DateOffset(months=3)
        t0_1y = avail[(avail.index >= t0_1y_cutoff) & (avail.index < t0_1y_upper)]
        if t0_1y.empty:
            continue
        t0_1y_eps = float(t0_1y.iloc[-1])

        # 避免除以0和极端值
        if t1_1y_eps == 0 or t0_1y_eps == 0:
            continue
        if t1_eps <= 0 or t0_eps <= 0:
            continue   # 亏损股EPS加速度无意义

        yoy_t1 = t1_eps / t1_1y_eps - 1
        yoy_t0 = t0_eps / t0_1y_eps - 1
        accel  = yoy_t1 - yoy_t0

        # 截断极端值（±3），避免异常季报主导
        result[code] = float(np.clip(accel, -3.0, 3.0))

    return pd.Series(result)


# ── 行业内 z-score ─────────────────────────────────────────────────────────
def industry_zscore(factor: pd.Series, ind_map: pd.Series) -> pd.Series:
    """在申万一级行业内做 z-score 标准化，±3σ 截断。"""
    result = pd.Series(np.nan, index=factor.index)
    for ind in ind_map.unique():
        codes = ind_map[ind_map == ind].index.tolist()
        sub   = factor.reindex(codes).dropna()
        if len(sub) < 3:
            result[sub.index] = 0.0
            continue
        mu, sigma = sub.mean(), sub.std()
        if sigma < 1e-8:
            result[sub.index] = 0.0
        else:
            z = ((sub - mu) / sigma).clip(-3, 3)
            result[sub.index] = z
    return result.fillna(0)


def winsorize(s: pd.Series, pct: float = 0.01) -> pd.Series:
    lo, hi = s.quantile(pct), s.quantile(1 - pct)
    return s.clip(lo, hi)


# ── 策略D 综合打分 ────────────────────────────────────────────────────────
def compute_score_d(
    panel: pd.DataFrame,
    date: pd.Timestamp,
    amount_panel: pd.DataFrame | None,
    stock_info: pd.DataFrame | None,
    fin_db: dict,
) -> pd.Series:
    """
    综合因子得分：
      0.55 × Z行业(6M动量)
    + 0.25 × Z行业(EP)
    + 0.20 × Z行业(EPS加速度)
    """
    date_str = str(date.date())
    hist     = panel[panel.index <= date]
    if len(hist) < MIN_BARS:
        return pd.Series(dtype=float)

    # ── 流动性过滤 ──────────────────────────────────────
    if amount_panel is not None:
        ha  = amount_panel[amount_panel.index <= date]
        ra  = ha.iloc[-20:].mean()
        liq = ra[ra > LIQUIDITY_THRESH].index
        hist = hist[hist.columns.intersection(liq)]
    if hist.empty:
        return pd.Series(dtype=float)

    # ── 6M 动量（Formula I 核心：动量×量价加成×成交额权重）──────────────
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
    raw_mom    = mom * (1 + boost)

    # 成交额权重（保留A的权重设计）
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
    mom_w = (raw_mom * tm).dropna()

    # ── 获取候选股 ────────────────────────────────────
    candidates = mom_w.index.tolist()

    # ── EP 因子（行业内标准化）────────────────────────
    price_snap = p.reindex(candidates)
    ep_raw     = get_ep_snapshot(fin_db, price_snap, date_str).reindex(candidates)

    # ── EPS 加速度（行业内标准化）────────────────────
    accel_raw  = get_eps_accel_snapshot(fin_db, date_str).reindex(candidates)

    # ── 行业内标准化 ──────────────────────────────────
    if stock_info is not None and "industry_l1" in stock_info.columns:
        ind_map = stock_info.set_index("code")["industry_l1"].reindex(candidates)

        mom_z   = industry_zscore(winsorize(mom_w), ind_map)
        ep_z    = industry_zscore(winsorize(ep_raw.dropna()), ind_map)
        accel_z = industry_zscore(winsorize(accel_raw.dropna()), ind_map)
    else:
        mom_z   = _zscore(mom_w)
        ep_z    = _zscore(ep_raw.dropna())
        accel_z = _zscore(accel_raw.dropna())

    # ── 综合得分（仅对三个因子都有数据的股票打分）───
    # 动量：所有流动性过滤后的股票都有
    # EP 和 EPS加速度：只有盈利且历史数据够的股票有
    score = (
        W_MOM   * mom_z
        + W_EP    * ep_z.reindex(mom_z.index).fillna(0)
        + W_ACCEL * accel_z.reindex(mom_z.index).fillna(0)
    )
    return score.dropna()


# ── 主回测循环（与策略A结构完全相同）────────────────────────────────────
def run_backtest_d(
    panel: pd.DataFrame,
    rebalance_dates: list,
    amount_panel: pd.DataFrame | None,
    regime: pd.Series | None,
    stock_info: pd.DataFrame | None,
    fin_db: dict,
) -> pd.Series:
    all_dates = panel.index
    port_rets = pd.Series(0.0, index=all_dates)
    cur_hold  = []
    cumul_nav = 1.0
    entry_hwm = 1.0
    nav_since = 1.0
    rebal_set = set(str(d.date()) if hasattr(d, "date") else d for d in rebalance_dates)

    for i, date in enumerate(all_dates):
        date_str = str(date.date())
        in_bull  = True
        if regime is not None and date in regime.index:
            in_bull = bool(regime.loc[date])

        if date_str in rebal_set and i >= MIN_BARS:
            if not in_bull:
                cur_hold  = []
                nav_since = 1.0
                entry_hwm = cumul_nav
            else:
                score = compute_score_d(panel, date, amount_panel, stock_info, fin_db)
                if len(score) >= N_HOLDINGS:
                    new_hold = score.nlargest(N_HOLDINGS).index.tolist()
                    if cur_hold:
                        old_s = set(cur_hold)
                        new_s = set(new_hold)
                        turnover = (len(old_s - new_s) + len(new_s - old_s)) / (2 * N_HOLDINGS)
                        port_rets.iloc[i] -= turnover * COMMISSION * 2
                    if not cur_hold:
                        entry_hwm = cumul_nav
                    cur_hold = new_hold
                nav_since = 1.0

        # 止损
        if cur_hold and i > 0:
            period_stop   = nav_since <= (1 + PERIOD_STOP)
            trailing_stop = (cumul_nav / entry_hwm - 1) <= TRAILING_STOP
            if period_stop or trailing_stop:
                cur_hold  = []
                nav_since = 1.0
                entry_hwm = cumul_nav

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


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--compare", action="store_true", help="与策略A对比")
    args = parser.parse_args()

    global BACKTEST_END
    cal_df = load_meta("trade_calendar")
    if not BACKTEST_END:
        BACKTEST_END = sorted(cal_df["trade_date"].tolist())[-1]

    logger.info("=" * 65)
    logger.info(f"策略D（质量动量）  {BACKTEST_START} → {BACKTEST_END}")
    logger.info(f"动量{W_MOM:.0%} + EP{W_EP:.0%} + EPS加速度{W_ACCEL:.0%}  持仓:{N_HOLDINGS}只  调仓:{REBAL_FREQ}")
    logger.info("=" * 65)

    trade_calendar = [d for d in cal_df["trade_date"].tolist()
                      if BACKTEST_START <= d <= BACKTEST_END]
    rebal_dates    = _make_rebal_dates(trade_calendar, REBAL_FREQ)
    logger.info(f"调仓日期数：{len(rebal_dates)} 个")

    csi800 = load_meta("csi800")
    codes  = sorted(csi800["code"].tolist())

    logger.info("加载价格+成交额矩阵...")
    panel, amount_panel = load_panels(codes, BACKTEST_START, BACKTEST_END)
    logger.info(f"价格矩阵：{panel.shape[0]} 天 × {panel.shape[1]} 只")

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

    logger.info("预加载财务数据（一次性）...")
    fin_db = preload_financials(codes)

    logger.info("运行策略D回测...")
    nav_d = run_backtest_d(panel, rebal_dates, amount_panel, regime, stock_info, fin_db)

    # ── 分年度结果 ──
    year_end = int(BACKTEST_END[:4])
    logger.info("── 策略D 分年度 ──")
    for year in range(2019, year_end + 1):
        yn = nav_d[nav_d.index.year == year]
        if len(yn) < 2:
            continue
        yr = yn.iloc[-1] / yn.iloc[0] - 1
        yd = ((yn - yn.cummax()) / yn.cummax()).min()
        logger.info(f"  {year}  收益:{yr:+.1%}  最大回撤:{yd:.1%}")

    md = calc_metrics(nav_d)
    logger.info("── 策略D 总体指标 ──")
    for k, v in md.items():
        logger.info(f"  {k}: {v}")

    nav_d.to_csv("logs/backtest_d_nav.csv", header=["nav"])
    logger.info("净值已保存 → logs/backtest_d_nav.csv")

    # ── 与策略A对比 ──
    if args.compare:
        nav_a_f = Path("logs/backtest_a_nav_I.csv")
        if not nav_a_f.exists():
            logger.warning("策略A净值文件不存在，请先运行 run_backtest_a.py")
        else:
            nav_a = pd.read_csv(nav_a_f, index_col=0, parse_dates=True)["nav"]
            nav_a.name = "nav"

            def _row(name, nav):
                m = calc_metrics(nav)
                return {
                    "策略": name,
                    "总收益": m["总收益率"],
                    "年化": m["年化收益率"],
                    "波动率": m["年化波动率"],
                    "夏普": m["夏普比率"],
                    "最大回撤": m["最大回撤"],
                    "月度胜率": m["月度胜率"],
                }

            cmp = pd.DataFrame([_row("策略A", nav_a), _row("策略D", nav_d)]).set_index("策略")
            print("\n" + "=" * 62)
            print("  策略A（纯动量）vs 策略D（质量动量）全周期对比")
            print("=" * 62)
            print(cmp.to_string())

            print("\n── 逐年收益对比 ──")
            for year in range(2019, year_end + 1):
                ya = nav_a[nav_a.index.year == year]
                yd_y = nav_d[nav_d.index.year == year]
                if len(ya) < 2 or len(yd_y) < 2:
                    continue
                ra = ya.iloc[-1] / ya.iloc[0] - 1
                rd = yd_y.iloc[-1] / yd_y.iloc[0] - 1
                diff = rd - ra
                mark = "↑D优" if diff > 0.01 else ("↓A优" if diff < -0.01 else "≈")
                print(f"  {year}: A={ra:+.1%}  D={rd:+.1%}  差={diff:+.1%} {mark}")

    ar = float(md["年化收益率"].strip("%")) / 100
    dd = float(md["最大回撤"].strip("%")) / 100
    sr = float(md["夏普比率"])
    ok = ar >= 0.15 and dd >= -0.25 and sr >= 1.0
    logger.info(f"\n结论: {'✅ 达标' if ok else '⚠️ 未达标'}  年化:{ar:.1%}  回撤:{dd:.1%}  夏普:{sr:.2f}")
    logger.info("=" * 65)


if __name__ == "__main__":
    main()
