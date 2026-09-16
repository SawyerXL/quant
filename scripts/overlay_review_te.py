"""
同宇宙叠加层复审的 σ 计算（2026-09-16）——s1 曲线落盘后按锁定方法
（登记文档 §1：TE=月频非重叠对数差分样本σ×√12，SE=TE/√T，σ 只报点值
效应±SE）计算两对差分：择时=A2c−A1c、组件=A3−A2c。不含 A0
（用户裁定：A0 是被污染对象本身）。判据：自身 TE 的 σ≥0.35 → 候选
资格（非确认）；效应带四元组（臂对,成本档,库状态,引擎版本）。
用法: python scripts/overlay_review_te.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd


def te_stats(ens_base, ens_test, years):
    """锁定方法唯一实现（与 single_var_harness.te_stats 同源）"""
    d = (np.log(ens_test) - np.log(ens_base)).dropna()
    m = d.resample("ME").last().dropna()
    if len(m) < 24:
        raise ValueError(f"月频差分样本 n={len(m)} < 24")
    te = float(m.std(ddof=1)) * np.sqrt(12.0)
    return {"TE_pct": te * 100.0, "SE_pp": te / np.sqrt(years) * 100.0,
            "n_months": int(len(m)), "years": years}


def main():
    names = {"S1_A1c_pool30买持纯", "S1_A2c_pool30+五档纯", "S1_A3_完整栈现网"}
    curves = {}
    for n in names:
        f = Path(f"logs/s1_curve_{n}.csv")
        if not f.exists():
            print(f"缺曲线: {f}")
            return
        c = pd.read_csv(f, index_col=0, parse_dates=True)
        curves[n] = c.iloc[:, 0].sort_index()
    years = 7.7  # 主窗 2019-01-01~2026-08-28

    pairs = [("择时 A2c−A1c", "S1_A2c_pool30+五档纯", "S1_A1c_pool30买持纯"),
             ("组件 A3−A2c", "S1_A3_完整栈现网", "S1_A2c_pool30+五档纯")]
    for label, hi, lo in pairs:
        common = curves[hi].index.intersection(curves[lo].index)
        a, b = curves[hi].loc[common], curves[lo].loc[common]
        effect = float(((1 + a.pct_change()).prod() ** (252 / len(a)) - 1)
                       - ((1 + b.pct_change()).prod() ** (252 / len(b)) - 1))
        ts = te_stats(b, a, years)
        effect_pp = effect * 100.0
        sigma = effect_pp / ts["SE_pp"]
        verdict = "候选资格(非确认)" if sigma >= 0.35 else "不通过"
        print(f"{label}: 效应 {effect*100:+.2f}pp ± {ts['SE_pp']:.2f}pp "
              f"(95%CI [{effect*100-1.96*ts['SE_pp']:+.2f}, "
              f"{effect*100+1.96*ts['SE_pp']:+.2f}])")
        print(f"  TE={ts['TE_pct']:.1f}% (n={ts['n_months']}月) SE={ts['SE_pp']:.2f}pp "
              f"σ={sigma:+.2f}(点值) MDE={0.35*ts['SE_pp']:.2f}pp → {verdict}")
        print(f"  四元组: (臂对={label}, 成本档=0.13%, 库状态=归一后库(9/12 apply, 稳定期2/3天), 引擎=patch重上后)")


if __name__ == "__main__":
    main()
