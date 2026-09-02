"""
调仓时点运气检验（2026-09-01，专家路线图第2项——"可能改写+6.92%本身，先跑"）。

问题: 固定调仓日(15日+月末)的策略, 年化里有"抽签成分"——换一组调仓日
可能差±1~2pp, 而这不是alpha。+6.92%从未被度量过这项。

方法(Newfound Research tranching 的反向应用): 把调仓锚点平移0~9个交易日,
跑10条路径, 看离散度。
判读(专家): 年化极差<0.5pp=运气中性不用改; >2pp=+6.92%本身待修正,
且之前所有A/B判决都要带此警示重看。
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
    base_rebal = [d for d in make_rebal_dates(cal, "biweekly") if START <= d <= END]

    print(f"\n{'偏移':<6}{'年化':>9}{'夏普':>8}{'回撤':>9}", flush=True)
    results = []
    for off in range(10):
        # 锚点平移: 每个调仓日顺延 off 个交易日
        idx_map = {d: i for i, d in enumerate(cal)}
        shifted = []
        for d in base_rebal:
            i = idx_map.get(d, 0) + off
            if i < len(cal):
                shifted.append(cal[i])
        rebal = [d for d in shifted if START <= d <= END]
        nav, _ = run_backtest(panel, ap, rebal, DEFAULT_CONFIG, ic)
        cm = calc_metrics(nav)
        ar = float(str(cm["年化收益率"]).strip("%"))
        sr = float(cm["夏普比率"])
        dd = float(str(cm["最大回撤"]).strip("%"))
        results.append((ar, sr, dd))
        print(f"+{off}日  {ar:>+8.2f}%{sr:>8.2f}{dd:>8.2f}%", flush=True)

    arr = [r[0] for r in results]
    print(f"\n离散度: 最高{max(arr):+.2f}% 最低{min(arr):+.2f}% 极差{max(arr)-min(arr):.2f}pp "
          f"均值{sum(arr)/len(arr):+.2f}% 标准差{__import__('numpy').std(arr):.2f}pp")
    verdict = ("运气中性(<0.5pp), 无需tranching" if max(arr)-min(arr) < 0.5
               else ("待修正(>2pp): +6.92%含显著抽签成分" if max(arr)-min(arr) > 2
                     else "中间地带(0.5~2pp): 建议tranching消除时点方差"))
    print(f"\n判读: {verdict}", flush=True)


if __name__ == "__main__":
    main()
