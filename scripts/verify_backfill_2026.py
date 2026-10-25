"""2026 回填验收（2026-10-25）。

读 `logs/backfill_2026_holes_20261025.json` + 回填后数据，检查：
- 已填票 2026 缺失（停牌除外）、重复日期、排序；
- OHLC 括号、量额>0、>50% 日跳、pct_chg 与前收一致；
- amount/volume 与 close 的 qfq 固定因子是否稳定（混合单位会出现 1e4 台阶）；
- 残余缺口与停牌清单（供 gap_skip / 周度扫描白名单）。
只读，不写数据；输出 logs/backfill_verify_20261025.json。
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from loguru import logger

from data.storage import load_daily

CAL = None


def calendar():
    global CAL
    if CAL is None:
        d = load_daily("SH000001", "2026-01-01", "2026-10-23")
        CAL = sorted(set(pd.to_datetime(d["date"]).astype(str).str[:10]))
    return CAL


def check_code(code: str) -> dict:
    d = load_daily(code, "2026-01-01", "2026-10-23")
    if d is None or len(d) == 0:
        return {"code": code, "error": "no_data"}
    d = d.copy()
    d["date"] = pd.to_datetime(d["date"])
    ds = d["date"].dt.strftime("%Y-%m-%d")
    dup = int(ds.duplicated().sum())
    ordered = bool(ds.is_monotonic_increasing)
    first, last = ds.iloc[0], ds.iloc[-1]
    miss = [x for x in calendar() if first <= x <= last and x not in set(ds)]
    o = pd.to_numeric(d["open"], errors="coerce")
    h = pd.to_numeric(d["high"], errors="coerce")
    lo = pd.to_numeric(d["low"], errors="coerce")
    c = pd.to_numeric(d["close"], errors="coerce")
    v = pd.to_numeric(d["volume"], errors="coerce")
    a = pd.to_numeric(d["amount"], errors="coerce")
    ohlc_bad = int((~((o > 0) & (h > 0) & (lo > 0) & (c > 0))).sum())
    bracket_bad = int((~((h >= pd.concat([o, c, lo], axis=1).max(axis=1))
                         & (lo <= pd.concat([o, c, h], axis=1).min(axis=1)))).sum())
    vol_bad = int((~((v > 0) & (a > 0))).sum())
    chg = c.pct_change().abs()
    jump_bad = int((chg > 0.5).sum())
    # pct_chg 与前收一致性（源 pct 保留 4 位；容许 0.2%）
    pct = pd.to_numeric(d["pct_chg"], errors="coerce")
    calc = c.pct_change() * 100
    pct_bad = int(((pct - calc).abs() > 0.2).sum())
    # qfq/raw 固定因子：amount/volume = raw VWAP = qfq_close * factor
    factor = (a / v) / c.replace(0, np.nan)
    fmed = float(factor.median()) if factor.notna().any() else None
    fdev = float((factor / fmed - 1).abs().max()) if fmed and fmed > 0 else None
    # amount/volume 是 raw VWAP、close 是 qfq：日内 VWAP-close 差 5-10% 正常；
    # 只有 >50% 或 factor 中位数远离 1（1e4/1e-4 混单位）才算台阶。
    unit_step = bool((fdev is not None and fdev > 0.5)
                     or (fmed is not None and not (0.2 <= fmed <= 5)))
    return {
        "code": code, "rows": int(len(d)), "first": first, "last": last,
        "missing": len(miss), "dup": dup, "ordered": ordered,
        "ohlc_bad": ohlc_bad, "bracket_bad": bracket_bad, "vol_bad": vol_bad,
        "jump_bad": jump_bad, "pct_bad": pct_bad,
        "factor_median": None if fmed is None else round(fmed, 5),
        "factor_max_dev": None if fdev is None else round(fdev, 4),
        "unit_step": unit_step,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=str(ROOT / "logs" / "backfill_2026_holes_20261025.json"))
    ap.add_argument("--scan", default=str(ROOT / "logs" / "coverage_scan_20261025.csv"))
    ap.add_argument("--out", default=str(ROOT / "logs" / "backfill_verify_20261025.json"))
    args = ap.parse_args()
    b = json.loads(Path(args.json).read_text())
    filled = [r["code"] for r in b.get("records", []) if r.get("filled", 0) > 0]
    skipped = {}
    for r in b.get("records", []):
        k = r.get("skipped")
        if k:
            skipped.setdefault(k, []).append(r["code"])
    logger.info(f"filled codes {len(filled)}, skipped kinds {list(skipped)}")
    checks = [check_code(c) for c in filled]
    # missing 是残余（停牌/源失败），单独报告；bad 只列完整性异常
    bad = [x for x in checks if (x.get("dup", 0)
                                 or not x.get("ordered", True) or x.get("ohlc_bad", 0)
                                 or x.get("bracket_bad", 0) or x.get("vol_bad", 0)
                                 or x.get("jump_bad", 0) or x.get("pct_bad", 0) > 5
                                 or x.get("unit_step", False))]
    residual_missing = [x for x in checks if x.get("missing", 0) > 0]
    scan = pd.read_csv(args.scan, dtype={"code": str}) if Path(args.scan).exists() else pd.DataFrame()
    if len(scan):
        scan["code"] = scan["code"].str.zfill(6)
        scan["active_missing"] = pd.to_numeric(scan["active_missing"], errors="coerce")
        residual = scan[scan["active_missing"].fillna(0) > 0]
    else:
        residual = pd.DataFrame()
    out = {
        "n_filled_codes": len(filled), "n_checks": len(checks),
        "n_bad": len(bad), "bad": bad[:50],
        "n_residual_missing": len(residual_missing),
        "residual_missing": residual_missing[:50],
        "skipped_kinds": {k: len(v) for k, v in skipped.items()},
        "suspected_suspension": sorted(set(
            skipped.get("source_has_no_bar", []) + skipped.get("source_empty", []))),
        "residual_candidates": int(len(residual)),
        "residual_top": residual.head(30).to_dict("records") if len(residual) else [],
        "records": checks,
    }
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    logger.info(json.dumps({k: out[k] for k in
                            ["n_filled_codes", "n_bad", "n_residual_missing",
                             "skipped_kinds", "residual_candidates"]}, ensure_ascii=False))
    logger.info(f"-> {args.out}")


if __name__ == "__main__":
    main()
