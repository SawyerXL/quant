"""
个股止损 A/B 回测 —— 评估 stop_monitor.py 那套"逐股追踪止损"到底帮不帮忙。

背景(为什么建这个)：
  - 回测引擎 run_backtest_a2 的止损是【组合层】：整本 NAV 跌破 -15%/-18% 才清仓。
  - 但 Windows 上 live 跑的 stop_monitor.py 是【逐股层】：每只票各自 成本-15% / 峰值-18%,
    每 20 分钟按 tick 扫一次。这套逐股追踪止损从未进过回测。
  - 2026-07-08/09 它把 10+ 只票(含浮盈的 300394 +4.8%)一刀切了 → 需要 A/B 验证设计。

本 harness 复刻 LIVE 主策略(成交额TOP30 + 等权 + MA200阶梯仓位 + MA10三日出清),
唯一变量 = 逐股止损规则, 对比 6 个变种。不含组合层 NAV 止损(live 信号里本就没有)。

口径诚实说明：
  - 回测用【日线收盘价】, live 用【20分钟 tick】→ live V0 比回测 V0 更易触发。
    因此回测会【低估】现行问题, close化/败者限定的真实收益大概率更大。
  - 成本基准: 建仓调仓日的收盘价; 持仓跨调仓不重置成本(近似券商摊平成本)。
  - 峰值: 建仓以来的收盘价滚动最高; 止损卖出后该票出场, 下次调仓若仍在TOP30才重进(重置成本/峰值)。
  - 未建模: 一手可买性过滤(略去几只688高价股, 对组合级指标可忽略); 涨跌停禁买卖(与变种对比无关)。

运行:
    python scripts/backtest_stoploss_ab.py
    BACKTEST_START=2019-01-01 BACKTEST_END=2026-07-07 python scripts/backtest_stoploss_ab.py
"""
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd
from loguru import logger
from data.storage import load_meta

from run_backtest_a import (
    load_panels, calc_metrics,
    BACKTEST_START, COMMISSION, MIN_BARS, LIQUIDITY_THRESH, MA_PERIOD, CASH_YIELD,
)
from run_backtest_a2 import get_position_ratio, _make_rebal_dates

N_HOLDINGS     = int(os.getenv("N_HOLDINGS", "30"))
MA10_EXIT_DAYS = 3

# ── 6 个止损变种 ──────────────────────────────────────────────────────────
# 每个规则: (price, cost, peak) -> True 表示触发止损卖出。其余策略逻辑完全一致。
COST_STOP  = -0.15   # 距成本硬止损
TRAIL_STOP = -0.18   # 距峰值追踪止损

def v0_live(price, cost, peak):
    """现行 live: 成本-15% 或 峰值-18%(不管盈亏一律追踪)。"""
    return price / cost - 1 <= COST_STOP or price / peak - 1 <= TRAIL_STOP

def v1_none(price, cost, peak):
    """无逐股止损(仅靠 MA10出清 + MA200择时)。"""
    return False

def v2_cost_only(price, cost, peak):
    """只有成本-15%硬止损, 不追踪(永不切浮盈票)。"""
    return price / cost - 1 <= COST_STOP

def v3_loser_trail(price, cost, peak):
    """败者限定追踪: 成本-15%; 追踪-18%仅在浮亏(price<cost)时生效, 浮盈票豁免。"""
    if price / cost - 1 <= COST_STOP:
        return True
    return price < cost and price / peak - 1 <= TRAIL_STOP

def v4_trail25(price, cost, peak):
    """放宽追踪到峰值-25%(成本-15%不变)。"""
    return price / cost - 1 <= COST_STOP or price / peak - 1 <= -0.25

def v5_profit_armed(price, cost, peak):
    """利润确认型: 追踪-18%仅在该票曾涨过+15%(peak/cost-1>=0.15)后才武装; 否则只用成本-15%。"""
    if price / cost - 1 <= COST_STOP:
        return True
    return peak / cost - 1 >= 0.15 and price / peak - 1 <= TRAIL_STOP

VARIANTS = {
    "V0 现行live(成本-15/峰-18)": v0_live,
    "V1 无逐股止损":              v1_none,
    "V2 仅成本-15":               v2_cost_only,
    "V3 败者限定追踪":            v3_loser_trail,
    "V4 追踪放宽-25":             v4_trail25,
    "V5 利润确认后追踪":          v5_profit_armed,
}


