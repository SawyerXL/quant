"""
低波动信号引擎 A/B（2026-09-05 立项，预检三查通过后开跑）。

预检结果(已入spec): ①相关0.465(中等, 主用vol20) ②残差IC-0.0946
(t=-3.6, 独立alpha确认) ③TOP60子集IC衰减到-0.064(t=-1.9, 排序力打折)
→ 预测按③下调: V1效果打折, V2/V3排序收益预期下调。
变体: 基线等权 / V1逆波动率加权 / V2 lowvol排序取30 / V3=V2+V1。
口径: pool30×50万lot×降档3%(部署口径), 双窗口×双成本档, 输出换手率
+三段拆解(2019-21牛/2022-24.6熊震荡/2024.7-26.8)。红旗: V1收益改善
>1pp→换手/成本拆解+事件归因。
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

BASE = {"pool_size": 30, "lot_size": 100, "initial_capital": 500_000.0}
VARIANTS = [
    ("基线等权", {}),
    ("V1逆波动率加权", {"weight_scheme": "inv_vol"}),
    ("V2 lowvol取30", {"pool_style": "lowvol"}),
    ("V3=V2+V1", {"pool_style": "lowvol", "weight_scheme": "inv_vol"}),
]
WINDOWS = [("全期2019-26.8", "2019-01-01", "2026-08-28"),
           ("OOS2015-18", "2015-01-01", "2018-12-31")]
SEGS = [("牛2019-21", "2019-01-01", "2021-12-31"),
        ("熊震荡2022-24.6", "2022-01-01", "2024-06-30"),
        ("政策牛+BEAR", "2024-07-01", "2026-08-28")]


def load_window(lo, hi, cal):
    meta = load_meta("stock_info_full")
    codes = meta["code"].tolist() if not meta.empty else []
    prices, amounts = {}, {}
    for code in codes:
        try:
            d = load_daily(code, lo, hi)
            if d.empty:
                continue
            d["date"] = pd.to_datetime(d["date"])
            d = d.set_index("date").sort_index()
            cl = pd.to_numeric(d["close"], errors="coerce").dropna()
            amt = pd.to_numeric(d.get("amount", pd.Series(dtype=float)),
                                errors="coerce")
            if len(cl) >= 250:
                prices[code] = cl
                if len(amt) >= 250:
                    amounts[code] = amt
        except Exception:
            pass
    panel = pd.DataFrame(prices).sort_index()
    ap = pd.DataFrame(amounts).sort_index()
    idx_c = load_meta("csi800_index").set_index("date")["close"].sort_index()
    idx_c.index = pd.to_datetime(idx_c.index)
    rebal = [d for d in make_rebal_dates(cal, "biweekly") if lo <= d <= hi]
    return panel, ap, idx_c, rebal


def seg_ann(nav, lo, hi):
    seg = nav[(nav.index >= pd.Timestamp(lo)) & (nav.index <= pd.Timestamp(hi))]
    if len(seg) < 10:
        return float("nan")
    tot = seg.iloc[-1] / seg.iloc[0] - 1
    days = (seg.index[-1] - seg.index[0]).days
    return (1 + tot) ** (365 / max(days, 1)) - 1


def main():
    sh = load_daily("000001", "2014-06-01", "2026-08-28")
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))
    for wn, lo, hi in WINDOWS:
        panel, ap, idx_c, rebal = load_window(lo, hi, cal)
        print(f"\n===== {wn} =====", flush=True)
        for comm in (0.0013, 0.0030):
            for name, ov in VARIANTS:
                cfg = BacktestConfig(**{**DEFAULT_CONFIG.to_dict(), **BASE,
                                        "commission": comm, **ov})
                nav, info = run_backtest(panel, ap, rebal, cfg, idx_c)
                cm = calc_metrics(nav)
                years = (panel.index[-1] - panel.index[0]).days / 365.25
                to = (info["total_commission"] / comm) / years
                segs = [f"{seg_ann(nav, a, b)*100:+.1f}%" for _, a, b in SEGS]
                print(f"  [{comm:.2%}] {name:<16} 年化{cm['年化_float']*100:+.2f}% "
                      f"夏普{cm['夏普_float']:.2f} 回撤{cm['回撤_float']*100:.2f}% "
                      f"换手{to*100:.0f}%/年 | 三段[{', '.join(segs)}]",
                      flush=True)


if __name__ == "__main__":
    main()
