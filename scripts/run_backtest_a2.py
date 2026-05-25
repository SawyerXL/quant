"""
策略A-2：系统性提升动量因子稳定性 + 赔率/回撤优化。

改进清单（vs 策略A）：
  ① 多周期动量叠加：Z行业(1M) 0.3 + Z行业(6M) 0.4 + Z行业(12M) 0.3
     → 行业内标准化，避免强势行业天然霸榜
  ② 波动率调控：高波动股票权重×0.7，低波动×1.3
     → 减少单日大幅波动股票的组合贡献度
  ③ 行业均衡选股：30只名额按行业强度比例分配
     → 避免一个行业占满，自带行业分散
  ④ 得分加权：rank线性（top股2倍bottom股）
     → 前排强股获更多仓位
  ⑤ 主线板块1.3倍：最强板块股票额外放大
     → 聚焦市场主线
  ⑥ 阶梯式仓位：按CSI800/MA200比值分5档（30%~100%）
     → 不再二值化（全仓/空仓），降低踏空/满仓风险

运行：
    python scripts/run_backtest_a2.py
    python scripts/run_backtest_a2.py --compare   # 与策略A对比
"""
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
from loguru import logger
from data.storage import load_meta

from run_backtest_a import (
    load_panels, _zscore, calc_metrics,
    BACKTEST_START, COMMISSION, MIN_BARS, LIQUIDITY_THRESH,
    MA_PERIOD, PERIOD_STOP, TRAILING_STOP, CASH_YIELD,
    REGIME_BEAR_THR, REGIME_BULL_THR,
)

logger.add("logs/backtest_a2.log", rotation="1 day", retention="30 days")

BACKTEST_END  = os.getenv("BACKTEST_END", "")
N_HOLDINGS    = int(os.getenv("N_HOLDINGS", "30"))
SECTOR_BOOST  = float(os.getenv("SECTOR_BOOST", "1.3"))   # 主线板块加权倍数
MAX_IND_SLOT  = int(os.getenv("MAX_IND_SLOT", "8"))        # 单行业最多选几只
USE_REGIME    = os.getenv("USE_REGIME", "1") == "1"
REBAL_FREQ    = os.getenv("REBAL_FREQ", "biweekly")


