"""美股指数 × 组合两腿 相关性测量(2026-10-11, 配置裁定前置, 不耗预算)。

口径: 日收益; 对齐=美东 T-1 收盘收益 vs 中国 T 日收益(时区因果对齐) + 同日对齐;
来源=新浪美股指数(.IXIC 纳指/.INX 标普) vs logs/stock_cb_joint_returns.csv
(s=股票腿 cb=CB腿, 2019-01 起) + csi800 库内序列对照。
输出: 全期相关矩阵 + 分年度相关 + beta。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import akshare as ak

sys.path.insert(0, str(Path(__file__).parent.parent))
from data.storage import load_meta


def us_index(code):
    df = ak.index_us_stock_sina(symbol=code)
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date")["close"].astype(float).pct_change().dropna()


def main():
    joint = pd.read_csv("logs/stock_cb_joint_returns.csv", index_col=0, parse_dates=True)
    us = {}
    for code, name in [(".IXIC", "纳指"), (".INX", "标普500")]:
        try:
            us[name] = us_index(code)
        except Exception as e:
            print(f"{name} 拉取失败: {str(e)[:50]}")
    csi = load_meta("csi800_index")
    csi["date"] = pd.to_datetime(csi["date"])
    csi_ret = csi.set_index("date")["close"].pct_change().dropna()

    for name, r in us.items():
        # 对齐: 美东 T-1 → 中国 T
        r_shifted = r.shift(1)
        j = pd.concat([joint["s"], joint["cb"], r_shifted.rename(f"{name}(T-1)"),
                       r.rename(f"{name}(同日)"), csi_ret.rename("csi800")],
                      axis=1, join="inner").dropna()
        print(f"\n=== {name} (n={len(j)}日) ===")
        cols = ["s", "cb", f"{name}(T-1)", f"{name}(同日)", "csi800"]
        print(j[cols].corr().round(3).to_string())
        # beta: 组合两腿对美股
        for leg in ["s", "cb"]:
            b = np.polyfit(j[f"{name}(T-1)"], j[leg], 1)[0]
            print(f"  beta({leg} ← {name} T-1) = {b:.2f}")
        # 分年度相关(T-1 对齐)
        j["yr"] = j.index.year
        for yr in sorted(j["yr"].unique()):
            w = j[j["yr"] == yr]
            if len(w) < 100:
                continue
            print(f"  {yr}: corr(s,{name})={w['s'].corr(w[f'{name}(T-1)']):+.3f} "
                  f"corr(cb,{name})={w['cb'].corr(w[f'{name}(T-1)']):+.3f}")


if __name__ == "__main__":
    main()