def _drop_corrupt_codes(codes: list[str], start: str, end: str) -> list[str]:
    """预扫描: 剔除本地 parquet 无法读取的 code(损坏文件会让整个回测崩)。返回可用 code。"""
    import pyarrow.parquet as pq
    from data.storage import _daily_path
    good, bad = [], []
    for code in codes:
        ok = True
        for year in range(int(start[:4]), int(end[:4]) + 1):
            p = _daily_path(code, year)
            if p.exists():
                try:
                    pq.read_metadata(p)
                except Exception:
                    ok = False; bad.append(f"{code}/{year}"); break
        if ok:
            good.append(code)
    if bad:
        logger.warning(f"⚠️ 剔除损坏日线文件 {len(bad)} 个: {bad} → 需修复(re-fetch)")
    return good


def _select_top_turnover(amount_hist: pd.DataFrame, n: int) -> list[str]:
    """成交额 TOP-N 选股(20日均额, 过流动性门槛)。"""
    avg = amount_hist.iloc[-20:].mean().dropna()
    avg = avg[avg > LIQUIDITY_THRESH]
    return avg.nlargest(n).index.tolist()


def simulate(panel, amount_panel, rebal_dates, index_close, stop_rule, n=N_HOLDINGS):
    """
    逐股级模拟。返回 (nav: pd.Series, stats: dict)。
    holdings[code] = {"cost":float, "peak":float, "w":float}
    止损卖出的票进 cash, 直到下个调仓日若仍在 TOP30 才重进。
    """
    all_dates = panel.index
    port_rets = pd.Series(0.0, index=all_dates)
    holdings: dict[str, dict] = {}
    below_ma10: dict[str, int] = {}
    pos_ratio = 1.0
    rebal_set = set(str(d.date()) if hasattr(d, "date") else str(d) for d in rebal_dates)
    stats = {"stop_events": 0, "winner_cuts": 0, "ma10_exits": 0}

    closes = panel  # alias
    for i, date in enumerate(all_dates):
        date_str = str(date.date())

        # ① 当日持仓收益(close_i / close_{i-1})
        if holdings and i > 0:
            prev = closes.iloc[i - 1]
            cur  = closes.iloc[i]
            ret = 0.0
            for code, h in holdings.items():
                pp = prev.get(code); cp = cur.get(code)
                if pp and cp and not pd.isna(pp) and not pd.isna(cp) and pp > 0:
                    ret += h["w"] * (cp / pp - 1)
            port_rets.iloc[i] += ret
        # 现金部分计息
        cash_ratio = max(0.0, 1.0 - sum(h["w"] for h in holdings.values())) if holdings else 1.0
        port_rets.iloc[i] += cash_ratio * CASH_YIELD / 252

        # ② 更新峰值 + MA10 连破计数(基于 close_i)
        cur = closes.iloc[i]
        for code, h in holdings.items():
            cp = cur.get(code)
            if cp and not pd.isna(cp) and cp > 0:
                h["peak"] = max(h["peak"], cp)

        # ③ MA10 三日出清(所有变种一致, 属主策略特性)
        if holdings and i >= 10:
            for code in list(holdings.keys()):
                sub = closes[code].iloc[max(0, i - 9):i + 1].dropna()
                cp = cur.get(code)
                if len(sub) >= 10 and cp and not pd.isna(cp):
                    ma10 = sub.iloc[-10:].mean()
                    below_ma10[code] = below_ma10.get(code, 0) + 1 if cp < ma10 else 0
                    if below_ma10[code] >= MA10_EXIT_DAYS:
                        del holdings[code]; below_ma10.pop(code, None)
                        stats["ma10_exits"] += 1

        # ④ 逐股止损(变种唯一差异)
        if holdings:
            for code in list(holdings.keys()):
                h = holdings[code]; cp = cur.get(code)
                if not cp or pd.isna(cp) or cp <= 0 or h["cost"] <= 0:
                    continue
                if stop_rule(cp, h["cost"], h["peak"]):
                    if cp >= h["cost"]:
                        stats["winner_cuts"] += 1   # 在成本之上被切(浮盈/打平)
                    stats["stop_events"] += 1
                    del holdings[code]; below_ma10.pop(code, None)

        # ⑤ 调仓日: 重建 TOP30 等权 * pos_ratio
        if date_str in rebal_set and i >= MIN_BARS:
            pos_ratio = get_position_ratio(index_close, date) if index_close is not None else 1.0
            if pos_ratio <= 0.30:
                if holdings:
                    port_rets.iloc[i] -= sum(h["w"] for h in holdings.values()) / 2 * COMMISSION * 2
                holdings = {}; below_ma10 = {}
            else:
                amount_hist = amount_panel[amount_panel.index <= date]
                selected = _select_top_turnover(amount_hist, n)
                if len(selected) >= n:
                    w = pos_ratio / n
                    old_w = {c: h["w"] for c, h in holdings.items()}
                    new_holdings: dict[str, dict] = {}
                    for c in selected:
                        cp = cur.get(c)
                        if not cp or pd.isna(cp) or cp <= 0:
                            continue
                        if c in holdings:  # 持仓延续: 成本/峰值不重置(近似券商成本)
                            h = holdings[c]; h["w"] = w; new_holdings[c] = h
                        else:              # 新建仓: 重置成本=峰值=当日收盘
                            new_holdings[c] = {"cost": float(cp), "peak": float(cp), "w": w}
                    # 换手成本
                    new_w = {c: h["w"] for c, h in new_holdings.items()}
                    enter = sum(new_w.get(c, 0) for c in set(new_w) - set(old_w))
                    exit_ = sum(old_w.get(c, 0) for c in set(old_w) - set(new_w))
                    port_rets.iloc[i] -= (enter + exit_) / 2 * COMMISSION * 2
                    holdings = new_holdings
                    below_ma10 = {c: below_ma10.get(c, 0) for c in holdings}

    nav = (1 + port_rets).cumprod()
    return nav, stats


