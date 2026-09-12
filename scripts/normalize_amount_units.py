"""
成交额/成交量单位归一 v5 (2026-09-12) —
把全库归一为单一口径 **万股/万元**(库历史标准, 与 MCP 锚标签一致)。
v1-v4 教训: 分段+交替/相对/岛识别全部在"摆尾段/单日夹心"上漏判
(000902 万元主导段混元日 / 688610 元段尾单日万元 / 002025 万元段
内含 48 日元尾——三种模式段级投票全盲)。v5 弃分段, 逐文件逐日:
  文件口径 = 元主导 若 (全文件有效额中位数>2e6) 或 (首有效日≥
            2026-06-16 的新文件), 否则万元主导
  元主导文件: 所有 >中位/1000 的日子 = 元 → ÷1e4 (万元日罕见, 除外)
  万元主导文件: 所有 ≥中位×1000 的日子 = 元日 → ÷1e4
真实换手 10^3 倍日内变化不存在, 单位差恰是 10^4 —— 1000 阈值保守。
安全: 默认 dry-run; --apply 全量备份 daily/ + 备份可读性验证 + 原子写。
锚闸门: apply 后 verify_unit_ground_truth.py 全部有数据配对必须通过。
用法: python scripts/normalize_amount_units.py [--apply]
"""
import sys, json, argparse, shutil, os, random
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd

DAILY_DIR = Path("/data/quant_data_store/daily")


def classify_v5(am, first_date):
    """返回 (fix_days, med, is_yuan_majority)。am=amount 列表。"""
    valid = [v for v in am if v is not None and v > 0]
    if not valid:
        return [], 0.0, False
    med = float(np.median(valid))
    is_yuan = (med > 2e6) or (first_date >= pd.Timestamp("2026-06-16"))
    # 2026-09-12 v6: 阈值 500×——真实换手波动<100×(涨停潮~20×),
    # 残留元日实测全部 ≥800× 中位, 1000 阈值漏判小票元日(920689类
    # 8.6e6 vs 中位9.9e3=874×), 500 取在两类之间
    if is_yuan:
        fix_days = [i for i, v in enumerate(am) if v is not None and v > med / 500]
    else:
        fix_days = [i for i, v in enumerate(am) if v is not None and v >= med * 500]
    return fix_days, med, is_yuan


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    files = sorted(DAILY_DIR.rglob("*.parquet"))
    print(f"扫描 {len(files)} 个文件...")
    plan = []
    for f in files:
        if f.name.startswith(("SH", "SZ")):
            continue
        try:
            d = pd.read_parquet(f, columns=["date", "amount"])
        except Exception:
            continue
        if d.empty:
            continue
        d = d.sort_values("date").reset_index(drop=True)
        am = pd.to_numeric(d["amount"], errors="coerce").tolist()
        first = pd.to_datetime(d["date"].iloc[0], errors="coerce")
        fix_days, med, is_yuan = classify_v5(am, first)
        if fix_days:
            plan.append({"file": str(f), "med": med,
                         "majority": "元" if is_yuan else "万元",
                         "fix_days": fix_days})
    # 计划有效期锚(2026-09-12): 库内最新日期必须等于计划生成时记录值,
    # 日更后库变了计划即失效——"计划与现实脱节"与配置漂移同型
    max_date = max((pd.read_parquet(f, columns=["date"])["date"].max()
                    for f in DAILY_DIR.glob("2026/*.parquet")
                    if not f.name.startswith(("SH", "SZ"))), default=None)
    max_date = str(max_date)[:10] if max_date is not None else "?"
    print(f"需归一文件数: {len(plan)}, 需修日总数: "
          f"{sum(len(p['fix_days']) for p in plan)}, 库最新日: {max_date}")
    json.dump({"library_max_date": max_date, "plan": plan},
              open("logs/unit_norm_plan.json", "w"), ensure_ascii=False,
              default=str)
    print("计划落盘: logs/unit_norm_plan.json (dry-run, 未改库)")
    if args.apply:
        apply_plan(plan)


def apply_plan(payload):
    plan = payload["plan"]
    want_max = payload.get("library_max_date")
    cur_max = max((pd.read_parquet(f, columns=["date"])["date"].max()
                   for f in DAILY_DIR.glob("2026/*.parquet")
                   if not f.name.startswith(("SH", "SZ"))), default=None)
    cur_max = str(cur_max)[:10] if cur_max is not None else "?"
    assert cur_max == want_max, (
        f"计划失效: 库最新日 {cur_max} ≠ 计划生成时 {want_max}——"
        f"库已更新, 请重新生成计划")
    print(f"计划有效期校验: {cur_max} == {want_max} ✓")
    backup = DAILY_DIR.parent / "backup_unit_norm_20260912" / "daily"
    if backup.exists():
        print(f"备份目录已存在: {backup} — 拒绝重复执行")
        return
    print(f"全量备份 daily/ → {backup} ...")
    shutil.copytree(DAILY_DIR, backup)
    n_src = sum(1 for _ in DAILY_DIR.rglob("*.parquet"))
    n_bak = sum(1 for _ in backup.rglob("*.parquet"))
    assert n_src == n_bak, f"备份文件数不一致: {n_src} vs {n_bak}"
    for sp in random.sample(list(backup.rglob("*.parquet")), 3):
        pd.read_parquet(sp)
    disk = shutil.disk_usage(DAILY_DIR)
    assert disk.free > 10 * 2**30, f"磁盘余量不足: {disk.free/2**30:.1f}G"
    print(f"备份验证: {n_bak} 文件一致, 3 抽样可读, 磁盘余 {disk.free/2**30:.1f}G ✓")
    print(f"执行归一 {len(plan)} 文件...")
    done = 0
    for p in plan:
        f = Path(p["file"])
        if not f.exists():
            continue
        d = pd.read_parquet(f)
        d = d.sort_values("date").reset_index(drop=True)
        for i in p["fix_days"]:
            if i >= len(d):
                continue
            amt = pd.to_numeric(d.loc[i, "amount"], errors="coerce")
            vol = pd.to_numeric(d.loc[i, "volume"], errors="coerce")
            if pd.notna(amt) and amt > 0:
                d.loc[i, "amount"] = amt / 1e4
            if pd.notna(vol) and vol > 0:
                d.loc[i, "volume"] = vol / 1e4
        tmp = f.with_suffix(".parquet.tmp")
        d.to_parquet(tmp, index=False)
        os.replace(tmp, f)
        done += 1
        if done % 2000 == 0:
            print(f"  已处理 {done}/{len(plan)}")
    print(f"归一完成: {done} 文件已重写, 备份在 {backup}")
    print("请立即运行 verify_unit_ground_truth.py + unit_consistency_check.py")


if __name__ == "__main__":
    main()
