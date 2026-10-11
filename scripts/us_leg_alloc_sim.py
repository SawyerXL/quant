"""美股腿配比模拟(2026-10-12, 配置裁定辅助, 不耗预算)。

口径: 日收益 2019-01→2026-10(中国交易日); 组合=股票腿(s)×CB腿(cb) 基准
40/60, 美股腿权重网格 0/10/15/20%(其余按 40/60 比例缩放); 美股腿两个
代理: ①纯纳指(美东 T-1 对齐) ②港美科技 60/40 混合代理=0.6×恒生科技+
0.4×纳指(港股通 60/40 结构)。未计: 汇率波动/交易成本/车辆溢价。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import akshare as ak

sys.path.insert(0, str(Path(__file__).parent.parent))


def load_us():
    ix = ak.index_us_stock_sina(symbol=".IXIC")
    ix["date"] = pd.to_datetime(ix["date"])
    us = ix.set_index("date")["close"].pct_change().shift(1).dropna()
    hk = None
    try:
        h = ak.stock_hk_index_daily_sina(symbol="HSTECH")
        h["date"] = pd.to_datetime(h["date"])
        hk = h.set_index("date")["close"].pct_change().dropna()
    except Exception as e:
        print(f"恒生科技拉取失败(混合代理退化为纯纳指): {str(e)[:50]}")
    return us, hk


def main():
    joint = pd.read_csv("logs/stock_cb_joint_returns.csv", index_col=0, parse_dates=True)
    us, hk = load_us()
    legs = {"纯纳指": us}
    if hk is not None:
        mix = pd.concat([hk.rename("hk"), us.rename("us")], axis=1, join="inner").dropna()
        legs["港美科技60/40代理"] = 0.6 * mix["hk"] + 0.4 * mix["us"]

    for name, ul in legs.items():
        j = pd.concat([joint["s"], joint["cb"], ul.rename("usleg")],
                      axis=1, join="inner").dropna()
        years = (j.index[-1] - j.index[0]).days / 365.25
        print(f"\n=== 美股腿代理: {name} (n={len(j)}日, {years:.1f}年) ===")
        print(f"{'美股权重':<8}{'年化':>8}{'夏普':>8}{'回撤':>10}")
        for w in [0.0, 0.10, 0.15, 0.20]:
            ws = 0.40 * (1 - w)
            wc = 0.60 * (1 - w)
            r = ws * j["s"] + wc * j["cb"] + w * j["usleg"]
            nav = (1 + r).cumprod()
            ann = nav.iloc[-1] ** (1 / years) - 1
            sharpe = r.mean() / r.std() * np.sqrt(252) if r.std() > 0 else 0
            dd = (nav / nav.cummax() - 1).min()
            print(f"{w*100:>6}%{ann*100:>+8.2f}%{sharpe:>8.2f}{dd*100:>+9.1f}%")


if __name__ == "__main__":
    main()
