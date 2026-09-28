"""ST 前置小测 (2026-09-28, F 区裁定降 P3 的前置): 当前 ST 名单 ∩ 近 12 个月
pool30 入选名单。盲区声明: ST 状态用当前名称(非历史 PIT)——对"曾入池现 ST"的
票会正确抓到, 对"已摘帽"的票抓不到(方向=漏报, 但风险问题=ST 入池, 现 ST 的票
若从未入池即安全); 排名口径=各月末最后一个交易日的当日成交额(信号链为 20 日
口径, ST 票成交额小两个量级, 单日口径足以判别)。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import numpy as np
from loguru import logger
from data.storage import load_meta, load_daily

logger.remove()
START, END = "2025-09-01", "2026-09-24"

meta = load_meta("stock_info_full")
st_codes = set(meta.loc[meta["name"].astype(str).str.contains("ST", na=False),
                        "code"].astype(str).str.zfill(6))
print(f"当前 ST(名称代理): {len(st_codes)} 只")

# 各月月末交易日
cal = load_meta("trade_calendar")["trade_date"].tolist()
dates = [d for d in cal if START <= d <= END]
month_ends = []
for d in dates:
    if not month_ends or d[:7] != month_ends[-1][:7]:
        month_ends.append(d)
print(f"检查月末日期: {month_ends[-12:]}")

# 全票 amount 面板(仅 amount 列)
codes = meta["code"].tolist()
panel = {}
for code in codes:
    try:
        d = load_daily(code, START, END)
        if d.empty:
            continue
        d["date"] = pd.to_datetime(d["date"])
        d = d.set_index("date").sort_index()
        amt = pd.to_numeric(d.get("amount", pd.Series(dtype=float)), errors="coerce")
        panel[code] = amt.dropna()
    except Exception:
        pass
print(f"面板覆盖: {len(panel)} 只")

union_top30 = set()
st_hits = []
for m in month_ends[-12:]:
    ts = pd.Timestamp(m)
    vals = {}
    for c, s in panel.items():
        s2 = s[s.index <= ts]
        if len(s2):
            vals[c] = float(s2.iloc[-1])
    top30 = sorted(vals, key=vals.get, reverse=True)[:30]
    union_top30 |= set(top30)
    hits = sorted(st_codes & set(top30))
    if hits:
        st_hits.append((m, hits))
    # ST 票的最高排名
    ranks = {c: i + 1 for i, c in enumerate(sorted(vals, key=vals.get, reverse=True))}
    st_ranks = {c: ranks[c] for c in st_codes if c in ranks}
    if st_ranks:
        best = min(st_ranks, key=st_ranks.get)
        print(f"  {m}: pool30∩ST = {hits or '无'} | ST 最高排名 {st_ranks[best]} ({best})")
    else:
        print(f"  {m}: pool30∩ST = 无 | 无 ST 票进入排名")

print(f"\n近 12 个月 pool30 并集: {len(union_top30)} 只 | ST 交集: {sorted(st_codes & union_top30) or '∅'}")
print(f"结论: {'PASS — 交集为空, P3 降级成立(盲区: 非历史PIT/单日口径)' if not (st_codes & union_top30) else 'FAIL — 有交集, 需逐只归因'}")
