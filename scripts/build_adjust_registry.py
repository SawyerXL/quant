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

BACKUP = Path("data_store/backup_qfq_rebuild_20261011")
AUDIT = Path("logs/audit_adjust_convention_20261011.csv")
OUT = Path("data_store/meta/adjust_basis_registry.parquet")
META = Path("data_store/meta/adjust_basis_registry_meta.json")
TAIL_GAP = {"601059", "601198"}

MAP = {"qfq": "qfq", "raw": "raw",
       "混合(raw→qfq)": "mixed", "混合(qfq→raw)": "mixed",
       "旧as-of/其他": "legacy_asof", "拉取失败": "unknown_pull_fail"}


def main() -> int:
    rebuilt = {f.stem for f in BACKUP.rglob("*.parquet")}
    audit = pd.read_csv(AUDIT, dtype={"code": str})
    audit["code"] = audit["code"].str.zfill(6)
    audit_map = dict(zip(audit["code"], audit["分类"]))
    all_codes = load_meta("stock_info_full")["code"].astype(str).str.zfill(6)
    # 并集: 当前库 + 审计集 + 重建集 + 残留(601059/601198 不在 stock_info_full)
    universe = set(all_codes) | set(audit_map) | rebuilt | TAIL_GAP
    rows = []
    for c in sorted(universe):
        if c in rebuilt:
            basis, src = "qfq", "pit800_rebuild_20261011"
        elif c in TAIL_GAP:
            basis, src = "raw_tail_gap", "audit_20261011(residual)"
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
            "audit": "logs/audit_adjust_convention_20261011.csv (1120 codes, probes 2020-2026)",
        },
        "n_codes": int(len(df)),
        "counts": {k: int(v) for k, v in counts.items()},
        "rule": ("basis 是标签不是修复；unknown_* 不得当 qfq 用；"
                 "qfq=库内旧史已按新浪 qfq 重建或审计判定一致；"
                 "raw/mixed/legacy=旧史未修；raw_tail_gap=源端尾部缺口。"),
        "caveats": [
            "审计探针 as_of=2026-10-09；重建 2026-10-11；其后日更只追加当日 bar，不改变旧史口径",
            "北交所(4/8/92)新浪不覆盖，未审计",
            "689009 CDR 新浪拉取失败，未单列（不在重建集）",
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
