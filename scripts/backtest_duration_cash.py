"""
方向一: 熊市档现金升级为国债久期（2026-09-01，预算1个）。

Put对冲判死的镜像答案: 同样的MA200择时低仓位档, 把"付保费的保险"换成
"收保费的保险"(国债票息+熊市宽松债牛)。复用择时, 不挖新信号。

事前登记预测(专家): 全期年化+0.5~1.2pp, 回撤持平或略浅。
生死关=股债双杀窗口(2013钱荒/2016债灾): 额外回撤<2pp才可采纳;
久期档位(全30年 vs 半10年)网格须平台。

变体: 现金2%(基线) / 全仓30年(511260) / 全仓10年(511010) / 半30年半现金
窗口: 全期2019-2026.8 + 近段2022-2026.8
双杀对照: 511010在2013-06/2016-12债灾窗口的最大回撤(独立计算)
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
from loguru import logger
logger.remove()

from data.storage import load_daily, load_meta
from backtest_config import DEFAULT_CONFIG
from backtest_engine import run_backtest, make_rebal_dates, calc_metrics

START, END = "2019-01-01", "2026-08-28"


def bond_ret(code):
    import akshare as ak
    d = ak.fund_etf_hist_sina(symbol=("sh" if code.startswith("5") else "sz") + code)
    d["date"] = pd.to_datetime(d["date"])
    s = d.set_index("date")["close"].astype(float)
    return s.pct_change().dropna()


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

    b30 = bond_ret("511260")
    b10 = bond_ret("511010")

    variants = [
        ("现金2%(基线)", None),
        ("全仓30年", b30),
        ("全仓10年", b10),
        ("半30年半现金", (b30 * 0.5)),
    ]

    for wname, lo, hi in [("全期2019-2026.8", START, END), ("近段2022-2026.8", "2022-01-01", END)]:
        p = panel[(panel.index >= lo) & (panel.index <= hi)]
        a = ap[(ap.index >= lo) & (ap.index <= hi)]
        rebal = [d for d in make_rebal_dates(cal, "biweekly") if lo <= d <= hi]
        print(f"\n=== {wname} ===", flush=True)
        print(f"{'配置':<16}{'年化':>9}{'夏普':>8}{'回撤':>9}", flush=True)
        for name, br in variants:
            nav, _ = run_backtest(p, a, rebal, DEFAULT_CONFIG, ic, cash_asset_ret=br)
            cm = calc_metrics(nav)
            ar = float(str(cm["年化收益率"]).strip("%"))
            sr = float(cm["夏普比率"])
            dd = float(str(cm["最大回撤"]).strip("%"))
            print(f"{name:<16}{ar:>+8.2f}%{sr:>8.2f}{dd:>8.2f}%", flush=True)

    # 双杀对照: 债灾窗口的国债回撤
    print("\n=== 双杀对照(独立计算) ===", flush=True)
    for lo, hi, label in [("2013-06-01", "2013-07-31", "2013钱荒"),
                          ("2016-11-01", "2017-01-31", "2016债灾")]:
        b = b10[(b10.index >= lo) & (b10.index <= hi)]
        if len(b) < 5:
            print(f"  {label}: 数据不足")
            continue
        nav = (1 + b).cumprod()
        dd = (nav / nav.cummax() - 1).min()
        print(f"  {label}: 511010期间最大回撤 {dd*100:.1f}% (累计{(nav.iloc[-1]-1)*100:+.1f}%)")


if __name__ == "__main__":
    main()
