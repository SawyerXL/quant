"""
001xxx 全量枚举 + MCP 逐只比对 (2026-09-13 用户要求) —
异常群集线索: 001218(漂移因子)/001230(34-219x虚高)/001317(价格振荡)/
12只极端qfq断裂(全001xxx)/09-09回填4只歧义码——前缀群集非巧合。
假设: 001xxx 在新浪/东财代码空间与基金或其他标的冲突, 取回错误标的
行情(变动的非10^4倍率=另一标的成交额比值天然变动)。
按闸门九: 这类应该枚举不该抽样——001xxx 数量小, 全查。
每只取 3 个日期 MCP 比对, 命中即列入隔离候选。
用法: python scripts/enumerate_001xxx.py > logs/enumerate_001xxx.log 2>&1
"""
import sys, json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
import numpy as np

from data.storage import load_daily, load_meta
from backtest_return_attribution import load_blacklist
from data.source.mcp_source import MCPSource

DATES = ["2026-04-15", "2026-06-15", "2026-08-15"]


def main():
    blacklist = load_blacklist()
    meta = load_meta("stock_info_full")
    codes = sorted({str(c).zfill(6) for c in meta["code"].tolist()
                    if str(c).zfill(6) not in blacklist
                    and str(c).zfill(6).startswith("001")})
    print(f"001xxx 枚举: {len(codes)} 只")
    src = MCPSource()
    hits = []
    for c in codes:
        for dt in DATES:
            lib = load_daily(c, dt, dt)
            if lib.empty:
                continue
            l = pd.to_numeric(lib["amount"].iloc[0], errors="coerce")
            if pd.isna(l) or l <= 0:
                continue
            try:
                mcp = src.get_daily(c, dt, dt)
            except Exception:
                mcp = pd.DataFrame()
            if mcp is None or len(mcp) == 0 or "amount" not in mcp.columns:
                continue
            m = pd.to_numeric(mcp["amount"].iloc[-1], errors="coerce")
            if pd.isna(m) or m <= 0:
                continue
            ratio = float(l) / float(m)
            if not (0.5 <= ratio <= 2.0):
                hits.append({"code": c, "date": dt,
                             "lib": round(float(l), 1),
                             "mcp": round(float(m), 1),
                             "ratio": round(ratio, 1)})
                print(f"  ✗ {c} {dt}: 库 {float(l):.0f} vs MCP {float(m):.0f} "
                      f"比值 {ratio:.1f}")
    print(f"\n命中: {len(hits)} 处")
    json.dump({"hits": hits, "codes_checked": codes},
              open("logs/enumerate_001xxx.json", "w"),
              ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