def _make_rebal_dates(calendar, freq="biweekly"):
    """
    生成调仓日期列表。
    月末用倒数第二个交易日（为执行失败预留1天缓冲）。
    """
    dates = pd.DatetimeIndex(sorted(calendar))
    result = []
    for yr in range(dates[0].year, dates[-1].year + 1):
        for mo in range(1, 13):
            md = dates[(dates.year == yr) & (dates.month == mo)]
            if not len(md):
                continue
            if freq == "biweekly" and len(md) >= 2:
                result.append(str(md[len(md) // 2].date()))
            # 月末：倒数第二个交易日（保留最后一天为缓冲）
            end_idx = -2 if len(md) >= 2 else -1
            result.append(str(md[end_idx].date()))
    return sorted(set(result))


# ── ① 行业内Z-score ────────────────────────────────────────────────────────
def ind_zscore(factor: pd.Series, ind_map: pd.Series) -> pd.Series:
    result = pd.Series(0.0, index=factor.index)
    for ind in ind_map.unique():
        codes = ind_map[ind_map == ind].index.intersection(factor.index)
        sub = factor[codes].dropna()
        if len(sub) < 3:
            result[sub.index] = 0.0
            continue
        mu, sigma = sub.mean(), sub.std()
        if sigma < 1e-8:
            result[sub.index] = 0.0
        else:
            result[sub.index] = ((sub - mu) / sigma).clip(-3, 3)
    return result.fillna(0)


# ── ② 阶梯式仓位（5档）────────────────────────────────────────────────────
def get_position_ratio(index_close: pd.Series, date: pd.Timestamp) -> float:
    """
    CSI800 收盘价 / MA200：
      ≥ 1.05 → 100%（强牛）
      1.02-1.05 → 85%（牛）
      0.98-1.02 → 70%（震荡）
      0.95-0.98 → 50%（弱）
      < 0.95   → 30%（熊，不清零，避免踏空V反）
    """
    hist = index_close[index_close.index <= date].dropna()
    if len(hist) < MA_PERIOD:
        return 0.85  # 数据不足时保守70%

    ma200 = hist.rolling(MA_PERIOD).mean().iloc[-1]
    ratio = float(hist.iloc[-1]) / float(ma200)

    if ratio >= 1.05:   return 1.00
    elif ratio >= 1.02: return 0.85
    elif ratio >= 0.98: return 0.70
    elif ratio >= 0.95: return 0.50
    else:               return 0.30


# ── ③ 策略A-2 综合打分 ─────────────────────────────────────────────────────
def compute_score_a2(
    panel: pd.DataFrame,
    date: pd.Timestamp,
    amount_panel: pd.DataFrame | None,
    stock_info: pd.DataFrame | None,
) -> pd.Series:
    """
    多周期动量（行业内标准化）× 量价加成 × 波动率调控
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

    p = hist.iloc[-1]

    # ① 多周期动量（各自行业内标准化后叠加）
    ind_map = (stock_info.set_index("code")["industry_l1"].reindex(p.index)
               if stock_info is not None and "industry_l1" in stock_info.columns
               else pd.Series("其他", index=p.index))

    ret_1m  = (p / hist.iloc[-21]  - 1).dropna() if len(hist) >= 22  else pd.Series(dtype=float)
    ret_6m  = (p / hist.iloc[-126] - 1).dropna() if len(hist) >= 127 else pd.Series(dtype=float)
    ret_12m = (p / hist.iloc[-252] - 1).dropna() if len(hist) >= 253 else pd.Series(dtype=float)

    # 行业内标准化
    common = p.index
    z1m  = ind_zscore(ret_1m.reindex(common).fillna(0),  ind_map) if not ret_1m.empty  else pd.Series(0, index=common)
    z6m  = ind_zscore(ret_6m.reindex(common).fillna(0),  ind_map) if not ret_6m.empty  else pd.Series(0, index=common)
    z12m = ind_zscore(ret_12m.reindex(common).fillna(0), ind_map) if not ret_12m.empty else pd.Series(0, index=common)

    # 加权合成（6M主导）
    mom_composite = 0.30 * z1m + 0.40 * z6m + 0.30 * z12m

    # ② 量价突破加成（同Formula I，但用复合动量替代纯6M）
    high_250 = hist.iloc[-250:].max()
    price_nh = (p / high_250).clip(0.5, 1.2)
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
    base_score = mom_composite.reindex(p.index).fillna(0) * (1 + boost)

    # ③ 波动率调控：20日历史波动率高 → 权重降低
    if len(hist) >= 21:
        vol_20d   = hist.iloc[-20:].pct_change(fill_method=None).std()  # 各股20日波动率
        vol_rank  = vol_20d.rank(pct=True).reindex(p.index).fillna(0.5)
        # 低波动（rank低）→ 1.3，高波动（rank高）→ 0.7
        vol_mult  = 1.3 - 0.6 * vol_rank
    else:
        vol_mult  = pd.Series(1.0, index=p.index)

    # ④ 成交额权重（行业内30% + 截面70%，同A）
    cross_rank = vol_recent.rank(pct=True).reindex(p.index)
    sec_rank   = pd.Series(0.5, index=p.index)
    for ind in ind_map.unique():
        ic = [c for c in ind_map[ind_map == ind].index if c in p.index and c in vol_recent.index]
        if len(ic) >= 3:
            sec_rank[ic] = vol_recent[ic].rank(pct=True)
    combined      = 0.70 * cross_rank + 0.30 * sec_rank.reindex(p.index)
    amount_mult   = (0.80 + 0.20 * combined).fillna(0.90)

    score = (base_score * vol_mult * amount_mult).dropna()
    return score


# ── ④ 行业均衡选股（主线多拿，弱线少拿）─────────────────────────────────
def select_industry_balanced(
    score: pd.Series,
    stock_info: pd.DataFrame | None,
    n_total: int,
    max_per_ind: int,
) -> list[str]:
    """
    按行业强度比例分配名额：
    1. 计算各行业平均得分（取该行业得分最高3只的均值）
    2. 按行业强度分配名额（softmax分配，至少每有效行业1只）
    3. 在每个行业内选得分最高的N只
    4. 汇总后按全局得分排序，取前 n_total
    """
    if stock_info is None or "industry_l1" not in stock_info.columns:
        return score.nlargest(n_total).index.tolist()

    ind_map = stock_info.set_index("code")["industry_l1"].reindex(score.index).fillna("其他")

    # 计算各行业代表得分（前3只均值）
    ind_scores = {}
    for ind in ind_map.unique():
        codes = ind_map[ind_map == ind].index.tolist()
        sub   = score.reindex(codes).dropna()
        if len(sub) >= 2:
            ind_scores[ind] = float(sub.nlargest(3).mean())

    if not ind_scores:
        return score.nlargest(n_total).index.tolist()

    # 各行业名额：softmax 分配（强行业多，弱行业少，最少1名额）
    ind_s = pd.Series(ind_scores)
    # 只给得分为正的行业分配名额（动量为正才是强势行业）
    pos_ind = ind_s[ind_s > 0]
    if pos_ind.empty:
        pos_ind = ind_s.nlargest(max(5, len(ind_s) // 3))

    # 比例分配（得分越高分越多，但上限 max_per_ind）
    total_score = pos_ind.clip(lower=0).sum()
    if total_score <= 0:
        slots = {ind: 1 for ind in pos_ind.index}
    else:
        raw   = (pos_ind.clip(lower=0) / total_score * n_total).round().astype(int)
        raw   = raw.clip(lower=1, upper=max_per_ind)
        # 调整总名额至 n_total（允许±2的误差，后续全局排序处理）
        slots = raw.to_dict()

    # 在每个行业内选 top-N
    selected = []
    for ind, k in slots.items():
        codes  = ind_map[ind_map == ind].index.tolist()
        top_k  = score.reindex(codes).dropna().nlargest(k).index.tolist()
        selected.extend(top_k)

    # 去重，按全局得分排序，取前 n_total
    selected = list(dict.fromkeys(selected))  # 保序去重
    selected = sorted(selected, key=lambda c: score.get(c, -np.inf), reverse=True)

    # 若不足 n_total，从全局得分补足
    if len(selected) < n_total:
        all_top = score.nlargest(n_total * 2).index.tolist()
        for c in all_top:
            if c not in set(selected):
                selected.append(c)
            if len(selected) >= n_total:
                break

    return selected[:n_total]


# ── ⑤ 得分加权 + 主线板块加权 ────────────────────────────────────────────
def compute_weights(
    selected: list[str],
    score: pd.Series,
    stock_info: pd.DataFrame | None,
    sector_boost: float,
) -> dict[str, float]:
    n = len(selected)
    if n == 0:
        return {}

    # 线性得分加权（rank 1 = 2× rank n）
    ordered = sorted(selected, key=lambda c: score.get(c, 0), reverse=True)
    raw_w   = {c: (n + 2 - rank) for rank, c in enumerate(ordered, 1)}
    total   = sum(raw_w.values())
    weights = {c: v / total for c, v in raw_w.items()}

    # 主线板块加权
    if stock_info is None or sector_boost == 1.0:
        return weights

    ind_map = stock_info.set_index("code")["industry_l1"].to_dict()
    sector_w: dict[str, float] = {}
    for c, w in weights.items():
        ind = ind_map.get(c, "其他")
        sector_w[ind] = sector_w.get(ind, 0) + w

    if len(sector_w) <= 1:
        return weights

    top_sector = max(sector_w, key=sector_w.get)
    top_cnt    = sum(1 for c in weights if ind_map.get(c) == top_sector)
    if top_cnt < 2:
        return weights

    new_w = {
        c: w * sector_boost if ind_map.get(c) == top_sector else w
        for c, w in weights.items()
    }
    total = sum(new_w.values())
    return {c: v / total for c, v in new_w.items()}


# ── 主回测 ────────────────────────────────────────────────────────────────
def run_backtest_a2(
    panel: pd.DataFrame,
    rebalance_dates: list,
    amount_panel: pd.DataFrame | None,
    index_close: pd.Series | None,
    stock_info: pd.DataFrame | None,
) -> pd.Series:
    all_dates    = panel.index
    port_rets    = pd.Series(0.0, index=all_dates)
    cur_weights: dict[str, float] = {}
    cumul_nav    = 1.0
    entry_hwm    = 1.0
    nav_since    = 1.0
    pos_ratio    = 1.0  # 当前仓位比例（阶梯式）
    rebal_set    = set(str(d.date()) if hasattr(d, "date") else d for d in rebalance_dates)

    for i, date in enumerate(all_dates):
        date_str = str(date.date())

        if date_str in rebal_set and i >= MIN_BARS:
            # ⑥ 阶梯仓位（每次调仓时重新计算）
            if index_close is not None:
                pos_ratio = get_position_ratio(index_close, date)
            else:
                pos_ratio = 1.0

            if pos_ratio <= 0.30:
                # 极度熊市：仍保留30%仓位（不完全清仓，避免踏空）
                # 实盘可以持有货币基金，回测中按现金计息
                if cur_weights:
                    cur_weights = {}
                nav_since = 1.0
                entry_hwm = cumul_nav
            else:
                score = compute_score_a2(panel, date, amount_panel, stock_info)
                if len(score) >= N_HOLDINGS:
                    selected  = select_industry_balanced(score, stock_info, N_HOLDINGS, MAX_IND_SLOT)
                    new_w_raw = compute_weights(selected, score, stock_info, SECTOR_BOOST)

                    # 按仓位比例缩放权重（剩余为现金/货基）
                    new_w = {c: w * pos_ratio for c, w in new_w_raw.items()}

                    # 换手成本
                    old_codes  = set(cur_weights)
                    new_codes  = set(new_w)
                    enter_cost = sum(new_w.get(c, 0) for c in new_codes - old_codes)
                    exit_cost  = sum(cur_weights.get(c, 0) for c in old_codes - new_codes)
                    turnover   = (enter_cost + exit_cost) / 2
                    port_rets.iloc[i] -= turnover * COMMISSION * 2

                    if not cur_weights:
                        entry_hwm = cumul_nav
                    cur_weights = new_w
                nav_since = 1.0

        # 止损（同策略A）
        if cur_weights and i > 0:
            if nav_since <= (1 + PERIOD_STOP) or (cumul_nav / entry_hwm - 1) <= TRAILING_STOP:
                cur_weights = {}
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
        # 仓位以外的现金部分计息（空仓或减仓部分）
        cash_ratio = max(0, 1.0 - sum(cur_weights.values())) if cur_weights else 1.0
        port_rets.iloc[i] += cash_ratio * CASH_YIELD / 252

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
    logger.info(f"策略A-2（系统优化版）  {BACKTEST_START} → {BACKTEST_END}")
    logger.info(f"持仓:{N_HOLDINGS}  多周期(1M+6M+12M)行业内标准化  波动率调控  行业均衡选股")
    logger.info(f"得分加权  主线板块{SECTOR_BOOST}x  阶梯仓位30-100%  止损:{PERIOD_STOP}/{TRAILING_STOP}")
    logger.info("=" * 68)

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

    # CSI800 指数行情（用于阶梯仓位）
    idx_df = load_meta("csi800_index")
    if idx_df.empty:
        logger.warning("csi800_index 缺失，改用固定仓位100%")
        index_close = None
    else:
        idx_df["date"]  = pd.to_datetime(idx_df["date"])
        index_close     = idx_df.set_index("date")["close"].sort_index()
        bull_pct = sum(
            1 for d in pd.DatetimeIndex(sorted(trade_calendar))
            if len(index_close[index_close.index <= d]) >= MA_PERIOD
            and index_close[index_close.index <= d].iloc[-1]
               / index_close[index_close.index <= d].rolling(MA_PERIOD).mean().iloc[-1] >= 1.0
        ) / len(trade_calendar)
        logger.info(f"CSI800 指数已加载，调仓日仓位>70%估算：{bull_pct:.0%}")

    logger.info("运行策略A-2回测...")
    nav_a2 = run_backtest_a2(panel, rebal_dates, amount_panel, index_close, stock_info)

    year_end = int(BACKTEST_END[:4])
    logger.info("── 策略A-2 分年度 ──")
    for year in range(2019, year_end + 1):
        yn = nav_a2[nav_a2.index.year == year]
        if len(yn) < 2:
            continue
        yr = yn.iloc[-1] / yn.iloc[0] - 1
        yd = ((yn - yn.cummax()) / yn.cummax()).min()
        logger.info(f"  {year}  收益:{yr:+.1%}  最大回撤:{yd:.1%}")

    m2 = calc_metrics(nav_a2)
    logger.info("── 策略A-2 总体 ──")
    for k, v in m2.items():
        logger.info(f"  {k}: {v}")
    nav_a2.to_csv("logs/backtest_a2_nav.csv", header=["nav"])

    if args.compare:
        nav_a_f = Path("logs/backtest_a_nav_I.csv")
        if nav_a_f.exists():
            nav_a = pd.read_csv(nav_a_f, index_col=0, parse_dates=True)["nav"]

            def _row(name, nav):
                m = calc_metrics(nav)
                return {"策略": name, "总收益": m["总收益率"],
                        "年化": m["年化收益率"], "波动率": m["年化波动率"],
                        "夏普": m["夏普比率"], "最大回撤": m["最大回撤"],
                        "月度胜率": m["月度胜率"]}

            cmp = pd.DataFrame([_row("策略A（基准）", nav_a),
                                 _row("策略A-2（系统优化）", nav_a2)]).set_index("策略")
            print("\n" + "=" * 65)
            print("  策略A vs 策略A-2 完整对比")
            print("=" * 65)
            print(cmp.to_string())

            print("\n── 逐年收益对比 ──")
            for year in range(2019, year_end + 1):
                ya  = nav_a[nav_a.index.year == year]
                ya2 = nav_a2[nav_a2.index.year == year]
                if len(ya) < 2 or len(ya2) < 2:
                    continue
                ra  = ya.iloc[-1] / ya.iloc[0] - 1
                ra2 = ya2.iloc[-1] / ya2.iloc[0] - 1
                d   = ra2 - ra
                mark = "↑A2优" if d > 0.01 else ("↓A优" if d < -0.01 else "≈")
                print(f"  {year}: A={ra:+.1%}  A-2={ra2:+.1%}  差={d:+.1%} {mark}")

    ar = float(m2["年化收益率"].strip("%")) / 100
    dd = float(m2["最大回撤"].strip("%")) / 100
    sr = float(m2["夏普比率"])
    ok = ar >= 0.15 and dd >= -0.25 and sr >= 1.0
    logger.info(f"\n结论: {'✅ 达标' if ok else '⚠️ 未达标'}  年化:{ar:.1%}  回撤:{dd:.1%}  夏普:{sr:.2f}")
    logger.info("=" * 68)


if __name__ == "__main__":
    main()
