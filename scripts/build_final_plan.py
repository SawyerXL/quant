"""
合并终计划 (2026-09-12) —
v6 计划(清晰多数) + MCP 灰带逐日真值(145只) + 59 处还原清单(×1e4)。
输出 logs/unit_final_plan.json: {library_max_date, fixes: {file: [days]},
restores: [(code, date, value)]}。
并跑 200 只抽样模拟: 残留≥100x 跳变应≈0。
"""
import sys, json, random
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd

from data.storage import load_daily, load_meta
from backtest_return_attribution import load_blacklist


def main():
    v6 = json.load(open("logs/unit_norm_plan.json"))
    v6_plan = v6 if isinstance(v6, list) else v6["plan"]
    max_date = v6.get("library_max_date", "?") if isinstance(v6, dict) else "?"
    gray = json.load(open("logs/unit_gray_resolution.json"))
    gray_res = gray.get("resolution", {})
    restore = json.load(open("logs/unit_quarantine.json"))
    restore_items = restore["items"]

    gray_codes = set(gray_res.keys())
    # 超薄文件(中位<1000万元, 永进不了top60池)与北交所同列缓处理,
    # 500x阈值在僵尸股上会误伤单日活跃日(002832型)
    bse = ("920", "430", "83", "87")
    deferred = set()
    for p in v6_plan:
        code = p["file"].split("/")[-1].replace(".parquet", "")
        # 北交所全号段(920/430/83/87)缓处理: MCP 无覆盖且不进任何池
        if code.startswith(bse):
            deferred.add(p["file"])
        elif p.get("med", 0) < 1000:
            # 2026-09-12 晚: 退化文件(新上市票 amount 近零, 元主导规则在
            # med≈0 上循环除坏)不分主导口径一律缓处理
            deferred.add(p["file"])
    fixes = {}
    for p in v6_plan:
        code = p["file"].split("/")[-1].replace(".parquet", "")
        if code in gray_codes or p["file"] in deferred:
            continue
        if p["fix_days"]:
            fixes[p["file"]] = p["fix_days"]
    final_deferred = {f: "med<1000" for f in deferred}

    # 灰带票: MCP 逐日真值 → 需要行号, 此处用 (code, date) 键, apply 时映射
    mcp_fixes = {}
    for code, r in gray_res.items():
        fd = sorted(dt for dt, v in r.items() if v == "fix")
        if fd:
            mcp_fixes[code] = fd

    final = {"library_max_date": max_date,
             "fixes_by_file": fixes,
             "mcp_fixes_by_date": mcp_fixes,
             "restores": restore_items,
             "deferred_files": final_deferred}
    json.dump(final, open("logs/unit_final_plan.json", "w"),
              ensure_ascii=False, default=str)
    print(f"终计划: v6 文件 {len(fixes)} + MCP灰带 {len(mcp_fixes)} 票 "
          f"+ 还原 {len(restore_items)} 处, 库锚 {max_date}")

    # 模拟: 200 抽样
    meta = load_meta("stock_info_full")
    bl = load_blacklist()
    codes = [str(c).zfill(6) for c in meta["code"].tolist()
             if str(c).zfill(6) not in bl]
    random.seed(11)
    sample = random.sample(codes, 200)
    resid, corrupt = 0, 0
    bad = []
    bse = ("920", "430", "83", "87")
    for c in sample:
        if c.startswith(bse):
            continue  # 缓处理票不参与残留统计(设计使然)
        d = load_daily(c, "2026-01-01", "2026-12-31")
        if d.empty:
            continue
        d = d.sort_values("date").reset_index(drop=True)
        am = pd.to_numeric(d["amount"], errors="coerce").tolist()
        fixed = set()
        if c in mcp_fixes:
            mf = set(mcp_fixes[c])
            for i in range(len(d)):
                if str(d["date"].iloc[i])[:10] in mf:
                    fixed.add(i)
        else:
            for f in [x for x in fixes if x.split("/")[-1].replace(".parquet", "") == c]:
                fixed |= set(fixes[f])
        # 还原日: 值×1e4
        restored = set()
        for it in restore_items:
            if it["code"] == c:
                for i in range(len(d)):
                    if str(d["date"].iloc[i])[:10] == it["date"]:
                        restored.add(i)
        sim = []
        for i, a in enumerate(am):
            v = a
            if v and v > 0:
                if i in restored:
                    v = v * 1e4
                elif i in fixed:
                    v = v / 1e4
            sim.append(v)
        prev = None
        for i, v in enumerate(sim):
            if v is None or v <= 0:
                continue
            if prev is not None and max(v, prev) / min(v, prev) >= 100:
                resid += 1
                bad.append(c)
                break
            prev = v
        med = np.median([a for a in am if a and a > 0]) if any(a and a > 0 for a in am) else 0
        if med and med < 2e6:
            for i in fixed:
                if am[i] and am[i] < med * 100:
                    corrupt += 1
                    break
    print(f"模拟: 残留≥100x {resid}/200 | 疑似误修 {corrupt}")
    if bad:
        print("残留股票:", bad[:10])


if __name__ == "__main__":
    main()
