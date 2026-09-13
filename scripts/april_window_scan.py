"""
四月窗口全量比对 (2026-09-13 用户证伪检验) —
污染是窗口型(四月, ~20交易日)而非前缀型: 4/4 命中全在四月, 而
001xxx 前缀只在 001xxx 里查过(枚举框假象)。本脚本:
  A) 87 只 001xxx × 四月整月(MCP 范围查询一次回整月)
  B) 50 只随机非 001xxx × 四月整月(证伪前缀群集)
逐日比对 lib vs MCP, 命中即列入。
用法: python scripts/april_window_scan.py > logs/april_window_scan.log 2>&1
输出: logs/april_window_scan.json
"""
import sys, json, random
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
import numpy as np

from data.storage import load_daily, load_meta
from backtest_return_attribution import load_blacklist
from data.source.mcp_source import MCPSource

SEED = 20260913


def main():
    blacklist = load_blacklist()
    meta = load_meta("stock_info_full")
    all_codes = sorted({str(c).zfill(6) for c in meta["code"].tolist()
                        if str(c).zfill(6) not in blacklist})
    codes_a = [c for c in all_codes if c.startswith("001")]
    random.seed(SEED)
    codes_b = random.sample([c for c in all_codes if not c.startswith("001")
                             and not c.startswith(("920", "430", "83", "87"))],
                            50)
    print(f"A层(001xxx全量): {len(codes_a)} 只 / B层(非001xxx随机): {len(codes_b)} 只")
    src = MCPSource()
    hits = []
    for layer, codes in [("A_001xxx", codes_a), ("B_non001xxx", codes_b)]:
        for c in codes:
            lib = load_daily(c, "2026-04-01", "2026-04-30")
            if lib.empty:
                continue
            try:
                mcp = src.get_daily(c, "2026-04-01", "2026-04-30")
            except Exception:
                mcp = pd.DataFrame()
            if mcp is None or len(mcp) == 0 or "amount" not in mcp.columns:
                continue
            mcp_map = {}
            for _, r in mcp.iterrows():
                mcp_map[str(r["date"])[:10]] = float(
                    pd.to_numeric(r["amount"], errors="coerce") or 0)
            for _, r in lib.iterrows():
                dt = str(r["date"])[:10]
                m = mcp_map.get(dt)
                if not m:
                    continue
                l = pd.to_numeric(r["amount"], errors="coerce")
                if pd.isna(l) or l <= 0:
                    continue
                ratio = float(l) / m
                if not (0.5 <= ratio <= 2.0):
                    hits.append({"layer": layer, "code": c, "date": dt,
                                 "lib": round(float(l), 1),
                                 "mcp": round(m, 1),
                                 "ratio": round(ratio, 1)})
                    print(f"  ✗ [{layer}] {c} {dt}: 库 {float(l):.0f} vs "
                          f"MCP {m:.0f} 比值 {ratio:.1f}")
    print(f"\n命中: {len(hits)} 处")
    json.dump({"hits": hits, "codes_a": codes_a, "codes_b": codes_b},
              open("logs/april_window_scan.json", "w"),
              ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
