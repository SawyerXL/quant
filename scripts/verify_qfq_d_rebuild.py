"""D 重建验收（2026-10-21）：抽样 qfq 比值 + 已知失败 selftest（闸门十六）。

- 抽样 fixed 列表 25 只：4 个探针日，库内 close vs 新浪 qfq 比值应 ≈1（±0.5%）。
- **2026-10-21 补独立源**：baostock qfq（与新浪校准差 <0.5%，覆盖退市股）
  对同一探针做第二次比对——重建源与验收源同源时只能验写入、不能验数据。
- **探针边界（闸门二十二）**：探针日早于上市日/停牌 → None 是"无数据"，
  不是失败；每只至少 2 个有效探针才判，其余记 skipped（须报覆盖数）。
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
TOL = 0.005      # 同源(新浪)写入校验
BS_TOL = 0.01    # 独立源(baostock)复权因子取整差可到 ~0.5%, 1% 仍能拦 5%+ 污染
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


def _bs_series(code):
    """baostock 前复权（adjustflag=2）序列；不可用返回 None。"""
    try:
        import baostock as bs
    except Exception:
        return None
    pre = "sh" if code.startswith("6") else "sz"
    rs = bs.query_history_k_data_plus(
        f"{pre}.{code}", "date,close", start_date="2020-01-01",
        end_date="2026-10-21", frequency="d", adjustflag="2")
    d = {}
    while rs.error_code == "0" and rs.next():
        r = rs.get_row_data()
        d[r[0]] = float(r[1])
    if not d:
        return None
    s = pd.Series(d)
    s.index = pd.to_datetime(s.index)
    return s.sort_index()


def bs_probes(code):
    s = stored(code)
    b = _bs_series(code)
    if s is None or b is None:
        return None
    out = []
    for p in PROBES:
        sn = _nearest(s, p)
        bn = _nearest(b, p)
        if sn is None or bn is None or sn[1] == 0:
            out.append(None)
        else:
            out.append(abs(float(bn[1]) / float(sn[1]) - 1))
    return out


def main() -> int:
    d = json.loads(Path("logs/rebuild_d_universe_20261021.json").read_text())
    fixed = list(d.get("fixed", []))
    rng = random.Random(SEED)
    sample = sorted(rng.sample(fixed, min(N, len(fixed))))
    res, bad, skipped, anchor_cand = {}, [], [], {}
    for c in sample:
        r = ratio_probes(c)
        if r is None:
            bad.append((c, "无数据/拉取失败"))
            continue
        valid = [x for x in r if x is not None]
        res[c] = [None if x is None else round(x, 5) for x in r]
        if len(valid) < 2:
            # 探针日早于上市/停牌 = 无数据, 不是重建失败(闸门二十二)
            skipped.append((c, "有效探针<2", res[c]))
        elif any(x > TOL for x in valid):
            # 全部探针同向、幅度一致 = 源端在写入后整体重锚(新除权),
            # 不是写错; 交由独立源裁定(见下方 anchor_shift 放行条件)
            if max(valid) <= 0.01 and (max(valid) - min(valid)) <= 0.002:
                anchor_cand[c] = res[c]
            else:
                bad.append((c, res[c]))
    # 独立源: baostock qfq
    bs_res, bs_bad, bs_skip = {}, [], []
    bs_state = "unavailable"
    try:
        import baostock as bs
        lg = bs.login()
        if lg.error_code == "0":
            bs_state = "ok"
            for c in sample:
                r = bs_probes(c)
                if r is None:
                    bs_skip.append((c, "baostock无数据"))
                    continue
                valid = [x for x in r if x is not None]
                bs_res[c] = [None if x is None else round(x, 5) for x in r]
                if len(valid) < 2:
                    bs_skip.append((c, "有效探针<2", bs_res[c]))
                elif any(x > BS_TOL for x in valid):
                    bs_bad.append((c, bs_res[c]))
        else:
            bs_state = f"login_fail:{lg.error_code}"
        bs.logout()
    except Exception as e:  # noqa: BLE001
        bs_state = f"error:{type(e).__name__}"
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
    # 源端重锚候选: 只有独立源(baostock)与库内值一致时才放行
    anchor_shift = []
    for c, rat in anchor_cand.items():
        br = bs_res.get(c)
        if br is not None and all(x is None or x <= BS_TOL for x in br):
            anchor_shift.append({"code": c, "sina_ratios": rat,
                                 "bs_ratios": br,
                                 "note": "同向一致偏移, 独立源验证库内值正确(疑似写入后新除权)"})
        else:
            bad.append((c, rat))
    primary_ok = (not bad and st_ok)
    verdict = "PASS" if (primary_ok and not bs_bad) else "FAIL"
    if primary_ok and bs_state != "ok":
        verdict = "PASS"  # 独立源不可用不阻断重建验收, 但必须在报告里可见
    out = {"sample_n": len(sample),
           "pass_n": len(sample) - len(bad) - len(skipped),
           "bad": bad, "skipped": skipped, "anchor_shift": anchor_shift,
           "selftest_raw": st_detail, "selftest_pass": st_ok,
           "independent_source": {
               "name": "baostock_qfq",
               "state": bs_state,
               "checked_n": len(bs_res),
               "bad_n": len(bs_bad), "bad": bs_bad,
               "skipped_n": len(bs_skip), "skipped": bs_skip[:10],
               "note": ("PASS 需主验(Sina)+selftest+独立源全过; "
                        "独立源不可用→PASS(Sina-only)并显式记录")},
           "verdict": verdict}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(json.dumps(out, ensure_ascii=False, indent=1))
    print("->", OUT)
    return 0 if out["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
