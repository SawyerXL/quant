"""
拥挤度过滤样本外验证 (2015-2018, 2026-08-30 外部评审驱动)。

背景: 过滤动机来自8/19跌停潮, 原四窗口(2019-2026)是同一历史的
重叠切片, 不是独立样本。本脚本用完全独立的历史踩踏场景
(2015股灾/2016熔断/2018熊市)做OOS验证。

结果: TOP60无过滤 -8.40%/-1.38/-33.3% vs +过滤5% -6.80%/-1.22/-28.7%
→ 方向一致(+1.6pp/回撤-4.6pp), 过滤在OOS成立。

注意: trade_calendar meta 2019前缺失, 日历用上证全史日期替代。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
from loguru import logger
logger.remove()

from data.storage import load_daily, load_meta
from backtest_config import BacktestConfig, DEFAULT_CONFIG
from backtest_engine import run_backtest, make_rebal_dates, calc_metrics

START, END = "2015-01-01", "2018-12-31"


def main():
    # 日历: trade_calendar meta 2019前缺失 → 上证全史日期
    sh = load_daily("000001", "2014-06-01", END)
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))

    meta = load_meta("stock_info_full")
    codes = meta["code"].tolist() if not meta.empty else []
    prices, amounts = {}, {}
    for code in codes:
        try:
            d = load_daily(code, START, END)
            if d.empty:
                continue
            d["date"] = pd.to_datetime(d["date"])
            d = d.set_index("date").sort_index()
            cl = pd.to_numeric(d["close"], errors="coerce").dropna()
            amt = pd.to_numeric(d.get("amount", pd.Series(dtype=float)), errors="coerce")
            if len(cl) >= 250:
                prices[code] = cl
                if len(amt) >= 250:
                    amounts[code] = amt
        except Exception:
            pass
    panel = pd.DataFrame(prices).sort_index()
    ap = pd.DataFrame(amounts).sort_index()
    print(f"Panel: {len(prices)}只, {panel.shape[0]}天")

    idx = load_meta("csi800_index")
    idx_c = idx.set_index("date")["close"].sort_index()
    idx_c.index = pd.to_datetime(idx_c.index)

    rebal = [d for d in make_rebal_dates(cal, "biweekly") if START <= d <= END]
    print("OOS窗口 2015-2018 (含2015股灾/2016熔断/2018熊市)")
    print(f"{'配置':<22}{'年化':>9}{'夏普':>8}{'最大回撤':>10}")
    for name, cfg in [
        ("TOP60无过滤", BacktestConfig(**{**DEFAULT_CONFIG.to_dict(), "max_vol20": 999})),
        ("TOP60+过滤5%", BacktestConfig(**{**DEFAULT_CONFIG.to_dict(), "max_vol20": 5.0})),
    ]:
        nav, _ = run_backtest(panel, ap, rebal, cfg, idx_c)
        cm = calc_metrics(nav)
        print(f"{name:<22}{float(str(cm['年化收益率']).strip('%')):>+8.2f}%"
              f"{float(cm['夏普比率']):>8.2f}{float(str(cm['最大回撤']).strip('%')):>9.2f}%")


if __name__ == "__main__":
    main()
