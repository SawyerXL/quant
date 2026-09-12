"""
比值审计 (2026-09-12 用户 apply 后第三审计) —
9/7 的比值检查对"量额同倍缩放"不敏感, 但对"量额不同倍缩放"高度敏感:
三条路径转换系数不同(东财 手→万股÷100 + 元→万元÷1e4; 新浪/东财额
÷1e4), 归一代码若把量/额配错源, 比值审计立刻爆——唯一能抓归一自身
实现 bug 的审计。口径: amount(万元)/volume(万股) ∈ [0.5, 2]×收盘价。
用法: python scripts/unit_ratio_audit.py [--sample 500] [--dates 5]
"""
import sys, argparse, random
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd

from data.storage import load_daily, load_meta
from backtest_return_attribution import load_blacklist

DAILY_DIR = Path("/data/quant_data_store/daily")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=500)
    ap.add_argument("--dates", type=int, default=5)
    args = ap.parse_args()

    sh = load_daily("SH000001", "2026-01-01", "2026-12-31")
    cal = sorted(sh["date"].astype(str).str[:10].tolist())
    dates = cal[-args.dates:]
    meta = load_meta("stock_info_full")
    bl = load_blacklist()
    codes = [str(c).zfill(6) for c in meta["code"].tolist()
             if str(c).zfill(6) not in bl]
    random.seed(13)
    sample = random.sample(codes, args.sample)

    bad, checked = [], 0
    for c in sample:
        d = load_daily(c, dates[0], dates[-1])
        if d.empty:
            continue
        d["amount"] = pd.to_numeric(d["amount"], errors="coerce")
        d["volume"] = pd.to_numeric(d["volume"], errors="coerce")
        d["close"] = pd.to_numeric(d["close"], errors="coerce")
        for _, r in d.iterrows():
            a, v, p = r["amount"], r["volume"], r["close"]
            if not (a > 0 and v > 0 and p > 0):
                continue
            ratio = a / v
            checked += 1
            if not (0.5 * p <= ratio <= 2.0 * p):
                bad.append((c, str(r["date"])[:10], ratio, p))
    print(f"抽查 {checked} 个(票,日)对, 比值越界 {len(bad)} 个")
    for c, dt, ratio, p in bad[:10]:
        print(f"  {c} {dt}: 额/量={ratio:.1f} vs 收盘{p:.1f} ✗")
    if bad:
        print("🔴 比值审计不通过 — 量额转换系数疑配错")
        sys.exit(1)
    print("比值审计通过 ✓")


if __name__ == "__main__":
    main()
