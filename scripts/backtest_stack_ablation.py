"""
整栈消融（2026-09-06，专家授权的唯一破冻实验——验证"架构"而非"零件"）。

问题: 6项采纳全是边际测试(单项<0.35σ), 从没问过"完整栈 vs 最小栈
差多少"。若差异不显著→剥3~4组件换运维+换手双降; 显著→整栈首次拿到
集体证据。同时补上 MA10-4d 开/关 的直接检验(exit_days网格只测了4/6/8,
机制贡献93%换手却从未被开关对照过——7/8单路径消融在噪声地板发现
之前, 按现行§0判读线不达标)。
四臂×10路径协议:
  A 完整栈(现网): vol5%过滤 + MA10-4d + TP30/60
  B 最小栈: 仅五档择时 + TOP30等权(全关)
  C 完整栈-MA10: 过滤+TP开, MA10关
  D 最小栈+MA10: 过滤+TP关, MA10开
口径: pool30×50万lot×降档3%; 输出均值/极差/摊平/回撤/换手。
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
from backtest_config import BacktestConfig, DEFAULT_CONFIG
from backtest_engine import run_backtest, make_rebal_dates, calc_metrics

START, END = "2019-01-01", "2026-08-28"
BASE = {"pool_size": 30, "lot_size": 100, "initial_capital": 500_000.0}
ARMS = [
    ("A完整栈(现网)", {}),
    ("B最小栈", {"max_vol20": 999.0, "enable_ma10_exit": False,
                 "enable_take_profit": False}),
    ("C完整栈-MA10", {"enable_ma10_exit": False}),
    ("D最小栈+MA10", {"max_vol20": 999.0, "enable_take_profit": False}),
]


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
    ic = load_meta("csi800_index").set_index("date")["close"].sort_index()
    ic.index = pd.to_datetime(ic.index)
    sh = load_daily("000001", "2014-06-01", END)
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))
    base = [d for d in make_rebal_dates(cal, "biweekly") if START <= d <= END]
    idx = {d: i for i, d in enumerate(cal)}

    def path(off):
        shifted = [cal[idx.get(d, 0) + off] for d in base
                   if idx.get(d, 0) + off < len(cal)]
        return [d for d in shifted if START <= d <= END]

    years = (panel.index[-1] - panel.index[0]).days / 365.25
    for name, ov in ARMS:
        anns, rets, tos, dds = [], [], [], []
        for off in range(10):
            cfg = BacktestConfig(**{**DEFAULT_CONFIG.to_dict(), **BASE, **ov})
            nav, info = run_backtest(panel, ap, path(off), cfg, ic)
            cm = calc_metrics(nav)
            anns.append(float(cm["年化_float"]))
            dds.append(float(cm["回撤_float"]))
            rets.append(nav.pct_change().dropna())
            tos.append(info["total_commission"] / cfg.commission / years)
        j = pd.concat(rets, axis=1).dropna()
        ens = (1 + j.mean(axis=1)).cumprod()
        ecm = calc_metrics(ens)
        print(f"{name:<16}: 路径均值{np.mean(anns)*100:+.2f}% "
              f"极差{(max(anns)-min(anns))*100:.2f}pp | "
              f"摊平{ecm['年化_float']*100:+.2f}% 夏普{ecm['夏普_float']:.2f} "
              f"回撤{ecm['回撤_float']*100:.2f}% | 换手{np.mean(tos)*100:.0f}%/年",
              flush=True)


if __name__ == "__main__":
    main()
