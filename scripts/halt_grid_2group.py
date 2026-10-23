"""live {2,7} 两组 40/60 的 15% 熔断网格（2026-10-23）。

原熔断网格基于单组合口径（`three_piece` 单路径）；本脚本用 live 结构
（两组等权股票腿 + CB nav_snapdelay_par）复核 -10/-15/-20/-25 阈值。
halt 语义：触发后停止月度回目标权重（持仓自然漂移），回撤恢复到 tau+2pp 解除。
只读；不改参数/信号链。
"""
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from three_piece_40_60 import combo, metrics_ret

OUT = ROOT / "logs" / "halt_grid_2group_20261023.json"


def main():
    stock = pd.read_csv(ROOT / "logs" / "stock_leg_nav_20w_avg.csv",
                        index_col=0, parse_dates=True)["nav"]
    cb = pd.read_csv(ROOT / "logs" / "cb_sched_compare_nav.csv",
                     index_col=0, parse_dates=True)["nav_snapdelay_par"]
    legs = pd.concat([stock.pct_change().rename("s"),
                      cb.pct_change().rename("cb")], axis=1,
                     join="inner").dropna()
    nav0, _, _ = combo(legs)
    out = {"no_halt": metrics_ret(nav0.pct_change().fillna(0)), "grid": {}}
    for tau in (-0.10, -0.15, -0.20, -0.25):
        nav, t, d = combo(legs, halt=(tau, 0.02))
        out["grid"][f"{tau}"] = {
            "metrics": metrics_ret(nav.pct_change().fillna(0)),
            "trig": int(t), "days_halted": int(d)}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=1))
    print("->", OUT)


if __name__ == "__main__":
    main()
