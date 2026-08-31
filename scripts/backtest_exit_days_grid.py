"""
MA10 exit_days 网格（3/4/5/6/8）——降本主战场的首个变体测试。

三道前置（专家 2026-08-31 锁定）:
  ① 网格找平台而非单点 A/B: 4 本身从没做过敏感性, 只有 6 好而 5/8 都差=尖点不采纳
  ② 8/19 型窗口单独验证: 延长退出=跌停潮多扛 N 天, 用含 8/19 的近段窗口量化
  ③ 动的是 7/8 消融幸存者, 证据标准与拥挤度过滤同级(多窗口+网格)
事前预测（专家）: 4→6 触发降 30~40%、省 0.4~0.6pp 成本、吐回部分深度回撤,
净效果接近打平, 看点是风险轮廓。
窗口: 全期 / 近段2022-2026.8 / 含8/19的近段2024.7-2026.8
新口径引擎(切档显性计成本)。
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

START, END = "2019-01-01", "2026-08-28"
WINDOWS = [("全期2019-2026.8", "2019-01-01", "2026-08-28"),
           ("近段2022-2026.8", "2022-01-01", "2026-08-28"),
           ("含8/19近段2024.7-26.8", "2024-07-01", "2026-08-28")]


def main():
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
    print(f"Panel: {len(prices)}只, {panel.shape[0]}天", flush=True)

    ic = load_meta("csi800_index")
    ic = ic.set_index("date")["close"].sort_index()
    ic.index = pd.to_datetime(ic.index)

    sh = load_daily("000001", "2014-06-01", END)
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))

    for wname, lo, hi in WINDOWS:
        p = panel[(panel.index >= lo) & (panel.index <= hi)]
        a = ap[(ap.index >= lo) & (ap.index <= hi)]
        rebal = [d for d in make_rebal_dates(cal, "biweekly") if lo <= d <= hi]
        print(f"\n=== {wname} ===", flush=True)
        print(f"{'exit_days':<10}{'年化':>9}{'夏普':>8}{'回撤':>9}{'年成本损耗':>10}", flush=True)
        for ed in (3, 4, 5, 6, 8):
            cfg = BacktestConfig(**{**DEFAULT_CONFIG.to_dict(), "ma_exit_days": ed})
            nav, m = run_backtest(p, a, rebal, cfg, ic)
            cm = calc_metrics(nav)
            ar = float(str(cm["年化收益率"]).strip("%"))
            sr = float(cm["夏普比率"])
            dd = float(str(cm["最大回撤"]).strip("%"))
            print(f"{ed:<10}{ar:>+8.2f}%{sr:>8.2f}{dd:>8.2f}%"
                  f"{m.get('annual_cost_drag', 0) * 100:>9.2f}%", flush=True)


if __name__ == "__main__":
    main()
