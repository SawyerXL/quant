"""
ingestion 单位 dry-run 验证 (2026-09-12 用户apply前置①) —
三条取数路径各取 1 只票 1 天, 打印归一后 volume/amount,
对照 MCP 锚(万股/万元) + 比值审计(amount/volume≈收盘价)。
不写库。三条全对才允许 apply。
用法: python scripts/verify_ingestion_units.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
import numpy as np

from data.source.akshare_source import AkshareSource
from data.source.mcp_source import MCPSource

# 锚真值(万元) — tests/unit_ground_truth.json 的 07-15 三票
ANCHOR = {
    ("600519", "2026-07-15"): 892286.14,
    ("000063", "2026-07-15"): 1067957.67,
    ("300750", "2026-07-15"): 1344021.52,
}


def check(code, date, df, label):
    if df is None or df.empty:
        print(f"[{label}] {code} {date}: 源返回空(不可测)")
        return False
    row = df[df["date"].astype(str).str[:10] == date]
    if row.empty:
        print(f"[{label}] {code} {date}: 该日无行")
        return False
    r = row.iloc[0]
    amt = float(pd.to_numeric(r["amount"], errors="coerce") or 0)
    vol = float(pd.to_numeric(r["volume"], errors="coerce") or 0)
    close = float(pd.to_numeric(r["close"], errors="coerce") or 0)
    ratio = amt / vol if vol else 0
    key = (code, date)
    ok_anchor = True
    msg = ""
    if key in ANCHOR:
        r_anchor = amt / ANCHOR[key]
        ok_anchor = 0.5 <= r_anchor <= 2.0
        msg += f"锚比值 {r_anchor:.2f} {'✓' if ok_anchor else '✗'}"
    ok_ratio = 0.5 <= ratio / close <= 2.0 if close else False
    msg += f" 额/量={ratio:.1f} vs 收盘{close:.1f} {'✓' if ok_ratio else '✗'}"
    print(f"[{label}] {code} {date}: amount={amt:.1f}(万元?) volume={vol:.1f}(万股?) "
          f"{msg}")
    return ok_anchor and ok_ratio


def main():
    date = "2026-07-15"
    ak = AkshareSource()
    mcp = MCPSource()
    results = []

    # 路径1: akshare 新浪(daily主路径, 新浪返回股/元 → 归一后应万股/万元)
    df1 = ak._daily_sina("600519", date, date)
    results.append(check("600519", date, df1, "新浪主路径"))

    # 路径2: akshare 东财(被封锁时返回空属预期, 只测可达性)
    df2 = ak._daily_em("000063", date, date)
    if df2.empty:
        print("[东财路径] 000063: 返回空(东财封IP预期内, 走新浪兜底已由路径1覆盖)")
    else:
        results.append(check("000063", date, df2, "东财路径"))

    # 路径3: MCP(查询已声明 成交量万股/成交额万元, 不应被改)
    df3 = mcp.get_daily("300750", date, date)
    results.append(check("300750", date, df3, "MCP路径"))

    n = sum(results)
    print(f"\n结论: {n}/{(2 if df2.empty else 3)} 条可测路径通过")
    if n < (2 if df2.empty else 3):
        print("🔴 未全通过 — 禁止 apply, 先修 ingestion")
        sys.exit(1)


if __name__ == "__main__":
    main()
