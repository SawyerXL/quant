"""今日 bar 单位验收（判据锁于 09-13/14 清单 + 闸门十七增强）。

四条判据（任一不过 = 单位可疑，失败分支=停 ingestion/回滚/p0）：
  ① 锚定票比值: 600519/000001/300750 今日 amount ÷ 09-11 amount
     ∈ [0.3, 3.0]
  ② 量纲自洽: amount(万元)÷volume(万股) ≈ close, 误差 <5%
  ③ 全市场 amount 总和 ∈ [3000亿, 3万亿]
  ④ 锚点逐位: tests/unit_ground_truth.json 对照, 差异=0
  ⑤ (闸门十七, 2026-09-18 增) 单票绝对量级哨兵: 全库扫描
     单票日额 >100亿元 计数——比值检查对双字段同倍失效(920 实锤),
     绝对量级是更基础的防线; 计数>0 = 失败分支
日更完成后跑(17:00 更新 ~17:50 落地)。输出机器值+判定一行。
用法: python scripts/daily_bar_acceptance.py
"""
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd

from data.storage import load_daily, load_meta

ANCHORS = ["600519", "000001", "300750"]
BASE_DATE = "2026-09-11"
MAX_STOCK_AMOUNT_WAN = 1_000_000.0   # 100亿元(万元单位), 闸门十七哨兵
TOTAL_LO, TOTAL_HI = 3000e8, 3e12    # 全市场总额带(元)


def main():
    today = pd.Timestamp.now().strftime("%Y-%m-%d")
    checks = []

    # ① 锚定票比值
    ratios = {}
    for c in ANCHORS:
        d = load_daily(c, "2026-09-01", today)
        if d.empty:
            checks.append((f"①锚定票 {c}", False, "缺数据"))
            continue
        d["date"] = pd.to_datetime(d["date"])
        d = d.set_index("date").sort_index()
        a = pd.to_numeric(d["amount"], errors="coerce")
        if today not in a.index or BASE_DATE not in a.index:
            checks.append((f"①锚定票 {c}", False, "缺今日或基准bar"))
            continue
        r = a[today] / a[BASE_DATE]
        ratios[c] = round(float(r), 3)
        checks.append((f"①锚定票 {c}={ratios[c]:.3f}",
                      0.3 <= r <= 3.0, ""))

    # ② 量纲自洽
    errs = {}
    for c in ANCHORS:
        d = load_daily(c, today, today)
        if d.empty:
            checks.append((f"②量纲 {c}", False, "缺bar"))
            continue
        row = d.iloc[0]
        est = float(row["amount"]) / float(row["volume"])
        err = abs(est - float(row["close"])) / float(row["close"])
        errs[c] = round(float(err), 4)
        checks.append((f"②量纲 {c} err={errs[c]*100:.2f}%", err < 0.05, ""))

    # ③ 全市场总额 + ⑤ 单票哨兵(一次遍历)
    meta = load_meta("stock_info_full")
    total, n, over_cap = 0.0, 0, []
    for c in meta["code"].tolist():
        try:
            d = load_daily(c, today, today)
            if d.empty:
                continue
            a = float(pd.to_numeric(d.iloc[0]["amount"], errors="coerce") or 0)
            total += a
            n += 1
            if a > MAX_STOCK_AMOUNT_WAN:
                over_cap.append((c, a))
        except Exception:
            pass
    total_yuan = total * 1e4
    checks.append((f"③全市场 {total_yuan/1e8:.0f}亿({n}票)",
                   TOTAL_LO <= total_yuan <= TOTAL_HI, ""))
    checks.append((f"⑤单票哨兵 >100亿: {len(over_cap)}只 {over_cap[:3]}",
                   len(over_cap) == 0, ""))

    # ④ 锚点逐位
    gt = json.loads(Path("tests/unit_ground_truth.json").read_text(
        encoding="utf-8"))
    n_ok = n_diff = 0
    for code, dates in gt["stocks"].items():
        for dt, exp in dates.items():
            d = load_daily(code, dt, dt)
            if d.empty:
                continue
            actual = float(d.iloc[0]["amount"])
            if actual == 0:
                continue
            if abs(actual - exp["amount"]) < 0.01:
                n_ok += 1
            else:
                n_diff += 1
    checks.append((f"④锚点 一致{n_ok} 差异{n_diff}", n_diff == 0, ""))

    passed = all(ok for _, ok, _ in checks)
    print(f"===== 今日 bar 验收 {today} =====")
    for name, ok, note in checks:
        print(f"  {'✓' if ok else '✗'} {name} {note}")
    print(f"判定: {'4/4+哨兵全过 → 单位正确' if passed else '失败分支: 停 ingestion+回滚+p0'}")


if __name__ == "__main__":
    main()
