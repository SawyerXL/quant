"""
锚模拟校验 (2026-09-09, 归一 apply 的闸门) —
按 logs/unit_norm_plan.json 的计划(含保护日)模拟归一, 对照
tests/unit_ground_truth.json: 15 组配对必须全部落在万元窗口
(库内模拟值/MCP真值 ∈ [0.5, 2]), 任一不通过 → 不允许 apply。
用法: python scripts/verify_norm_plan_against_anchor.py
"""
import sys, json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd

from data.storage import load_daily

ANCHOR = Path(__file__).parent.parent / "tests" / "unit_ground_truth.json"
PLAN = Path(__file__).parent.parent / "logs" / "unit_norm_plan.json"


def main():
    anchor = json.load(open(ANCHOR))
    plan = json.load(open(PLAN))
    # 计划按 code+year 索引
    by_key = {}
    for p in plan:
        code = p["file"].split("/")[-1].replace(".parquet", "")
        year = p["file"].split("/")[-2]
        by_key[(code, year)] = p

    passed, failed = 0, []
    for code, dates in anchor["stocks"].items():
        d_all = load_daily(code, "2026-01-01", "2026-08-28")
        if d_all.empty:
            for d in dates:
                failed.append((code, d, "库内无数据"))
            continue
        d_all = d_all.sort_values("date").reset_index(drop=True)
        p = by_key.get((code, "2026"))
        fix_ranges = p["fix"] if p else []
        protect = set(p.get("protect", [])) if p else set()
        for d, truth in dates.items():
            mcp_wan = truth["amount"]
            rows = d_all[d_all["date"] == d]
            if rows.empty:
                failed.append((code, d, "库内无行"))
                continue
            i = rows.index[0]
            lib = float(pd.to_numeric(rows["amount"].iloc[0], errors="coerce") or 0)
            # 模拟: 若行在修段内且不在保护日 → ÷1e4
            for s, e, med in fix_ranges:
                if s <= i < e and i not in protect:
                    lib /= 1e4
                    break
            ratio = lib / mcp_wan if mcp_wan else 0
            if 0.5 <= ratio <= 2.0:
                passed += 1
                print(f"{code} {d}: 模拟后 {lib:.0f} vs 真值 {mcp_wan:.0f} "
                      f"→ {ratio:.2f} ✓")
            else:
                failed.append((code, d, f"ratio={ratio:.3f}"))
                print(f"{code} {d}: 模拟后 {lib:.0f} vs 真值 {mcp_wan:.0f} "
                      f"→ {ratio:.2f} ✗")
    print(f"\n闸门: {passed} 通过 / {len(failed)} 失败")
    if failed:
        print("失败明细:", failed)
        sys.exit(1)


if __name__ == "__main__":
    main()
