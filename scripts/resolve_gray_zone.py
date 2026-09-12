"""
灰色带 MCP 真值解析 (2026-09-12, 归一 v6 的补全件) —
单变量分类在 [1e5, 2e6] 重叠带存在本质歧义(万元大票 vs 元小票),
v6 阈值 500× 的残留实测为 8/200(含次阈值元日 390-450× 与元主导
混合日)。对灰色带股票逐只查 MCP 真值(万元标注), 逐日判定:
  lib/MCP ≈ 1    → 库内已是万元 → 不修
  lib/MCP ≈ 1e-4 → 库内是元   → 修(÷1e4)
灰色带定义(宽口径):
  万元主导文件: 任一有效日 ∈ [中位×100, 中位×1000)
  元主导文件:   任一有效日 ∈ (中位/1000, 中位×1000]
对灰色票查询 MCP 过渡窗口(2026-06-01~2026-09-11)全窗口, 逐日比
对, 输出 logs/unit_gray_resolution.json:
  {code: {date: "fix"|"keep"|"missing"}}
用法: python scripts/resolve_gray_zone.py
"""
import sys, json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd

from data.storage import load_daily
from data.source.mcp_source import MCPSource

PLAN_PATH = Path("logs/unit_norm_plan.json")
OUT_PATH = Path("logs/unit_gray_resolution.json")
DAILY_DIR = Path("/data/quant_data_store/daily")


def main():
    plan = json.load(open(PLAN_PATH))
    gray = []
    for p in plan:
        f = p["file"]
        if "/2026/" not in f:
            continue
        med = p["med"]
        if not med:
            continue
        try:
            d = pd.read_parquet(f, columns=["date", "amount"])
        except Exception:
            continue
        am = pd.to_numeric(d["amount"], errors="coerce").tolist()
        if p["majority"] == "万元":
            suspect = any(v and med * 200 <= v < med * 500 for v in am)
        else:
            suspect = any(v and med / 500 < v <= med / 200 for v in am)
        if suspect:
            gray.append(p["file"].split("/")[-1].replace(".parquet", ""))
    print(f"灰色带股票: {len(gray)} 只, 预计 {len(gray)*26.5/60:.0f} 分钟")
    src = MCPSource()
    done = 0
    resolution, switches = {}, {}
    if OUT_PATH.exists():
        old = json.load(open(OUT_PATH))
        resolution, switches = old.get("resolution", {}), old.get("switches", {})
        # 重判已有条目(判读带已修正): 把 old 里的 odd(1.00e+04) 改为 fix
        for code, res in resolution.items():
            first_fix = None
            for dt, v in sorted(res.items()):
                if isinstance(v, str) and v.startswith("odd") and "1.00e+04" in v:
                    res[dt] = "fix"
                    if first_fix is None:
                        first_fix = dt
            if first_fix:
                switches[code] = first_fix
        done = len(resolution)
        print(f"复用已有解析 {done} 只(判读带已重判)")
    for code in gray:
        if code in resolution:
            continue
        try:
            mcp = src.get_daily(code, "2026-06-01", "2026-09-11")
            lib = load_daily(code, "2026-06-01", "2026-09-11")
            if mcp is None or "date" not in mcp.columns \
                    or "amount" not in mcp.columns:
                resolution[code] = {}
                continue
            res = {}
            mcp_by_date = {}
            for _, r in mcp.iterrows():
                try:
                    mcp_by_date[str(r["date"])[:10]] = float(
                        pd.to_numeric(r["amount"], errors="coerce") or 0)
                except Exception:
                    continue
            first_fix = None
            for _, r in lib.iterrows():
                dt = str(r["date"])[:10]
                m = mcp_by_date.get(dt)
                if not m:
                    continue
                l = float(pd.to_numeric(r["amount"], errors="coerce") or 0)
                if l <= 0:
                    continue
                ratio = l / m
                # 2026-09-12 判读带修正(前版反了): MCP返回万元,
                # 库内元=lib/MCP≈1e4→fix; 库内万元=≈1→keep
                if 0.5 <= ratio <= 2.0:
                    res[dt] = "keep"
                elif 5e3 <= ratio <= 2e4:
                    res[dt] = "fix"
                    if first_fix is None:
                        first_fix = dt
                else:
                    res[dt] = f"odd({ratio:.2e})"
            resolution[code] = res
            if first_fix:
                switches[code] = first_fix
        except Exception as e:
            resolution[code] = {"_error": str(e)}
        done += 1
        if done % 10 == 0:
            print(f"  已解析 {done}/{len(gray)}, 切换日 {len(switches)}")
            json.dump({"switches": switches, "resolution": resolution},
                      open(OUT_PATH, "w"), ensure_ascii=False)
    json.dump({"switches": switches, "resolution": resolution},
              open(OUT_PATH, "w"), ensure_ascii=False)
    n_fix = sum(1 for r in resolution.values() for v in r.values() if v == "fix")
    n_keep = sum(1 for r in resolution.values() for v in r.values() if v == "keep")
    n_odd = sum(1 for r in resolution.values() for v in r.values()
                if isinstance(v, str) and v.startswith("odd"))
    from collections import Counter
    dist = Counter(switches.values())
    print(f"完成: {n_fix} fix / {n_keep} keep / {n_odd} 异常")
    print("切换日期分布(全库一致性检验):")
    for dt, cnt in sorted(dist.items()):
        print(f"  {dt}: {cnt}只")


if __name__ == "__main__":
    main()
