"""
全10路径摊平验证（2026-09-01，backtest_tranching.py 的完整版）。

why: 5组摊平(+12.12%)已优于现网(+6.92%), 但5组只用了偏移{0,2,4,6,8}。
     部署前需要全10路径摊平的精确期望值, 并分解2019-2025 vs 2026,
     确认摊平收益不是2026单年运气(时点检验纪律: 样本脆弱性)。
口径: 10组各10%资金, 组g在路径g(偏移g日)上调仓, 日收益等权合成。
"""
import sys
from pathlib import Path
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

    rets, ann = [], []
    for off in range(10):
        nav, _ = run_backtest(panel, ap, path(off), DEFAULT_CONFIG, ic)
        cm = calc_metrics(nav)
        ann.append(float(cm["年化_float"]))
        rets.append(nav.pct_change().dropna())
        print(f"路径{off}: 年化{ann[-1]*100:+.2f}% 末NAV{nav.iloc[-1]:.3f}",
              flush=True)

    j = pd.concat(rets, axis=1).dropna()
    ens = (1 + j.mean(axis=1)).cumprod()
    cm = calc_metrics(ens)
    print(f"\n=== 10路径摊平(各10%资金) ===", flush=True)
    print(f"年化 {cm['年化收益率']}  夏普 {cm['夏普比率']}  "
          f"回撤 {cm['最大回撤']}  波动 {cm['年化波动率']}", flush=True)

    # 2026单年贡献分解: 2019-2025子区间 vs 2026至今
    seg = ens[ens.index < pd.Timestamp("2026-01-01")]
    sub = seg.iloc[-1] / seg.iloc[0] - 1
    days = (seg.index[-1] - seg.index[0]).days
    ann_sub = (1 + sub) ** (365 / max(days, 1)) - 1
    y2026 = ens.iloc[-1] / seg.iloc[-1] - 1
    print(f"2019-2025子区间: 总{sub*100:+.1f}% 年化{ann_sub*100:+.2f}%  "
          f"| 2026至今: {y2026*100:+.1f}%", flush=True)

    # 单路径对照: 现网口径
    nav0, _ = run_backtest(panel, ap, path(0), DEFAULT_CONFIG, ic)
    cm0 = calc_metrics(nav0)
    seg0 = nav0[nav0.index < pd.Timestamp("2026-01-01")]
    sub0 = seg0.iloc[-1] / seg0.iloc[0] - 1
    days0 = (seg0.index[-1] - seg0.index[0]).days
    ann0_sub = (1 + sub0) ** (365 / max(days0, 1)) - 1
    y26_0 = nav0.iloc[-1] / seg0.iloc[-1] - 1
    print(f"现网单组(偏移0): 全期年化{cm0['年化收益率']} | "
          f"2019-2025年化{ann0_sub*100:+.2f}% | 2026至今{y26_0*100:+.1f}%",
          flush=True)

    print(f"\n结论: 摊平期望值 = 10路径算术均值年化 "
          f"{np.mean(ann)*100:+.2f}% (极差{(max(ann)-min(ann))*100:.2f}pp)",
          flush=True)


if __name__ == "__main__":
    main()
