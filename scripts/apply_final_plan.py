"""
终计划 apply 执行器 (2026-09-12 周六晚, 用户批准"现在继续做") —
执行 logs/unit_final_plan.json:
  fixes_by_file:  逐文件行号 ÷1e4 (amount+volume)
  mcp_fixes_by_date: 灰带票按日期映射行 ÷1e4 (MCP 真值裁决)
  restores:       (code, date) ×1e4 (多次转换痕迹还原, amount+volume)
前置: 库锚校验(最新日==计划生成时) / 全量备份+可读性验证 / 原子写。
备份目录 backup_unit_norm_20260912/ (已存在则拒绝执行)。
回滚判据(事前钉死, 见 logs/rollback_criteria_20260912.md):
  任一审计不通过 → 整体回滚, 禁止向前修补。
用法: python scripts/apply_final_plan.py
"""
import sys, json, shutil, os, random
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd

DAILY_DIR = Path("/data/quant_data_store/daily")
PLAN_PATH = Path("logs/unit_final_plan.json")


def main():
    final = json.load(open(PLAN_PATH))
    want_max = final["library_max_date"]

    # ① 库锚校验
    cur_max = max((str(pd.read_parquet(f, columns=["date"])["date"].max())[:10]
                   for f in DAILY_DIR.glob("2026/*.parquet")
                   if not f.name.startswith(("SH", "SZ"))), default="?")
    assert cur_max == want_max, (f"计划失效: 库最新日 {cur_max} ≠ "
                                 f"计划生成时 {want_max}——重新生成计划")
    print(f"① 库锚校验: {cur_max} == {want_max} ✓")

    # ② 全量备份 + 可读性验证
    backup = DAILY_DIR.parent / "backup_unit_norm_20260912" / "daily"
    assert not backup.exists(), f"备份已存在 {backup} — 拒绝重复执行"
    print("② 全量备份 daily/ ...")
    shutil.copytree(DAILY_DIR, backup)
    n_src = sum(1 for _ in DAILY_DIR.rglob("*.parquet"))
    n_bak = sum(1 for _ in backup.rglob("*.parquet"))
    assert n_src == n_bak
    for sp in random.sample(list(backup.rglob("*.parquet")), 3):
        pd.read_parquet(sp)
    disk = shutil.disk_usage(DAILY_DIR)
    assert disk.free > 10 * 2**30
    print(f"   备份 {n_bak} 文件一致, 3 抽样可读, 磁盘余 {disk.free/2**30:.1f}G ✓")

    # ③ 执行
    print("③ 执行归一...")
    done = 0
    for f_str, days in final["fixes_by_file"].items():
        f = Path(f_str)
        if not f.exists():
            continue
        d = pd.read_parquet(f).sort_values("date").reset_index(drop=True)
        for i in days:
            if i >= len(d):
                continue
            for col in ("amount", "volume"):
                v = pd.to_numeric(d.loc[i, col], errors="coerce")
                if pd.notna(v) and v > 0:
                    d.loc[i, col] = v / 1e4
        tmp = f.with_suffix(".parquet.tmp")
        d.to_parquet(tmp, index=False)
        os.replace(tmp, f)
        done += 1
    print(f"   v6 文件 {done} 个")

    mcp_done = 0
    for code, dates in final["mcp_fixes_by_date"].items():
        dset = set(dates)
        for f in DAILY_DIR.glob(f"2026/{code}.parquet"):
            d = pd.read_parquet(f).sort_values("date").reset_index(drop=True)
            hit = False
            for i in range(len(d)):
                if str(d["date"].iloc[i])[:10] in dset:
                    for col in ("amount", "volume"):
                        v = pd.to_numeric(d.loc[i, col], errors="coerce")
                        if pd.notna(v) and v > 0:
                            d.loc[i, col] = v / 1e4
                    hit = True
            if hit:
                tmp = f.with_suffix(".parquet.tmp")
                d.to_parquet(tmp, index=False)
                os.replace(tmp, f)
                mcp_done += 1
    print(f"   MCP灰带 {mcp_done} 票")

    rest_done = 0
    restore_map = {}
    for it in final["restores"]:
        restore_map.setdefault(it["code"], set()).add(it["date"])
    for code, dates in restore_map.items():
        for f in DAILY_DIR.glob(f"2026/{code}.parquet"):
            d = pd.read_parquet(f).sort_values("date").reset_index(drop=True)
            hit = False
            for i in range(len(d)):
                if str(d["date"].iloc[i])[:10] in dates:
                    for col in ("amount", "volume"):
                        v = pd.to_numeric(d.loc[i, col], errors="coerce")
                        if pd.notna(v) and v > 0:
                            d.loc[i, col] = v * 1e4
                    hit = True
            if hit:
                tmp = f.with_suffix(".parquet.tmp")
                d.to_parquet(tmp, index=False)
                os.replace(tmp, f)
                rest_done += 1
    print(f"   还原 {rest_done} 票 / {len(final['restores'])} 处")
    print("④ 请运行: verify_unit_ground_truth.py / unit_consistency_check.py "
          "/ unit_ratio_audit.py / 幂等检查(重跑dry-run≈0)")


if __name__ == "__main__":
    main()
