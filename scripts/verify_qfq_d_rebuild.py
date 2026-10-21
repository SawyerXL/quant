"""D 重建验收（2026-10-21）：抽样 qfq 比值 + 已知失败 selftest（闸门十六）。

- 抽样 fixed 列表 25 只：4 个探针日，库内 close vs 新浪 qfq 比值应 ≈1（±0.5%）。
- 已知失败：registry 里 raw 的一只（未重建）应判 raw/mixed——若被判 qfq，
  说明验收器失效（SELFTEST_FAIL）。
输出 logs/verify_qfq_d_rebuild_20261021.json。
用法：python scripts/verify_qfq_d_rebuild.py
"""
import json
import random
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from audit_adjust_convention import _sina, _nearest  # noqa: E402
from data.storage import load_daily

PROBES = ["2026-09-20", "2025-12-15", "2023-06-15", "2020-06-15"]
TOL = 0.005
SEED = 20261021
N = 25
OUT = Path("logs/verify_qfq_d_rebuild_20261021.json")


def stored(code):
    try:
        d = load_daily(code, "1990-01-01", "2026-10-21")
        if d.empty:
            return None
        d["date"] = pd.to_datetime(d["date"])
        return d.set_index("date")["close"].astype(float)
    except Exception:
        return None


def ratio_probes(code):
    s = stored(code)
    q = _sina(code, "qfq")
    if s is None or q is None:
        return None
    out = []
    for p in PROBES:
        sn = _nearest(s, p)
        qn = _nearest(q, p)
        if sn is None or qn is None or sn[1] == 0:
            out.append(None)
        else:
            out.append(abs(float(qn[1]) / float(sn[1]) - 1))
    return out


def main() -> int:
    d = json.loads(Path("logs/rebuild_d_universe_20261021.json").read_text())
    fixed = list(d.get("fixed", []))
    rng = random.Random(SEED)
    sample = sorted(rng.sample(fixed, min(N, len(fixed))))
    res, bad = {}, []
    for c in sample:
        r = ratio_probes(c)
        if r is None:
            bad.append((c, "无数据/拉取失败"))
            continue
        ok = all(x is not None and x <= TOL for x in r)
        res[c] = [None if x is None else round(x, 5) for x in r]
        if not ok:
            bad.append((c, res[c]))
    # selftest: 已知 raw（registry raw，未重建）必须至少一个远端探针偏离
    reg = pd.read_parquet("data_store/meta/adjust_basis_registry.parquet")
    raw_codes = reg.loc[reg["basis"] == "raw", "code"].astype(str).tolist()
    st_ok = False
    st_detail = None
    for st_code in raw_codes[:5]:
        r = ratio_probes(st_code)
        if r is None:
            continue
        dev = any(x is not None and x > TOL for x in r)
        st_detail = {"code": st_code,
                     "ratios": [None if x is None else round(x, 4) for x in r],
                     "detected_raw": dev}
        if dev:
            st_ok = True
            break
    out = {"sample_n": len(sample), "pass_n": len(sample) - len(bad),
           "bad": bad, "selftest_raw": st_detail,
           "selftest_pass": st_ok,
           "verdict": "PASS" if (not bad and st_ok) else "FAIL"}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(json.dumps(out, ensure_ascii=False, indent=1))
    print("->", OUT)
    return 0 if out["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
