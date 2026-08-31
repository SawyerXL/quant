"""
回测引擎 — 可配置参数 + 止损止盈 + 业绩计算
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd, numpy as np
from dataclasses import dataclass
from loguru import logger; logger.remove(); logger.add(sys.stderr, level='ERROR')

from scripts.backtest_config import BacktestConfig


def apply_filters(pool_codes, panel, idx, config, return_tags=False):
    """
    三层漏斗过滤。
    return_tags: 若为True，返回 (codes_list, {code: is_overheated}) 元组
    """
    if idx < 65:
        codes = pool_codes[:min(len(pool_codes), config.pool_size)]
        return (codes, {c: False for c in codes}) if return_tags else codes

    eligible = []
    tags = {}
    for code in pool_codes:
        if code not in panel.columns:
            continue
        cl = panel[code]
        cur_val = cl.iloc[idx]
        if pd.isna(cur_val) or cur_val <= 0:
            continue

        hist = cl.iloc[:idx+1].dropna()
        if len(hist) < 60:
            continue

        cur_hist = hist.iloc[-1]

        # ── 过热检查 ──
        overheat = False; overheat_reasons = []
        if len(hist) >= 21:
            ret20 = (cur_hist / hist.iloc[-21] - 1) * 100
            if ret20 > config.max_20d_return:
                overheat = True; overheat_reasons.append(f'20日{ret20:.0f}%')
        cons_up = 0
        for j in range(len(hist)-1, max(0, len(hist)-16), -1):
            if hist.iloc[j] > hist.iloc[j-1]: cons_up += 1
            else: break
        if cons_up >= config.max_consec_up_days:
            overheat = True; overheat_reasons.append(f'连涨{cons_up}天')
        if len(hist) >= 6:
            ret5 = (cur_hist / hist.iloc[-6] - 1) * 100
            if ret5 > config.max_5d_return:
                overheat = True; overheat_reasons.append(f'5日{ret5:.0f}%')
        if len(hist) >= 22:
            # 严格口径(vol20_use_today=False): 只用T-1及以前, 排除调仓日当天收益的look-ahead可能
            rets = hist.pct_change()
            win = rets.iloc[-20:] if config.vol20_use_today else rets.iloc[-21:-1]
            vol20 = win.std() * 100
            if vol20 > config.max_vol20:
                overheat = True; overheat_reasons.append(f'波动{vol20:.0f}%')
        high20 = hist.iloc[-20:].max()
        dh = (cur_hist / high20 - 1) * 100

        if config.enable_entry_filter:
            if config.ma10_entry_mode == "near_ma10":
                ma10_val = hist.iloc[-10:].mean()
                dist_ma10 = abs(cur_hist / ma10_val - 1) * 100
                if dist_ma10 > config.ma10_tolerance:
                    overheat = True; overheat_reasons.append(f'离MA10{dist_ma10:.0f}%')
            else:
                if dh > -config.min_dist_from_high:
                    overheat = True; overheat_reasons.append(f'近高{dh:.0f}%')

        # Mode: eliminate vs reduce
        if config.overheat_mode == "eliminate" and overheat:
            continue

        eligible.append(code)
        tags[code] = overheat

    def _select(codes):
        if len(codes) >= config.pool_size:
            return codes[:config.pool_size]
        elif len(codes) > 0:
            return codes
        return pool_codes[:min(len(pool_codes), config.pool_size)]

    selected = _select(eligible)
    if return_tags:
        return selected, {c: tags.get(c, False) for c in selected}
    return selected


def run_backtest(panel, amount_panel, rebal_dates, config, index_close=None, open_panel=None):
    """
    主回测引擎。open_panel 可选(ma10_exit_delay=True 时用于次日开盘卖价)。
    返回: (nav_series, metrics_dict)
    """
    all_dates = panel.index
    rebal_set = set(rebal_dates)

    port_rets = pd.Series(0.0, index=all_dates)
    cur_weights = {}; entry_prices = {}
    days_below_ma10 = {}; trail_hwm = {}
    overheat_tags = {}  # code -> bool, updated on rebalance and persists across days
    cumul_nav = 1.0
    # 回撤熔断状态机 (halt_mode != "none" 时生效)
    halted = False
    halt_peak = 1.0
    halt_low = None
    halt_day_count = 0
    halt_triggers = []   # (date, nav, mode)
    # MA200 档位状态机 (抗噪机制: 降档即时/升档确认)
    TIER_POS = [0.30, 0.50, 0.70, 0.85, 1.00]
    TIER_LOW = [None, 0.95, 0.98, 1.02, 1.05]
    TIER_UP = [0.95, 0.98, 1.02, 1.05, None]
    tier_state = 4
    pending_tier = None
    pending_days = 0
    tier_switch_count = 0
    pending_exits = {}   # ma10_exit_delay: code -> 触发日索引
    cooldown_until = {}  # ma10_reentry_cool: code -> 解冻日索引

    trade_count = 0; total_sells = 0; total_buys = 0
    exit_ma10_sells = 0.0    # MA10/止损退出的权重和(换手分解诊断用)
    rebal_sells = 0.0        # 调仓轮换卖出的权重和
    tier_sells = 0.0         # 切档减仓的权重和(pos_ratio下降)
    total_commission_paid = 0.0  # track cumulative commission
    max_single_track = []; top3_track = []

    for i, date in enumerate(all_dates):
        date_str = str(date.date())

        # ── Step 0a: MA200 档位状态机（每日更新；抗噪机制只作用于升档） ──
        if index_close is not None and i >= 200:
            try:
                hist = index_close[index_close.index <= date].dropna()
                if len(hist) >= 200:
                    ratio = float(hist.iloc[-1] / hist.rolling(200).mean().iloc[-1]) \
                            + getattr(config, "ma200_thresh_shift", 0.0)
                    sm = getattr(config, "ma200_smooth_days", 0)
                    if sm > 1:
                        rs = (hist / hist.rolling(200).mean()).dropna()
                        if len(rs) >= sm:
                            ratio = float(rs.rolling(sm).mean().iloc[-1]) + getattr(config, "ma200_thresh_shift", 0.0)
                    hyst = getattr(config, "ma200_hysteresis", 0.0)
                    conf = getattr(config, "ma200_confirm_days", 0)
                    def tier_of(r):
                        if r >= 1.05: return 4
                        if r >= 1.02: return 3
                        if r >= 0.98: return 2
                        if r >= 0.95: return 1
                        return 0
                    # 降档: 即时生效（避损快）
                    if TIER_LOW[tier_state] is not None and ratio < TIER_LOW[tier_state]:
                        new_t = tier_of(ratio)
                        if new_t < tier_state:
                            tier_switch_count += 1
                            tier_state = new_t
                            pending_tier, pending_days = None, 0
                    # 升档: 需确认（追涨慢）
                    if TIER_UP[tier_state] is not None and ratio >= TIER_UP[tier_state] + hyst:
                        new_t = tier_of(ratio)
                        if new_t > tier_state:
                            if conf <= 1:
                                tier_switch_count += 1
                                tier_state = new_t
                                pending_tier, pending_days = None, 0
                            else:
                                if pending_tier == new_t:
                                    pending_days += 1
                                    if pending_days >= conf:
                                        tier_switch_count += 1
                                        tier_state = new_t
                                        pending_tier, pending_days = None, 0
                                else:
                                    pending_tier, pending_days = new_t, 1
            except Exception:
                pass

        pos_ratio_now = TIER_POS[tier_state]
        bear = getattr(config, "ma200_bear_pos", None)
        if bear is not None and tier_state == 0:
            pos_ratio_now = bear

        # ── Step 0: 回撤熔断检查 ──
        if getattr(config, "halt_mode", "none") != "none" and not halted:
            if cumul_nav / halt_peak - 1 <= -config.halt_dd_limit:
                halted = True
                halt_low = cumul_nav
                halt_day_count = 0
                halt_triggers.append((str(date.date()), round(cumul_nav, 4), config.halt_mode))
                if config.halt_mode == "A" and cur_weights:
                    wsum = sum(cur_weights.values())
                    port_rets.iloc[i] -= wsum * config.commission
                    cur_weights = {}; entry_prices = {}; days_below_ma10 = {}
                    trail_hwm = {}; overheat_tags = {}
                elif config.halt_mode == "C" and cur_weights:
                    wsum = sum(cur_weights.values())
                    if wsum > 0.30:
                        scale = 0.30 / wsum
                        port_rets.iloc[i] -= wsum * (1 - scale) * config.commission
                        cur_weights = {c: w * scale for c, w in cur_weights.items()}

        # ── Step 0b: 延迟次日开盘卖的处理 ──
        if pending_exits and i > 0:
            for code in list(pending_exits.keys()):
                w = cur_weights.get(code, 0)
                if w <= 0:
                    del pending_exits[code]
                    continue
                # 当日MTM已按昨收→今收计, 开盘卖需扣回今开→今收段
                if open_panel is not None:
                    o_t = open_panel.iloc[i].get(code)
                    c_t = panel.iloc[i].get(code)
                    if o_t and c_t and not pd.isna(o_t) and not pd.isna(c_t) and o_t > 0:
                        port_rets.iloc[i] -= w * (c_t / o_t - 1)
                total_commission_paid += w * config.commission
                port_rets.iloc[i] -= w * config.commission
                cur_weights.pop(code, None)
                entry_prices.pop(code, None); days_below_ma10.pop(code, None)
                trail_hwm.pop(code, None)
                trade_count += 1; total_sells += 1
                del pending_exits[code]

        # ── Step 1: Mark-to-market ──
        if cur_weights and i > 0:
            ret = 0.0
            for code, w in cur_weights.items():
                pp = panel.iloc[i-1].get(code)
                cp = panel.iloc[i].get(code)
                if pp and cp and not pd.isna(pp) and not pd.isna(cp) and pp > 0:
                    ret += w * (cp/pp - 1)
            port_rets.iloc[i] += ret

        # ── Step 2: Stops & exits (daily check) ──
        if cur_weights and i >= 10 and config.enable_stops:
            exits = []
            for code in list(cur_weights.keys()):
                col = panel[code] if code in panel.columns else None
                if col is None: continue
                cp = panel.iloc[i].get(code)
                if pd.isna(cp) or cp <= 0: continue
                ep = entry_prices.get(code, cp)
                if ep <= 0: ep = cp

                # Tighten stops for overheated stocks
                stop_tighten = config.overheat_stop_tighten if overheat_tags.get(code, False) else 1.0
                abs_stop = config.absolute_stop * stop_tighten
                trail_stop = config.trailing_stop * stop_tighten

                # Absolute stop
                if config.enable_absolute_stop and cp/ep - 1 <= abs_stop:
                    exits.append(code); continue

                # Trailing stop
                if config.enable_trailing_stop:
                    if code not in trail_hwm or cp > trail_hwm[code]:
                        trail_hwm[code] = cp
                    th = trail_hwm.get(code, cp)
                    if th > 0 and cp/th - 1 <= trail_stop:
                        exits.append(code); continue

                # MA10 exit
                if config.enable_ma10_exit:
                    hist_ma = col.iloc[max(0,i-11):i+1].dropna()
                    if len(hist_ma) >= 5:
                        ma10 = hist_ma.mean()
                        if cp < ma10:
                            days_below_ma10[code] = days_below_ma10.get(code, 0) + 1
                        else:
                            days_below_ma10[code] = 0
                        if days_below_ma10.get(code, 0) >= config.ma_exit_days:
                            exits.append(code)

            for code in set(exits):
                w = cur_weights.pop(code, 0)
                if getattr(config, "ma10_reentry_cool", 0) > 0:
                    cooldown_until[code] = i + config.ma10_reentry_cool
                if getattr(config, "ma10_exit_delay", False):
                    # 延迟次日开盘卖: 当日不卖出, 次日开盘价成交(开盘/昨收近似次日gap)
                    pending_exits[code] = i
                    # 权重暂缓移除, 次日处理
                    cur_weights[code] = w
                    days_below_ma10[code] = days_below_ma10.get(code, 0)
                    continue
                total_commission_paid += w * config.commission
                port_rets.iloc[i] -= w * config.commission
                entry_prices.pop(code, None); days_below_ma10.pop(code, None)
                trail_hwm.pop(code, None)
                exit_ma10_sells += w
                trade_count += 1; total_sells += 1

        # ── Step 3: Take profit (partial sells) ──
        if cur_weights and i > 0 and config.enable_stops and config.enable_take_profit:
            sells = []
            for code in list(cur_weights.keys()):
                ep = entry_prices.get(code)
                if not ep or ep <= 0: continue
                cp = panel.iloc[i].get(code)
                if pd.isna(cp): continue
                pnl = cp/ep - 1

                if pnl >= config.take_profit_2 and cur_weights.get(code, 0) > 0:
                    reduce_w = cur_weights[code] * 0.33
                    sells.append((code, reduce_w))
                elif pnl >= config.take_profit_1 and cur_weights.get(code, 0) > 0:
                    reduce_w = cur_weights[code] * 0.33
                    sells.append((code, reduce_w))

            for code, reduce_w in sells:
                cur_weights[code] = cur_weights.get(code, 0) - reduce_w
                if cur_weights[code] <= 0.001:
                    cur_weights.pop(code)
                    entry_prices.pop(code, None)
                total_commission_paid += reduce_w * config.commission
                port_rets.iloc[i] -= reduce_w * config.commission
                trade_count += 1; total_sells += 1

        # ── Step 4: Rebalance ──
        if date_str in rebal_set and i >= config.min_bars and not halted:
            pos_ratio = pos_ratio_now if index_close is not None else 1.0
            # timing_scale: MA200择时强度系数(<1=熊市降仓更狠), clamp到1
            if pos_ratio < 1.0:
                pos_ratio = min(1.0, pos_ratio * getattr(config, "timing_scale", 1.0))

            if pos_ratio <= 0.3:
                cur_weights = {}; entry_prices = {}; days_below_ma10 = {}
                trail_hwm = {}; overheat_tags = {}
            else:
                amt_avg = amount_panel.iloc[max(0,i-20):i].mean().dropna()
                if getattr(config, "pool_style", "amount") == "momentum":
                    # 评审P1-4: 成交额排名=拥挤度因子。流动性池内按动量排序,
                    # 保留流动性前提的同时选"真正在涨"的票而非"最热"的票。
                    # mom_skip_days>0: 12-1逻辑, 跳过最近N日规避短期反转污染
                    liq = amt_avg.nlargest(config.liquidity_pool).index.tolist()
                    skip = getattr(config, "mom_skip_days", 0)
                    px_end = panel.ffill().iloc[max(0, i - skip)]
                    px_start = panel.ffill().iloc[max(0, i - skip - config.mom_window)]
                    mom = ((px_end / px_start) - 1).reindex(liq).dropna()
                    pool = mom.nlargest(config.pool_size * 2).index.tolist()
                else:
                    pool = amt_avg.nlargest(config.pool_size * 2).index.tolist()
                # 重入冷却: MA10退出的票N日内不买回
                if getattr(config, "ma10_reentry_cool", 0) > 0 and i >= config.min_bars:
                    pool = [c for c in pool if cooldown_until.get(c, -1) <= i]
                selected, sel_tags = apply_filters(pool, panel, i, config, return_tags=True)
                old_set = set(cur_weights.keys())
                n = min(len(selected), config.pool_size)
                # rank buffer: 旧持仓排名在池子规模×mult 以内者保留(进N出N×mult)
                rbm = getattr(config, "rank_buffer_mult", 1.0)
                if rbm > 1.0 and i >= config.min_bars:
                    exit_rank = int(config.pool_size * rbm)
                    keep_old = [c for c in pool[:exit_rank] if c in old_set and c not in selected]
                    for c in keep_old:
                        if c in panel.columns:
                            selected.append(c)
                            sel_tags[c] = False
                    n = len(selected)
                if n == 0: continue

                # Update overheat tags for the new portfolio
                overheat_tags = {c: sel_tags.get(c, False) for c in selected[:n]}

                new_set = set(selected[:n])

                # Keep existing weights for stocks that stay, cap at max_single
                new_w = {}
                for c in old_set & new_set:
                    w = cur_weights[c] * pos_ratio
                    if w > config.max_single:
                        w = config.max_single
                    new_w[c] = w

                # Remaining cash = pos_ratio - old_weights + excess from capping
                remaining_cash = pos_ratio - sum(new_w.values())
                new_only = [c for c in selected[:n] if c not in old_set]
                if new_only and remaining_cash > 0.01:
                    w_per_base = min(config.max_position_pct, remaining_cash / len(new_only))
                    for c in new_only:
                        w = w_per_base
                        # 过热股仓位打折
                        if config.overheat_mode == "reduce" and overheat_tags.get(c, False):
                            w *= config.overheat_position_ratio
                        new_w[c] = w

                # Calculate turnover & commission
                enter_w = sum(new_w.get(c, 0) for c in set(new_w) - old_set)
                exit_w = sum(cur_weights.get(c, 0) for c in old_set - set(new_w))
                rebal_sells += exit_w
                rebal_cost = (enter_w + exit_w) / 2 * config.commission * 2
                total_commission_paid += rebal_cost
                port_rets.iloc[i] -= rebal_cost

                # Update entry prices for new stocks
                cp_s = panel.ffill().iloc[i]
                for c in set(new_w) - old_set:
                    ep = cp_s.get(c)
                    if ep and not pd.isna(ep):
                        entry_prices[c] = float(ep)
                        total_buys += 1; trade_count += 1
                # Remove tracking for sold stocks
                for c in old_set - set(new_w.keys()):
                    entry_prices.pop(c, None); days_below_ma10.pop(c, None)
                    total_sells += 1; trade_count += 1

                cur_weights = new_w
                # Track concentration
                if cur_weights:
                    wvals = sorted(cur_weights.values(), reverse=True)
                    max_single_track.append(wvals[0] if wvals else 0)
                    top3_track.append(sum(wvals[:3]) if len(wvals)>=3 else sum(wvals))

        # ── Step 5: Cash yield ──
        cash_r = max(0, 1.0 - sum(cur_weights.values())) if cur_weights else 1.0
        port_rets.iloc[i] += cash_r * config.cash_yield / 252

        # ── Step 5.5: 做T增厚 (期望值模型, 全市场回测验证+26%/年) ──
        if getattr(config, 'enable_t0', False) and cur_weights and i >= 10:
            daily_t0 = sum(w * config.t0_annual_enhancement / 252
                          for w in cur_weights.values())
            port_rets.iloc[i] += daily_t0

        cumul_nav *= (1 + port_rets.iloc[i])

        # 熔断恢复判定
        if halted:
            halt_day_count += 1
            halt_low = min(halt_low, cumul_nav)
            if halt_day_count >= config.halt_recover_min_days and \
               cumul_nav >= halt_low * (1 + config.halt_recover_rebound):
                halted = False
                halt_peak = cumul_nav
        else:
            halt_peak = max(halt_peak, cumul_nav)

    nav = (1 + port_rets).cumprod()
    # ── Diagnostics ──
    max_single_seen = max(max_single_track) if max_single_track else 0.0
    top3_avg = np.mean(top3_track) if top3_track else 0.0

    # Annualized cost drag
    n_days = (all_dates[-1] - all_dates[0]).days
    ann_factor = 365 / max(n_days, 1)
    annual_cost_drag = total_commission_paid * ann_factor

    return nav, {
        "trades": trade_count, "buys": total_buys, "sells": total_sells,
        "total_commission": total_commission_paid,
        "annual_cost_drag": annual_cost_drag,
        "max_single_weight": max_single_seen,
        "top3_concentration": top3_avg,
        "halt_triggers": halt_triggers,
        "tier_switch_count": tier_switch_count,
        "exit_ma10_sells": round(exit_ma10_sells, 4),
        "rebal_sells": round(rebal_sells, 4),
        "tier_sells": round(tier_sells, 4),
    }


def calc_metrics(nav_series, label=""):
    """计算年化收益、夏普、最大回撤等"""
    n = len(nav_series)
    if n < 2:
        return {"年化收益率": "0.00%", "夏普比率": "0.00", "最大回撤": "0.00%", "年化波动率": "0.00%", "胜率": "0.00%"}

    # Total return
    total_ret = nav_series.iloc[-1] / nav_series.iloc[0] - 1
    days = (nav_series.index[-1] - nav_series.index[0]).days
    ann_ret = (1 + total_ret) ** (365 / max(days, 1)) - 1

    # Daily returns
    daily_r = nav_series.pct_change().dropna()

    # Sharpe
    if daily_r.std() > 0:
        sharpe = (daily_r.mean() * 252 - 0.02) / (daily_r.std() * np.sqrt(252))
    else:
        sharpe = 0.0

    # Max drawdown
    cummax = nav_series.cummax()
    max_dd = (nav_series / cummax - 1).min()

    # Annualized volatility
    ann_vol = daily_r.std() * np.sqrt(252)

    # Win rate
    win_rate = (daily_r > 0).mean()

    # Calmar
    calmar = ann_ret / abs(max_dd) if max_dd != 0 else 0

    # Sortino ratio (downside deviation only)
    downside = daily_r[daily_r < 0]
    if len(downside) > 0 and downside.std() > 0:
        sortino = (daily_r.mean() * 252 - 0.02) / (downside.std() * np.sqrt(252))
    else:
        sortino = 0.0

    # Max drawdown duration (longest consecutive days below previous peak)
    dd_duration = 0
    current_dd_days = 0
    cummax_series = nav_series.cummax()
    for i in range(len(nav_series)):
        if nav_series.iloc[i] < cummax_series.iloc[i]:
            current_dd_days += 1
            dd_duration = max(dd_duration, current_dd_days)
        else:
            current_dd_days = 0

    return {
        "年化收益率": f"{ann_ret*100:.2f}%",
        "夏普比率": f"{sharpe:.2f}",
        "索提诺比率": f"{sortino:.2f}",
        "最大回撤": f"{max_dd*100:.2f}%",
        "最大回撤持续": f"{dd_duration}天",
        "年化波动率": f"{ann_vol*100:.2f}%",
        "月胜率": f"{win_rate*100:.1f}%",
        "Calmar": f"{calmar:.2f}",
        "年化_float": ann_ret,
        "夏普_float": sharpe,
        "索提诺_float": sortino,
        "回撤_float": max_dd,
        "回撤持续_float": dd_duration,
        "波动_float": ann_vol,
        "胜率_float": win_rate,
    }


def make_rebal_dates(calendar, freq="biweekly"):
    """生成调仓日期列表: weekly=每周五, biweekly=每月15日+月末, monthly=每月最后交易日"""
    dates = pd.to_datetime(calendar)
    if freq == "weekly":
        return sorted([str(d.date()) for d in dates if d.weekday() == 4])
    elif freq == "biweekly":
        # 每月15日和最后交易日 (约两周一次)
        result = []
        for d in dates:
            last_day = pd.Timestamp(d.year, d.month, 1) + pd.offsets.MonthEnd(0)
            if d.day == 15 or d == last_day:
                result.append(str(d.date()))
        return sorted(set(result))
    elif freq == "monthly":
        # 每月最后一个交易日
        result = []
        for ym in set((d.year, d.month) for d in dates):
            month_dates = [d for d in dates if d.year == ym[0] and d.month == ym[1]]
            if month_dates:
                result.append(str(month_dates[-1].date()))
        return sorted(result)
    else:
        # 默认双周
        result = []
        for d in dates:
            last_day = pd.Timestamp(d.year, d.month, 1) + pd.offsets.MonthEnd(0)
            if d.day == 15 or d == last_day:
                result.append(str(d.date()))
        return sorted(set(result))