def main():
    end = os.getenv("BACKTEST_END", "") or sorted(load_meta("trade_calendar")["trade_date"].tolist())[-1]
    start = BACKTEST_START
    logger.info("=" * 72)
    logger.info(f"个股止损 A/B  {start} → {end}  (TOP{N_HOLDINGS}等权 + MA200 + MA10出清)")
    logger.info("=" * 72)

    cal_df = load_meta("trade_calendar")
    trade_calendar = [d for d in cal_df["trade_date"].tolist() if start <= d <= end]
    rebal_dates = _make_rebal_dates(trade_calendar, "biweekly")
    logger.info(f"调仓日期: {len(rebal_dates)} 个")

    codes = sorted(load_meta("csi800")["code"].tolist())
    codes = _drop_corrupt_codes(codes, start, end)
    logger.info("加载价格+成交额矩阵(CSI800)...")
    panel, amount_panel = load_panels(codes, start, end)
    logger.info(f"价格矩阵: {panel.shape[0]}天 × {panel.shape[1]}只")

    idx_df = load_meta("csi800_index")
    idx_df["date"] = pd.to_datetime(idx_df["date"])
    index_close = idx_df.set_index("date")["close"].sort_index()
    index_close = pd.to_numeric(index_close, errors="coerce").dropna()

    year_end = int(end[:4])
    rows = []
    navs = {}
    for name, rule in VARIANTS.items():
        nav, stats = simulate(panel, amount_panel, rebal_dates, index_close, rule)
        navs[name] = nav
        m = calc_metrics(nav)
        rows.append({
            "变种": name,
            "总收益": m["总收益率"], "年化": m["年化收益率"], "夏普": m["夏普比率"],
            "最大回撤": m["最大回撤"], "月度胜率": m["月度胜率"],
            "止损发火": stats["stop_events"], "切浮盈": stats["winner_cuts"],
            "MA10出清": stats["ma10_exits"],
        })
        logger.info(f"{name:24s} 年化{m['年化收益率']:>7} 夏普{m['夏普比率']:>5} "
                    f"回撤{m['最大回撤']:>7} 发火{stats['stop_events']:>4} 切浮盈{stats['winner_cuts']:>4}")

    df = pd.DataFrame(rows).set_index("变种")
    print("\n" + "=" * 90)
    print(f"  个股止损变种对比  {start} → {end}")
    print("=" * 90)
    print(df.to_string())

    # 逐年收益
    print("\n── 逐年收益 ──")
    ycols = {}
    for name, nav in navs.items():
        yr = {}
        for year in range(2019, year_end + 1):
            yn = nav[nav.index.year == year]
            if len(yn) >= 2:
                yr[year] = yn.iloc[-1] / yn.iloc[0] - 1
        ycols[name] = yr
    ydf = pd.DataFrame(ycols)
    print(ydf.map(lambda x: f"{x:+.1%}" if pd.notna(x) else "—").to_string())

    out = Path("logs/backtest/stoploss_ab.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out)
    print(f"\n结果已存 → {out}")


if __name__ == "__main__":
    main()
