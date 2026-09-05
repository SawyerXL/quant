"""
熊市档信号 vs 逆信号持有（2026-09-05，用户问题: 周一降仓与市场研判是否一致, 该听谁的）。

口径: CSI800/MA200-0.03<0.95(降档3%后的熊市档触发线)首次进入日,
之后20/60日指数收益 = '逆信号持有'结果; 0 = '听策略清仓'结果。
近关口<2%子样本 = 当前情境(距上方整数关口近)的直接镜像。
判读(2026-09-05): 近关口子样本20日-0.5%/60日-3.6%(胜率33%), 听策略。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd
from loguru import logger
logger.remove()

from data.storage import load_meta


def main():
    ic = load_meta("csi800_index")
    ic["date"] = pd.to_datetime(ic["date"])
    cl = ic.set_index("date")["close"].sort_index()
    ratio = cl / cl.rolling(200).mean()
    sig = (ratio - 0.03 < 0.95).astype(bool)
    enter = sig & ~sig.shift(1, fill_value=False)
    dates = cl.index[enter]
    print(f"历史熊市档触发(降档3%口径): {len(dates)}次")
    for h in (20, 60):
        fwd = [cl[cl.index > dt].iloc[h-1] / cl.loc[dt] - 1
               for dt in dates if len(cl[cl.index > dt]) >= h]
        fwd = np.array(fwd)
        print(f"{h}日: 逆信号持有均值{fwd.mean()*100:+.1f}% "
              f"胜率{(fwd>0).mean()*100:.0f}% 最差{fwd.min()*100:.1f}% "
              f"最好{fwd.max()*100:.1f}% (n={len(fwd)})")
    near = []
    for dt in dates:
        tgt = np.ceil(cl.loc[dt] / 100) * 100
        if tgt / cl.loc[dt] - 1 < 0.02:
            for h in (20, 60):
                fut = cl[cl.index > dt]
                if len(fut) >= h:
                    near.append((h, fut.iloc[h-1] / cl.loc[dt] - 1))
    nd = pd.DataFrame(near, columns=["h", "ret"])
    for h in (20, 60):
        sub = nd[nd.h == h]["ret"]
        print(f"[近关口<2%子样本] {h}日: 均值{sub.mean()*100:+.1f}% "
              f"胜率{(sub>0).mean()*100:.0f}% (n={len(sub)})")


if __name__ == "__main__":
    main()
