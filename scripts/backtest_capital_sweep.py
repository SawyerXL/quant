"""资金规模画像扫描 (2026-09-27, 用户问"不同资金量的年化/夏普/回撤对比")。

依据 CLAUDE.md 资金口径硬触发(09-02): floor-to-lot 保护是"策略×资金规模"画像,
旧锚点=50万/组回撤-12~-14% / 保护消失点100~200万 / 500万回退回-25%(旧档位)。
本脚本以当前配置(ma200_thresh_shift=-0.03 已实装)重扫: 无约束参照 + 50/100/
150/200/300/500万/组, pool30, lot_size=100, 双周调仓, 无熔断(纯画像)。
输出各资金档的 年化/夏普/回撤/跳票率 + 15%熔断线的穿越档位。
"""
import sys
from pathlib import Path
from dataclasses import replace
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
from loguru import logger
logger.remove()

from data.storage import load_daily, load_meta
from backtest_config import DEFAULT_CONFIG
from backtest_engine import run_backtest, make_rebal_dates, calc_metrics

END_20260924 = "2026-09-24"   # 工件 as_of 冻结(闸门十三)
START = "2019-01-01"
CAPITALS = [500_000, 1_000_000, 1_500_000, 2_000_000, 3_000_000, 5_000_000]


def main():
    sh = load_daily("SH000001", "2014-06-01", END_20260924)
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))
    rebal = [d for d in make_rebal_dates(cal, "biweekly")
             if START <= d <= END_20260924]

    meta = load_meta("stock_info_full")
    codes = meta["code"].tolist() if not meta.empty else []
    prices, amounts = {}, {}
    for code in codes:
        try:
            d = load_daily(code, START, END_20260924)
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
    idx = load_meta("csi800_index")
    idx_c = idx.set_index("date")["close"].sort_index()
    idx_c.index = pd.to_datetime(idx_c.index)
    print(f"面板 {panel.shape[0]}天×{panel.shape[1]}只, 调仓日 {len(rebal)}个")

    def run(cfg, label):
        nav, m = run_backtest(panel, ap, rebal, cfg, idx_c)
        cm = calc_metrics(nav)
        ar = float(str(cm["年化收益率"]).strip("%"))
        sr = float(cm["夏普比率"])
        dd = float(str(cm["最大回撤"]).strip("%"))
        skip = m.get("lot_skips", m.get("skips", 0))
        print(f"{label:<22} 年化{ar:>+7.2f}% 夏普{sr:>6.2f} 回撤{dd:>8.2f}%"
              f" 跳票{skip:>6}" + (" ⚠破15%熔断线" if dd <= -15 else ""))
        return ar, sr, dd

    print(f"\n{'资金/组':<22}{'年化':>9}{'夏普':>7}{'回撤':>9}{'跳票':>7}")
    print("─" * 60)
    print(f"{'无约束(无限可分)':<22} ", end="")
    base = replace(DEFAULT_CONFIG, lot_size=0, initial_capital=CAPITALS[0])
    run(base, "无约束(无限可分)")
    for cap in CAPITALS:
        cfg = replace(DEFAULT_CONFIG, lot_size=100, initial_capital=cap)
        run(cfg, f"lot {cap/10000:.0f}万/组")
    print(f"\nas_of={END_20260924}, pool30, 双周调仓, shift=-0.03, 无熔断纯画像")


if __name__ == "__main__":
    main()
