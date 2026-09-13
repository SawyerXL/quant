"""
抽样补层 A/B (2026-09-13 用户要求, 继 200 单元 top60 层之后) —
现有层只能抓"幽灵入池"(假阳性), 结构上抓不到:
  A 层: 归一计划修改过的记录——修改本身是否正确(过度归一=池外消失,
        137 处还原值证明过度归一发生过)
  B 层: 排名 60~300 带——边界翻转(该进没进, 被过度归一挤出池)
各 100 单元, MCP 逐一对真值。
用法: python scripts/stratified_sample_verify_ab.py
输出: logs/stratified_sample_verify_ab.json
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
    random.seed(SEED)
    blacklist = load_blacklist()
    meta = load_meta("stock_info_full")
    codes = [str(c).zfill(6) for c in meta["code"].tolist()
             if str(c).zfill(6) not in blacklist]

    # A 层: 归一计划修改过的 (文件, 日) 记录
    plan = json.load(open("logs/unit_final_plan.json"))
    units_a = []
    for f, days in plan["fixes_by_file"].items():
        code = f.split("/")[-1].replace(".parquet", "")
        d = load_daily(code, "2026-01-01", "2026-12-31")
        if d.empty:
            continue
        d = d.sort_values("date").reset_index(drop=True)
        for i in days:
            if i < len(d):
                units_a.append((code, str(d["date"].iloc[i])[:10]))
    restore = json.load(open("logs/unit_quarantine.json"))["items"]
    units_a += [(it["code"], it["date"]) for it in restore]
    units_a = random.sample(units_a, min(100, len(units_a)))

    # B 层: 排名 60~300 带(最新截面)
    sh = load_daily("SH000001", "2026-01-01", "2026-12-31")
    last = str(sh["date"].max())[:10]
    rows = []
    for c in codes:
        d = load_daily(c, "2026-01-01", last)
        if d.empty:
            continue
        d = d[d["date"] <= last].sort_values("date")
        amt = pd.to_numeric(d["amount"], errors="coerce")
        amt = amt[amt > 0].tail(20)
        if len(amt) >= 10:
            rows.append((c, float(amt.mean())))
    rows.sort(key=lambda x: -x[1])
    band = rows[60:300]
    units_b = [(c, last) for c, _ in random.sample(band, min(100, len(band)))]

    src = MCPSource()
    results = {}
    for layer, units in [("A_modified", units_a), ("B_band60_300", units_b)]:
        hits, checked, skipped = [], 0, 0
        for c, dt in units:
            lib = load_daily(c, dt, dt)
            if lib.empty:
                continue
            try:
                mcp = src.get_daily(c, dt, dt)
            except Exception:
                mcp = pd.DataFrame()
            if mcp is None or len(mcp) == 0 or "amount" not in mcp.columns:
                continue
            m = pd.to_numeric(mcp["amount"].iloc[-1], errors="coerce")
            if pd.isna(m) or not m or m <= 0:
                skipped += 1
                continue
            m = float(m)
            l = float(pd.to_numeric(lib["amount"].iloc[0], errors="coerce") or 0)
            checked += 1
            if not (0.5 <= l / m <= 2.0):
                hits.append({"code": c, "date": dt, "lib": l, "mcp": m,
                             "ratio": round(l / m, 2)})
        results[layer] = {"units": len(units), "checked": checked,
                          "skipped": skipped, "hits": hits}
        print(f"{layer}: 单元{len(units)} 有效比对{checked} 跳过{skipped} "
              f"命中{len(hits)}")
        for h in hits[:5]:
            print(f"  {h['code']} {h['date']}: lib={h['lib']:.2e} "
                  f"mcp={h['mcp']:.2e} ratio={h['ratio']}")
    json.dump(results, open("logs/stratified_sample_verify_ab.json", "w"),
              ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
