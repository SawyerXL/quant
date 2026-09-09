"""
单位锚校验 (2026-09-09 用户定: 归一的前置) —
对照 tests/unit_ground_truth.json (MCP 真值, 万元) 与库内值,
逐对分类库内单位口径: 元 / 万元 / 量级错配。归一必须让所有对
通过"元"口径(库标准=股/元), 否则不允许 commit。
用法: python scripts/verify_unit_ground_truth.py
"""
import sys, json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd

from data.storage import load_daily

ANCHOR = Path(__file__).parent.parent / "tests" / "unit_ground_truth.json"


def main():
    anchor = json.load(open(ANCHOR))
    print(f"{'代码':<8}{'日期':<12}{'库内值':>14}{'MCP真值(万元)':>16}"
          f"{'库内/MCP':>12}{'判定':>12}")
    n_yuan = n_wan = n_bad = 0
    for code, dates in anchor["stocks"].items():
        for d, truth in dates.items():
            mcp_wan = truth["amount"]
            if not mcp_wan:
                continue
            df = load_daily(code, d, d)
            if df.empty:
                print(f"{code:<8}{d:<12}{'无数据':>14}{mcp_wan:>16.0f}"
                      f"{'—':>12}{'无数据':>12}")
                n_bad += 1
                continue
            lib = float(pd.to_numeric(df["amount"], errors="coerce").iloc[0] or 0)
            if not lib:
                n_bad += 1
                verdict = "库内额为0"
            else:
                # MCP 万元 → 元 = ×1e4
                ratio = lib / (mcp_wan * 1e4)
                if 0.5 <= ratio <= 2.0:
                    verdict = "✓元口径"
                    n_yuan += 1
                elif 5e-5 <= ratio <= 2e-4:
                    verdict = "⚠万元口径"
                    n_wan += 1
                else:
                    verdict = "✗量级错配"
                    n_bad += 1
            print(f"{code:<8}{d:<12}{lib:>14.0f}{mcp_wan:>16.0f}"
                  f"{ratio:>12.2e}{verdict:>12}")
    print(f"\n汇总: ✓元 {n_yuan} / ⚠万元 {n_wan} / ✗错配或缺失 {n_bad}")


if __name__ == "__main__":
    main()
