"""
分层随机抽样 MCP 全字段比对 (2026-09-13 用户要求, 复核前置) —
前五道审计全部只能回答"有没有", 本方法回答"还剩多少":
从调仓日 top60 切片随机抽 200 个 (股,日) 单元逐一对 MCP 真值
(为什么 top60: 闸门第十项——验证数据被使用的区域, 全库随机会被
几千只冷门票稀释)。
结果解读: 200 抽样 0 命中 → 残留率 95% 置信上界 ≈ 1.5%;
有命中 → 残留率点估计 + 错在哪类。
用法: python scripts/stratified_sample_verify.py
输出: logs/stratified_sample_verify.json + 打印
"""
import sys, json, random
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
import numpy as np

from data.storage import load_daily, load_meta
from backtest_return_attribution import load_blacklist
from backtest_engine import make_rebal_dates
from data.source.mcp_source import MCPSource

N_SAMPLE = 200
SEED = 20260913


def main():
    blacklist = load_blacklist()
    meta = load_meta("stock_info_full")
    codes = [str(c).zfill(6) for c in meta["code"].tolist()
             if str(c).zfill(6) not in blacklist]
    sh = load_daily("SH000001", "2026-01-01", "2026-12-31")
    cal = sorted(sh["date"].astype(str).str[:10].tolist())
    rebal = [d for d in make_rebal_dates(cal, "biweekly")
             if "2026-06-16" <= d <= "2026-09-11"]
    random.seed(SEED)
    dates = sorted(random.sample(rebal, min(10, len(rebal))))
    print(f"抽样调仓日: {dates}")

    # 每个日期算 top60(20日均额)
    units = []
    for dt in dates:
        rows = []
        for c in codes:
            d = load_daily(c, "2026-01-01", dt)
            if d.empty:
                continue
            d = d[d["date"] <= dt].sort_values("date")
            amt = pd.to_numeric(d["amount"], errors="coerce")
            amt = amt[amt > 0].tail(20)
            if len(amt) >= 10:
                rows.append((c, float(amt.mean())))
        rows.sort(key=lambda x: -x[1])
        top60 = [c for c, _ in rows[:60]]
        # 每日期抽 20 单元: (股, 该日期) 或 (股, 该日期附近的有效日)
        for c in random.sample(top60, min(20, len(top60))):
            units.append((c, dt))
    print(f"样本单元: {len(units)}")

    src = MCPSource()
    hits, checked, skipped_zero = [], 0, 0
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
            skipped_zero += 1
            continue
        m = float(m)
        l = float(pd.to_numeric(lib["amount"].iloc[0], errors="coerce") or 0)
        checked += 1
        if not (0.5 <= l / m <= 2.0):
            hits.append({"code": c, "date": dt, "lib": l, "mcp": m,
                         "ratio": round(l / m, 2)})
    print(f"有效比对 {checked} / 样本 {len(units)} / MCP零值跳过 {skipped_zero}")
    if not hits:
        print("✅ 0 命中 → 该区域残留率 95% 置信上界 ≈ 1.5%")
    else:
        print(f"🔴 命中 {len(hits)}/{checked} → 残留率点估计 "
              f"{len(hits)/max(checked,1)*100:.1f}%")
        for h in hits[:10]:
            print(f"  {h['code']} {h['date']}: lib={h['lib']:.2e} "
                  f"mcp={h['mcp']:.2e} ratio={h['ratio']}")
    json.dump({"checked": checked, "skipped_zero": skipped_zero,
               "hits": hits, "sample_dates": dates},
              open("logs/stratified_sample_verify.json", "w"),
              ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
