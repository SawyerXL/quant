"""
资金闲置 A/B 回测（2026-09-03，pTrade 归因闭环后的修复验证）

归因结论(见 ptrade 迁移记忆): pTrade 回测低收益的真凶是"资金闲置"——
股数计算用 min(nav,CAP)×ratio/N_HOLDINGS 预算(20万口径≈2333元/只),
池子买得起过滤用 40万×ratio/N_HOLDINGS 口径(3333-6667元/只), 两套预算不一致
+ 整手取整 → 实际只投出约一半资金, 其余现金闲置(beta 0.76 佐证)。

本脚本按"股数级"精确模拟 pTrade 行为(整手取整/闲置现金/T收盘成交/5元最低费),
对照两个口径:
  ptrade  = 现口径: 每只预算 = 目标投入/N_HOLDINGS
  fix     = 修复口径: 每只预算 = 目标投入/实际持仓数(全额投入)
判定: ptrade 模式应复现 pTrade 回测(≈2.2%/年); fix 模式应显著更高。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd

from data.storage import load_meta
from scripts.run_backtest_a import load_panels
from scripts.run_backtest_a2 import get_position_ratio
from scripts.daily_signal_a_v2 import is_rebalance_day

N_HOLDINGS  = 60
MAX_VOL20   = 5.0
MA10_EXIT_DAYS = 4
MAX_TURNOVER = 0.50
POOL_BASE   = 400_000
CAP         = 200_000
COMM_RATE   = 0.0001
MIN_COMM    = 5.0
STAMP_SELL  = 0.001

START = "2019-01-02"
END   = "2026-09-01"


def _select_pool(avg20, vol20, prices, budget, n):
    picked = []
    for code in avg20.nlargest(n * 2).index:
        if len(picked) >= n:
            break
        v = vol20.get(code)
        if pd.notna(v) and v > MAX_VOL20:
            continue
        p = prices.get(code)
        if p and p > 0:
            lot = 200 if code.startswith("688") else 100
            if p * lot <= budget:
                picked.append(code)
    return picked[:n]


def run(budget_mode: str) -> dict:
    cal = sorted(load_meta("trade_calendar")["trade_date"].tolist())
    csi800 = sorted(load_meta("csi800")["code"].astype(str).str.zfill(6).tolist())
    idx = load_meta("csi800_index").copy()
    idx["date"] = pd.to_datetime(idx["date"])
    idx = idx.sort_values("date").set_index("date")
    idx_close = pd.to_numeric(idx["close"], errors="coerce").dropna()

    price, amt = load_panels(csi800, "2018-03-01", END)
    if price.empty or amt.empty:
        raise RuntimeError("数据加载失败")

    dates = [d for d in price.index if START <= str(d.date()) <= END]
    rebal_set = {d for d in dates if is_rebalance_day(str(d.date()), cal)}

    nav = 200_000.0
    cash = 200_000.0
    holdings = {}                     # code -> shares
    days_below = {}
    nav_series = []
    n_trades = 0
    idle_sum = 0.0
    idle_n = 0

    for i, d in enumerate(dates):
        pos = price.index.get_loc(d)
        if pos < 220:
            nav_series.append(nav)
            continue
        prev = pos - 1
        close_t = price.iloc[pos]
        closes_upto = price.iloc[:pos]
        amt_upto    = amt.iloc[:pos]
        prices_t1   = closes_upto.iloc[-1].dropna()
        avg20       = amt_upto.tail(20).mean()
        vol20       = closes_upto.pct_change().iloc[-21:-1].std() * 100
        ratio = get_position_ratio(idx_close, pd.Timestamp(d))

        # MA10 出清
        exits = []
        if holdings:
            for c in list(holdings):
                s = closes_upto[c].dropna()
                if len(s) < 10:
                    days_below[c] = 0
                    continue
                if float(s.iloc[-1]) < float(s.iloc[-10:].mean()):
                    days_below[c] = days_below.get(c, 0) + 1
                    if days_below[c] >= MA10_EXIT_DAYS:
                        exits.append(c)
                else:
                    days_below[c] = 0

        rebal = d in rebal_set
        target = []
        invested_target = min(nav, CAP) * ratio
        pool_budget = POOL_BASE * ratio / N_HOLDINGS

        if ratio <= 0.30:
            target = []
            days_below = {}
        elif rebal or exits:
            pool = _select_pool(avg20, vol20, prices_t1, pool_budget, N_HOLDINGS)
            held_after = [c for c in holdings if c not in exits]
            if exits:
                repl = [c for c in pool if c not in held_after and c not in exits][:len(exits)]
                held_after += repl
                days_below = {k: v for k, v in days_below.items() if k in held_after}
            if rebal:
                new_set = set(pool)
                prev_set = set(held_after)
                buy_list = [c for c in pool if c not in prev_set and c not in exits]
                sell_list = [c for c in held_after if c not in new_set]
                turnover = (len(buy_list) + len(sell_list)) / (2 * N_HOLDINGS)
                if turnover > MAX_TURNOVER:
                    max_rep = int(N_HOLDINGS * MAX_TURNOVER)
                    old_to_rep = [c for c in held_after if c not in new_set]
                    sell_list = old_to_rep[:max_rep]
                    keep_old = set(held_after) - set(sell_list)
                    buy_list = [c for c in pool if c not in keep_old and c not in exits][:max_rep]
                target = [c for c in held_after if c not in sell_list] + buy_list
            else:
                target = held_after

        # ── 执行(股数级, T收盘成交) ──
        if budget_mode == "fix":
            budget = invested_target / max(len(target), 1)
        else:
            budget = invested_target / N_HOLDINGS

        # 卖出: 不在 target 的全卖; 在 target 的不调权(与 pTrade 同)
        for c in list(holdings):
            if c not in target:
                sh = holdings.pop(c)
                px = close_t.get(c)
                if pd.notna(px) and sh > 0:
                    val = sh * float(px)
                    cash += val - (max(MIN_COMM, COMM_RATE * val) + STAMP_SELL * val)
                    n_trades += 1
        # 买入: 新进入的按预算整手
        for c in target:
            if c not in holdings:
                p = prices_t1.get(c)
                if not p or p <= 0:
                    continue
                lot = 200 if c.startswith("688") else 100
                sh = int(budget / float(p) / lot) * lot
                if sh <= 0:
                    continue
                px = close_t.get(c)
                if pd.notna(px):
                    val = sh * float(px)
                    cash -= val + max(MIN_COMM, COMM_RATE * val)
                    holdings[c] = sh
                    n_trades += 1

        value = sum(sh * float(close_t.get(c)) for c, sh in holdings.items()
                    if pd.notna(close_t.get(c)))
        nav = cash + value
        # 闲置资金占比(目标投入 vs 实际持仓市值)
        idle_sum += max(0.0, invested_target - value) / nav
        idle_n += 1
        nav_series.append(nav)

    s = pd.Series(nav_series, index=dates)
    rets = s.pct_change().dropna()
    total = s.iloc[-1] / s.iloc[0] - 1
    years = (pd.Timestamp(END) - pd.Timestamp(START)).days / 365.25
    ann = (1 + total) ** (1 / years) - 1
    dd = (s / s.cummax() - 1).min()
    sharpe = rets.mean() / rets.std() * np.sqrt(252) if rets.std() > 0 else 0
    return {"total": total, "annual": ann, "maxdd": dd, "sharpe": sharpe,
            "trades": n_trades, "final_nav": s.iloc[-1], "avg_idle": idle_sum / idle_n}


if __name__ == "__main__":
    for label, mode in (("现口径(每只=目标投入/60)", "ptrade"),
                        ("修复口径(每只=目标投入/实际持仓数)", "fix")):
        r = run(budget_mode=mode)
        print(f"{label}: 累计={r['total']:.2%} 年化={r['annual']:.2%} "
              f"回撤={r['maxdd']:.2%} 夏普={r['sharpe']:.2f} 笔数={r['trades']} "
              f"平均闲置资金={r['avg_idle']:.1%}")
