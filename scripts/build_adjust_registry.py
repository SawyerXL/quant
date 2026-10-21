"""复权口径登记表（C 方案第一步，2026-10-21，只读+落元数据，不改价格）。

来源：
- `data_store/backup_qfq_rebuild_20261011/` 的 651 只 = 10-11 A 方案已重建
  → basis=qfq (source=pit800_rebuild_20261011)。
- `logs/audit_adjust_convention_20261011.csv` 的 1120 只：pit800 已 qfq 的
  记为 qfq；非 pit800 抽样按审计分类（raw/mixed/legacy）。
- 601059/601198 = 源端尾部缺口残留 → raw_tail_gap。
- 其余全库 = **unknown_unaudited**（不是 qfq！）；北交所单列 unknown_bse。
产出 `data_store/meta/adjust_basis_registry.parquet` + `_meta.json`。
用法：python scripts/build_adjust_registry.py
"""
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from data.storage import load_meta

BACKUPS = [
    (Path("data_store/backup_qfq_rebuild_20261011"), "pit800_rebuild_20261011"),
    (Path("data_store/backup_qfq_rebuild_20261021"), "d_rebuild_20261021"),
]
AUDIT = Path("logs/audit_adjust_convention_20261011.csv")
OUT = Path("data_store/meta/adjust_basis_registry.parquet")
META = Path("data_store/meta/adjust_basis_registry_meta.json")
# 中金公司换股吸并东兴/信达，2026-10-13 终止上市；源端 09-14 为最后
# 真实 bar，09-15 起停牌——不是数据缺口，无需 qfq 重建（MCP 公告独立确认）
DELISTED_MERGER = {"601059": "信达证券(CICC换股吸并,20261013终止上市)",
                   "601198": "东兴证券(CICC换股吸并,20261013终止上市)"}

MAP = {"qfq": "qfq", "raw": "raw",
       "混合(raw→qfq)": "mixed", "混合(qfq→raw)": "mixed",
       "旧as-of/其他": "legacy_asof", "拉取失败": "unknown_pull_fail"}


def main() -> int:
    rebuilt = {}
    for bak, src in BACKUPS:
        if bak.exists():
            for f in bak.rglob("*.parquet"):
                rebuilt[f.stem] = src
    audit = pd.read_csv(AUDIT, dtype={"code": str})
    audit["code"] = audit["code"].str.zfill(6)
    audit_map = dict(zip(audit["code"], audit["分类"]))
    all_codes = load_meta("stock_info_full")["code"].astype(str).str.zfill(6)
    # 并集: 当前库 + 审计集 + 重建集 + 终止上市(不在 stock_info_full)
    universe = set(all_codes) | set(audit_map) | set(rebuilt) | set(DELISTED_MERGER)
    rows = []
    for c in sorted(universe):
        if c in rebuilt:
            basis, src = "qfq", rebuilt[c]
        elif c in DELISTED_MERGER:
            basis, src = "delisted_merger", "mcp_announcements_20261013/20261016"
        elif c in audit_map:
            cls = audit_map[c]
            basis = MAP.get(cls, "unknown_other")
            src = "audit_adjust_convention_20261011"
        elif c.startswith(("4", "8", "92")):
            basis, src = "unknown_bse", "not_audited_sina_unsupported"
        else:
            basis, src = "unknown_unaudited", "not_audited"
        rows.append({"code": c, "basis": basis, "source": src})
    df = pd.DataFrame(rows)
    df.to_parquet(OUT, index=False)
    counts = df["basis"].value_counts().to_dict()
    meta = {
        "as_of": "2026-10-21",
        "sources": {
            "pit800_rebuild": "data_store/backup_qfq_rebuild_20261011 (651 codes)",
            "d_rebuild": f"data_store/backup_qfq_rebuild_20261021 "
                         f"({'present' if Path('data_store/backup_qfq_rebuild_20261021').exists() else 'absent/not-run'})",
            "audit": "logs/audit_adjust_convention_20261011.csv (1120 codes, probes 2020-2026)",
            "delisted_merger": "MCP公告: 中金换股吸并东兴/信达, 20261013终止上市",
        },
        "n_codes": int(len(df)),
        "counts": {k: int(v) for k, v in counts.items()},
        "rule": ("basis 是标签不是修复；unknown_* 不得当 qfq 用；"
                 "qfq=库内旧史已按新浪 qfq 重建或审计判定一致；"
                 "raw/mixed/legacy=旧史未修；"
                 "delisted_merger=换股吸并/终止上市, 源端最后bar=真实停牌边界, 无需qfq。"),
        "caveats": [
            "审计探针 as_of=2026-10-09；A重建 2026-10-11；D重建 2026-10-21(453只, 见备份目录)",
            "北交所(4/8/92)新浪不覆盖，未审计",
            "689009 CDR 新浪拉取失败，未单列（不在重建集）",
            "601059/601198: 中金换股吸并、2026-10-13终止上市; 源端止于09-14为真实停牌边界, 库内09-15~09-30为停牌重复行, 不重建",
            "loaders 尚未强制读本表；C 方案第二步=接入负载路径/健康行",
        ],
        "built_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    META.write_text(json.dumps(meta, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, indent=1))
    print("->", OUT, META)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
