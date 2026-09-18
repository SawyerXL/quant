"""主回测分析（2026-09-18，事后分析性质，登记见账本"主回测登记"节）。

输入=现成归一库曲线（09-16 晚 s1 落盘）+ 官方000906价格指数：
  主指标: 配对差分年化夏普 d_t=ln(A3/A3前)−ln(B/B前)（B=官方指数）
  SE: circular block bootstrap, block=21交易日, B=2000, seed 固定
  判读带: σ=差分夏普/SE_boot, ≥0.35=候选(描述) / <0.35=不可分辨
  两档全跑: 0.13%(_013 曲线) / 0.30%(无后缀曲线)
  叠加层分解: A3−A2c / A2c−A1c / A1c−A0 同配对差分法
所有结论前缀"截断宇宙 415/424 下"。只描述，不验证。
用法: python scripts/main_backtest_analysis.py > logs/main_backtest_20260918.log 2>&1
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd

from data.storage import load_meta

BLOCK = 21          # 登记锁死: 与TE月频约定一致
B = 2000            # 登记锁死
SEED = 20260918     # 登记锁死
TIERS = [("0.13%档", "_013"), ("0.30%档", "")]
ARMS = {"A3": "S1_A3_完整栈现网", "A2c": "S1_A2c_pool30+五档纯",
        "A1c": "S1_A1c_pool30买持纯", "A0": "S1_A0_pit800全关"}


def circ_block_boot(d: np.ndarray, n: int = B, b: int = BLOCK,
                    seed: int = SEED) -> list:
    """circular block bootstrap 重抽 d 序列(登记锁死实现)"""
    rng = np.random.default_rng(seed)
    out = []
    L = len(d)
    for _ in range(n):
        i = rng.integers(0, L, size=int(np.ceil(L / b)))
        out.append(np.concatenate([d[(s + np.arange(b)) % L] for s in i])[:L])
    return out


def ann_sharpe_of_diff(d: np.ndarray):
    m, s = d.mean(), d.std(ddof=1)
    return m / s * np.sqrt(252) if s > 0 else 0.0


def main():
    idx = load_meta("csi800_index").set_index("date")["close"].sort_index()
    idx.index = pd.to_datetime(idx.index)
    for tier_name, suf in TIERS:
        print(f"===== {tier_name} =====")
        curves = {}
        for k, n in ARMS.items():
            f = Path(f"logs/s1_curve_{n}{suf}.csv")
            if f.exists():
                s = pd.read_csv(f, index_col=0, parse_dates=True).iloc[:, 0]
                curves[k] = s
            else:
                print(f"  {k}: 曲线缺失({f.name})")
        if "A3" not in curves:
            print("  A3 缺失, 跳过")
            continue
        a3 = curves["A3"]
        common = a3.index.intersection(idx.index)
        a3c, bc = a3.loc[common], idx.loc[common]
        d = np.log(a3c / a3c.shift(1)).dropna().values - \
            np.log(bc / bc.shift(1)).dropna().values

        sh = ann_sharpe_of_diff(d)
        boots = [ann_sharpe_of_diff(x) for x in circ_block_boot(d)]
        se = float(np.std(boots))
        sigma = sh / se if se > 0 else 0.0
        print(f"  [防伪] SE_boot 全精度 = {se:.6f} (B={B}, block={BLOCK}, "
              f"seed={SEED}, n={len(d)}日)")
        ann_a = (a3c.iloc[-1] / a3c.iloc[0]) ** (252 / len(a3c)) - 1
        ann_b = (bc.iloc[-1] / bc.iloc[0]) ** (252 / len(bc)) - 1
        cdar = float((1 - (a3c / a3c.cummax())).max())
        verdict = ("候选(描述强度)" if sigma >= 0.35
                   else "与0不可分辨")
        print(f"  截断宇宙 415/424 下: 差分夏普 {sh:+.2f} ± {se:.2f} "
              f"(SE_boot) → σ={sigma:+.2f} → {verdict}")
        print(f"  年化: A3 {ann_a*100:+.2f}% vs 官方指数 {ann_b*100:+.2f}% "
              f"→ Δ {100*(ann_a-ann_b):+.2f}pp | CDaR(A3) {cdar*100:.1f}%")
        # 叠加层分解 —— 2026-09-18 更正: 层间判读归锁定 TE 方法
        # (叠加层复审视唯一方法, 09-16 锁"不得重选"), 不再用主指标
        # bootstrap(块bootstrap层σ已撤回, 见账本)
        from overlay_review_te import te_stats as layer_te
        years = (a3c.index[-1] - a3c.index[0]).days / 365.25
        for lo, hi, label in (("A0", "A1c", "池层"),
                              ("A1c", "A2c", "择时层"),
                              ("A2c", "A3", "组件层")):
            if lo not in curves or hi not in curves:
                print(f"  {label}: 缺曲线, 跳过")
                continue
            lo_s, hi_s = curves[lo], curves[hi]
            te = layer_te(lo_s, hi_s, years)
            ann_hi = (hi_s.iloc[-1] / hi_s.iloc[0]) ** (252 / len(hi_s)) - 1
            ann_lo = (lo_s.iloc[-1] / lo_s.iloc[0]) ** (252 / len(lo_s)) - 1
            eff = (ann_hi - ann_lo) * 100
            sig = eff / te["SE_pp"]
            print(f"  {label} (hi−lo, 锁定TE法): 效应{eff:+.2f}pp "
                  f"±{te['SE_pp']:.2f}pp → σ={sig:+.2f} → "
                  f"{'过线(≥0.35)' if abs(sig) >= 0.35 else '与0不可分辨'}")


if __name__ == "__main__":
    main()
