"""全库复权口径审计 (2026-10-11 立项, B3 结案时的 P0 发现: 库内复权混存)。

为什么: 600519/300274 库内旧史=不复权原值, 而库契约(源层 docstring
"全程前复权(qfq)")与 26 只 fix2 票近期段=当前 as-of qfq——混存且无档案
标记。日更链只拉当日 bar(当日 qfq==raw 不可分), 历史 bar 冻结在原摄取
口径。本审计逐票判定库内 close 口径, 出覆盖率+污染名单(闸门十四)。

方法: 每股拉新浪 qfq(库契约源), 与库内在 4 个探针日(近3月/约1年/约3年/
约6年, 库内取±3日最近bar)比 close 比值:
  |ratio-1|≤0.5% → 该探针 qfq 一致
  不符 → 再拉新浪 raw 判定 raw/其他(旧 as-of qfq 或异源)
分类: 全探针 qfq→qfq; 全探针 raw→raw; 近 qfq 远 raw→混合(raw→qfq);
  反之→混合(qfq→raw); 其余→其他(逐票记录)。北交所(4/8/92开头)新浪不
  覆盖, 单列不计入口径统计。
自检(闸门十六): 已知 raw 的 600519/300274 必须被检出(raw 或混合);
  已知 qfq 的 688411/689009(2026-10-11 覆写)必须判 qfq; 否则 SELFTEST_FAIL。
只读审计, 不写库。产出 logs/audit_adjust_convention_YYYYMMDD.csv + 屏显
覆盖率。用法: python scripts/audit_adjust_convention.py [--limit N]
"""
import sys
import argparse
import random
from pathlib import Path

import pandas as pd
import akshare as ak

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from loguru import logger
from data.storage import load_daily, load_meta

PROBES = ["2026-09-20", "2025-12-15", "2023-06-15", "2020-06-15"]
TOL = 0.005          # 比值偏离 1 的判定带(±0.5%)
SEED = 20261011      # 抽样种子(可复现)
SAMPLE_N = 300       # 全库抽样量(覆盖统计用)
SELFTEST_RAW = ["600519", "300274"]       # 已知 raw(必须检出)
SELFTEST_QFQ = ["688411"]                 # 已知 qfq(2026-10-11 覆写, 必须放行)
# 689009(2026-10-11 腾讯qfq覆写)不进自检: 新浪对CDR拉取失败, 其口径
# 已在 B3 用腾讯近期段比值 1.00000 验证, 审计输出中记为拉取失败已知项。


def _sym(code: str) -> str:
    return ("sh" if code.startswith(("6", "9", "5", "11")) else "sz") + code


def _sina(code: str, adjust: str) -> pd.Series | None:
    """新浪日线 close 序列(qfq 或 raw)。北交所/失败返回 None。"""
    try:
        df = ak.stock_zh_a_daily(symbol=_sym(code), start_date="19900101",
                                 end_date="20261009", adjust=adjust)
        if df is None or df.empty:
            return None
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
        return df.set_index("date")["close"].astype(float)
    except Exception:
        return None


def _stored(code: str) -> pd.Series | None:
    d = load_daily(code, "1990-01-01", "2026-10-09")
    if d is None or d.empty:
        return None
    d = d.copy()
    d["date"] = pd.to_datetime(d["date"])
    s = d.set_index("date")["close"].astype(float)
    return s[~s.index.duplicated(keep="last")].sort_index()


def _nearest(s: pd.Series, probe: str) -> tuple[str, float] | None:
    """库内探针日±3日最近bar。"""
    t = pd.Timestamp(probe)
    win = s[(s.index >= t - pd.Timedelta(days=3)) & (s.index <= t + pd.Timedelta(days=3))]
    if win.empty:
        return None
    delta = (win.index - t)
    i = int(__import__("numpy").argmin(__import__("numpy").abs(delta.values)))
    return str(win.index[i].date()), float(win.iloc[i])


