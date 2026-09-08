"""
S0-PIT 成员表审计 (2026-09-08 深夜, 用户最高优先) —
370/800 无覆盖代码逐类分类 + "成员表是否真 PIT"检验。
分类维度: ①指数代码混入(399xxx/000xxx指数) ②退市(t7表) ③北交所(920/430/83/87)
④次新(baostock首日≈上市日, 晚于快照日) ⑤代码变更/无任何档案 ⑥纯数据缺失
(baostock有数据而本地无→需回填)。
真PIT检验: ①快照间成员流转率(真PIT半年~5-15%) ②2019-06-28快照中
baostock首日 > 2019-06-28 的代码数(>0=快照含当时不存在的股票=非PIT前视)。
用法: python scripts/s0_pit_audit.py > logs/s0_pit_audit.log 2>&1
输出: logs/s0_pit_audit.csv
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
import numpy as np

from data.storage import load_daily, load_meta

PIT_PATH = "/data/quant_data_store/meta/csi800_universe_bs.parquet"
BS_DIR = Path("/data/quant_data_store/baostock/daily")
DELIST_CSV = "logs/t7_delist_candidates.csv"
OUT = "logs/s0_pit_audit.csv"


def main():
    pit = pd.read_parquet(PIT_PATH)
    pit["date"] = pd.to_datetime(pit["date"])
    print(f"PIT表: {len(pit)}个快照 {pit['date'].min().date()}~{pit['date'].max().date()}")
    # 快照间流转率
    print("\n=== 快照间成员流转率(真PIT应~5-15%/半年) ===")
    for i in range(1, len(pit)):
        prev = set(str(c) for c in pit.iloc[i-1]["codes"].split(","))
        cur = set(str(c) for c in pit.iloc[i]["codes"].split(","))
        churn = len(prev ^ cur) / 2 / len(cur) * 100
        print(f"  {pit.iloc[i-1]['date'].date()}→{pit.iloc[i]['date'].date()}: "
              f"换入{len(cur-prev)} 换出{len(prev-cur)} 流转{churn:.1f}%")

    # 2019快照的前视检验
    first = pit.iloc[0]
    first_codes = [str(c) for c in first["codes"].split(",")]
    first_date = first["date"]
    fwd, idx_codes = [], []
    for c in first_codes:
        p = BS_DIR / f"{c}.parquet"
        if p.exists():
            try:
                bs = pd.read_parquet(p, columns=["date"])
                bd = pd.to_datetime(bs["date"])
                if bd.min() > first_date:
                    fwd.append((c, str(bd.min().date())))
            except Exception:
                pass
    print(f"\n=== 2019-06-28快照前视检验 ===")
    print(f"  快照中 baostock 首日 > 快照日 的代码: {len(fwd)}只")
    for c, d in fwd[:15]:
        print(f"    {c} 首日{d} (快照日之后才上市→快照含当时不存在的股票)")

    # 最新快照800代码全分类
    last = pit.iloc[-1]
    last_codes = [str(c) for c in last["codes"].split(",")]
    print(f"\n=== 最新快照 {last['date'].date()} {len(last_codes)}只分类 ===")
    delist = set()
    try:
        dl = pd.read_csv(DELIST_CSV)
        dl["code"] = dl["code"].astype(str).str.zfill(6)
        delist = set(dl["code"])
    except Exception as e:
        print(f"  (退市表读取失败: {e})")

    meta = load_meta("stock_info_full")
    meta_names = dict(zip(meta["code"].astype(str).str.zfill(6), meta["name"])) \
        if not meta.empty else {}

    rows = []
    cat = {}
    for c in last_codes:
        bs_path = BS_DIR / f"{c}.parquet"
        bs_first = bs_last = None
        if bs_path.exists():
            try:
                bs = pd.read_parquet(bs_path, columns=["date"])
                bd = pd.to_datetime(bs["date"])
                bs_first, bs_last = str(bd.min().date()), str(bd.max().date())
            except Exception:
                pass
        # 本地覆盖
        local_any = False
        try:
            d = load_daily(c, "2026-01-01", "2026-08-28")
            local_any = not d.empty
        except Exception:
            pass

        if c not in meta_names:
            cls = "指数代码混入"
        elif c in delist:
            cls = "退市"
        elif c.startswith(("920", "430", "83", "87")):
            cls = "北交所(baostock归档多无覆盖)"
        elif bs_first is None and c not in meta_names:
            cls = "无任何档案(代码变更?)"
        elif bs_first is None:
            cls = "baostock无文件但meta有档"
        elif not local_any and bs_first and bs_first >= "2025-12-01":
            cls = "次新(上市晚于本地覆盖尾)"
        elif not local_any:
            cls = "纯数据缺失(baostock有/本地无→需回填)"
        else:
            cls = "本地有数据(非幽灵)"
        cat[cls] = cat.get(cls, 0) + 1
        rows.append({"code": c, "name": meta_names.get(c, ""),
                     "bs_first": bs_first or "", "bs_last": bs_last or "",
                     "local_2026": local_any, "class": cls})
    for k, v in sorted(cat.items(), key=lambda kv: -kv[1]):
        print(f"  {k}: {v}只 ({v/len(last_codes)*100:.1f}%)")
    pd.DataFrame(rows).to_csv(OUT, index=False, encoding="utf-8-sig")
    print(f"\n落盘: {OUT}")


if __name__ == "__main__":
    main()
