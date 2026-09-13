"""
价格侧断裂隔离 (2026-09-13, 复核阻塞项处理) —
147 只温和 qfq 复权断裂票(close 相邻比值>1.25 或 <0.8 且 ≥2 次)的
跳变日 close 置 NaN: 收益率计算跳过该日, 组合净值不再混入虚假
单日 ±25~50% 收益(pool30 单票 3.3% 权重 × 50% 假收益 = 组合 1.7pp,
与待决优势同数量级)。
处置根因: qfq 复权断裂(与 12 只极端振荡同类, 幅度更温和);
>50% 档已由 9/7 修复+黑名单覆盖, 本脚本补 1.25~50% 档。
用法: python scripts/quarantine_price_breaks.py
"""
import sys, json, os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd

DAILY = Path("/data/quant_data_store/daily")


def main():
    breaks = {x["code"] for x in json.load(
        open("logs/mild_qfq_breaks.json"))}
    n_files, n_days = 0, 0
    for c in sorted(breaks):
        f = DAILY / "2026" / f"{c}.parquet"
        if not f.exists():
            continue
        d = pd.read_parquet(f).sort_values("date").reset_index(drop=True)
        cl = pd.to_numeric(d["close"], errors="coerce")
        prev = None
        hit = False
        for i, r in d.iterrows():
            c2 = pd.to_numeric(r["close"], errors="coerce")
            if prev is not None and pd.notna(c2) and prev > 0:
                rr = c2 / prev
                if rr > 1.25 or rr < 0.8:
                    d.loc[i, "close"] = float("nan")
                    hit = True
                    n_days += 1
            if pd.notna(c2):
                prev = c2
        if hit:
            tmp = f.with_suffix(".parquet.tmp")
            d.to_parquet(tmp, index=False)
            os.replace(tmp, f)
            n_files += 1
    print(f"价格侧隔离: {n_files} 只 / {n_days} 个跳变日 close→NaN")


if __name__ == "__main__":
    main()