def classify(probes: list) -> str:
    """probes: 每个探针的判定 'q'/'r'/'o'/'x'。

    只统计有判别力的探针(qfq≠raw 的日期, 其后发生过除权):
      x = 无数据/无判别力(近期无除权, qfq==raw 不可区分)
      o = qfq/raw 均不符 = 旧 as-of qfq 或异源(第三种口径)
    """
    qs = probes.count("q")
    rs = probes.count("r")
    os_ = probes.count("o")
    decisive = qs + rs
    if os_ and os_ >= decisive - os_:  # 旧as-of主导
        return "旧as-of/其他"
    if not decisive:
        return "不可判定(近期无除权)"
    if qs and rs:
        # 按近→远找切换方向: PROBES 时间降序
        seen = [p for p in probes if p in ("q", "r", "o")]
        first = next((p for p in seen if p in ("q", "r")), "x")
        return "混合(raw→qfq)" if first == "q" else "混合(qfq→raw)"
    if qs:
        return "qfq" if not os_ else "混合(旧as-of→qfq)"
    return "raw" if not os_ else "混合(旧as-of→raw)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只跑前N只(冒烟)")
    args = ap.parse_args()

    from backfill_holes_2026 import member_codes
    pit = sorted(member_codes())
    meta = load_meta("stock_info_full")
    all_codes = meta["code"].tolist() if not meta.empty else []
    rng = random.Random(SEED)
    sample = sorted(rng.sample([c for c in all_codes if c not in pit], min(SAMPLE_N, len(all_codes))))
    # 自检票置顶(闸门十六: 必须最先跑且必然在审计宇宙内)
    universe = list(SELFTEST_RAW) + list(SELFTEST_QFQ) + \
        [c for c in pit if c not in SELFTEST_RAW + SELFTEST_QFQ] + sample
    if args.limit:
        universe = universe[:args.limit]
    print(f"审计宇宙: pit800 {len(pit)} + 抽样 {len(sample)} + 自检 {len(SELFTEST_RAW+SELFTEST_QFQ)}"
          f" → 实跑 {len(universe)} 只 (as_of=2026-10-09)", flush=True)

    rows, counts = [], {"qfq": 0, "raw": 0, "混合(raw→qfq)": 0, "混合(qfq→raw)": 0,
                        "混合(旧as-of→qfq)": 0, "混合(旧as-of→raw)": 0,
                        "旧as-of/其他": 0, "不可判定(近期无除权)": 0, "拉取失败": 0}
    bj_skipped = 0
    for i, code in enumerate(universe):
        if code.startswith(("4", "8", "92")):
            bj_skipped += 1
            continue
        stored = _stored(code)
        qfq = _sina(code, "qfq")
        if qfq is None:
            counts["拉取失败"] += 1
            rows.append({"code": code, "分类": "拉取失败", "探针": ""})
            continue
        verdicts, detail = [], []
        need_raw = False
        for probe in PROBES:
            nb = _nearest(stored, probe) if stored is not None else None
            if nb is None:
                verdicts.append("x")
                detail.append(f"{probe}:库内无bar")
                continue
            d, sc = nb
            if d not in qfq.index:
                verdicts.append("x")
                detail.append(f"{probe}:新浪无bar")
                continue
            rq = sc / float(qfq.loc[d])
            if abs(rq - 1) <= TOL:
                verdicts.append("q?")
                detail.append(f"{probe}:qfq一致({rq:.4f})")
            else:
                verdicts.append("?")
                need_raw = True
                detail.append(f"{probe}:qfq不符({rq:.4f})")
        # 只要有一探针不符, 拉 raw 复核全部探针(判别力=该日 qfq≠raw)
        if need_raw:
            raw = _sina(code, "")
            for j, v in enumerate(verdicts):
                probe = PROBES[j]
                nb = _nearest(stored, probe)
                if nb is None:
                    verdicts[j] = "x"
                    continue
                d, sc = nb
                if raw is None or d not in raw.index or d not in qfq.index:
                    verdicts[j] = "x"
                    continue
                decisive = abs(float(qfq.loc[d]) / float(raw.loc[d]) - 1) > TOL
                rr = sc / float(raw.loc[d])
                if v == "?":
                    if abs(rr - 1) <= TOL:
                        verdicts[j] = "r"
                        detail[j] += f"|raw一致({rr:.4f})"
                    else:
                        verdicts[j] = "o"
                        detail[j] += f"|raw亦不符({rr:.4f})"
                elif v == "q?":
                    if not decisive:
                        verdicts[j] = "x"
                        detail[j] += "|该日qfq==raw无判别力"
                    else:
                        verdicts[j] = "q"
                else:
                    verdicts[j] = "x"
        else:
            verdicts = ["q" if v == "q?" else v for v in verdicts]
        kind = classify(verdicts)
        counts[kind] = counts.get(kind, 0) + 1
        rows.append({"code": code, "分类": kind, "探针": " | ".join(detail)})
        if (i + 1) % 100 == 0:
            print(f"进度 {i+1}/{len(universe)}, qfq={counts['qfq']} raw={counts['raw']} "
                  f"混合={counts['混合(raw→qfq)']+counts['混合(qfq→raw)']+counts['混合(旧as-of→qfq)']+counts['混合(旧as-of→raw)']} "
                  f"旧as-of={counts['旧as-of/其他']}", flush=True)

    out = pd.DataFrame(rows)
    out_path = Path("logs") / f"audit_adjust_convention_20261011.csv"
    out.to_csv(out_path, index=False, encoding="utf-8-sig")
    print(f"\n结果: {out_path} ({len(out)} 行)", flush=True)
    total_checked = sum(v for k, v in counts.items() if k != "拉取失败")
    print(f"口径统计(pit800+抽样, 已检 {total_checked} 只, 北交所跳过 {bj_skipped}, "
          f"拉取失败 {counts['拉取失败']}):", flush=True)
    for k in ["qfq", "raw", "混合(raw→qfq)", "混合(qfq→raw)", "混合(旧as-of→qfq)",
              "混合(旧as-of→raw)", "旧as-of/其他", "不可判定(近期无除权)"]:
        print(f"  {k}: {counts.get(k, 0)} ({counts.get(k,0)/max(total_checked,1):.1%})", flush=True)

    # 自检(闸门十六): 已知用例必须命中
    fails = []
    by_code = {r["code"]: r["分类"] for r in rows}
    for c in SELFTEST_RAW:
        if by_code.get(c) not in ("raw", "混合(raw→qfq)", "混合(qfq→raw)"):
            fails.append(f"{c} 应判raw/混合 实判{by_code.get(c)}")
    for c in SELFTEST_QFQ:
        if by_code.get(c) != "qfq":
            fails.append(f"{c} 应判qfq 实判{by_code.get(c)}")
    if fails:
        print("SELFTEST_FAIL: " + "; ".join(fails), flush=True)
    else:
        print("SELFTEST_PASS: 已知raw/qfq用例全部命中", flush=True)


if __name__ == "__main__":
    main()
