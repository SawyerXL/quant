"""
敞口归因三输出（2026-09-01，熔断重校前置——"回撤腰斩"机制排查）。

假设(用户事前登记): floor-to-lot是隐藏的顺周期去杠杆器——净值下跌→
每只分配资金变少→跳票率升高→实际敞口自动下降, 跌得越深敞口降得越多。
若成立, 该保护不随资金规模扩展: 500万/组下跳票趋零, 风险画像回到无约束。

三输出:
1. 有效敞口/目标敞口时间序列: 回撤期间是否系统性走低
2. 跳票率 vs 净值相关: 顺周期假设的直接检验
3. 现金去向: (目标-实际)敞口缺口 = cash_yield踏空成本量化

规模对照: pool30/60 × {50万, 500万} × offset{0,7} 共8跑。
"""
import sys
from pathlib import Path
from dataclasses import replace
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd
from loguru import logger
logger.remove()

from data.storage import load_daily, load_meta
from backtest_config import DEFAULT_CONFIG
from backtest_engine import run_backtest, make_rebal_dates, calc_metrics

START, END = "2019-01-01", "2026-08-28"
OUT = Path(__file__).parent.parent / "logs" / "exposure_attr"


def main():
    OUT.mkdir(exist_ok=True)
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
    print(f"Panel: {len(prices)}只, {panel.shape[0]}天", flush=True)

    ic = load_meta("csi800_index")
    ic = ic.set_index("date")["close"].sort_index()
    ic.index = pd.to_datetime(ic.index)
    sh = load_daily("000001", "2014-06-01", END)
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))
    base = [d for d in make_rebal_dates(cal, "biweekly") if START <= d <= END]
    idx = {d: i for i, d in enumerate(cal)}

    def path(off):
        shifted = [cal[idx.get(d, 0) + off] for d in base
                   if idx.get(d, 0) + off < len(cal)]
        return [d for d in shifted if START <= d <= END]

    rows = []
    for pool in (60, 30):
        for cap in (500_000.0, 5_000_000.0):
            for off in (0, 7):
                cfg = replace(DEFAULT_CONFIG, pool_size=pool, lot_size=100,
                              initial_capital=cap, diag_exposure=True)
                nav, info = run_backtest(panel, ap, path(off), cfg, ic)
                cm = calc_metrics(nav)
                exp = pd.DataFrame(info["exposure_ts"],
                                   columns=["date", "actual", "target", "nav"])
                exp["date"] = pd.to_datetime(exp["date"])
                exp = exp.set_index("date")
                exp.to_parquet(OUT / f"p{pool}_c{int(cap/1e4)}w_off{off}.parquet")
                sk = pd.DataFrame(info["skip_ts"], columns=["date", "skips"])
                sk["date"] = pd.to_datetime(sk["date"])
                sk = sk.set_index("date")
                sk.to_parquet(OUT / f"skips_p{pool}_c{int(cap/1e4)}w_off{off}.parquet")
                # 敞口缺口: 平均(目标-实际) = 现金拖累的仓位口径
                gap = float((exp["target"] - exp["actual"]).mean())
                # 回撤状态下的敞口比 vs 正常状态
                hwm = exp["nav"].cummax()
                dd = exp["nav"] / hwm - 1
                deep = dd < -0.10
                r_deep = float((exp.loc[deep, "actual"] /
                                exp.loc[deep, "target"]).mean()) \
                    if deep.any() else float("nan")
                r_norm = float((exp.loc[~deep, "actual"] /
                                exp.loc[~deep, "target"]).mean())
                rows.append({
                    "pool": pool, "cap万": int(cap / 1e4), "off": off,
                    "年化": float(cm["年化_float"]) * 100,
                    "回撤": float(cm["回撤_float"]) * 100,
                    "跳票": info["lot_skips"],
                    "平均敞口缺口": gap * 100,
                    "深回撤敞口比": r_deep * 100,
                    "正常敞口比": r_norm * 100,
                })
                print(f"p{pool} {int(cap/1e4)}万 off{off}: 年化"
                      f"{rows[-1]['年化']:+.2f}% 回撤{rows[-1]['回撤']:.1f}% "
                      f"跳票{rows[-1]['跳票']} 缺口{rows[-1]['平均敞口缺口']:.1f}% "
                      f"深回撤敞口比{rows[-1]['深回撤敞口比']:.0f}% "
                      f"正常{rows[-1]['正常敞口比']:.0f}%", flush=True)

    res = pd.DataFrame(rows)
    res.to_csv(OUT / "summary.csv", index=False)
    print("\n=== 汇总 ===", flush=True)
    print(res.to_string(index=False), flush=True)


if __name__ == "__main__":
    main()
