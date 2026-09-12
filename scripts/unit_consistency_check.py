"""
单位一致性检查 (2026-09-09 用户定顺序第3步) — 两条断言:
  ①截面绝对量级: 全市场日成交额中位数 ∈ [1e7, 1e9] 元
    = 库口径(万元) [1e3, 1e5] 万元——单峰但整体偏移 10^4 时立即触发
  ②逐股时序连续性: 同票相邻有效日成交额比值 < 100——
    抓 10^4 跳变, 无论全库还是单股(2026-06 切换当天本应报警)
检查最近 N 个交易日全库截面 + 抽样逐股时序; 违规非零退出。
归一完成且每日链跑稳后挂 cron(建议 18:10 日报链前)。
用法: python scripts/unit_consistency_check.py [--days 5]
"""
import sys, argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd

from data.storage import load_daily, load_meta
from backtest_return_attribution import load_blacklist

DAILY_DIR = Path("/data/quant_data_store/daily")


def check_cross_section(dates):
    """断言①: 逐日全市场 amount 中位数 ∈ [1e3, 1e5] 万元"""
    viol = []
    for dt in dates:
        vals = []
        for f in (DAILY_DIR / "2026").glob("*.parquet"):
            if f.stem.startswith(("SH", "SZ")):
                continue
            try:
                d = pd.read_parquet(f, columns=["date", "amount"])
            except Exception:
                continue
            d = d[d["date"].astype(str).str[:10] == dt]
            if d.empty:
                continue
            a = pd.to_numeric(d["amount"], errors="coerce")
            a = a[(a > 0)]
            if len(a):
                vals.append(float(a.median()))
        if not vals:
            continue
        med = float(np.median(vals))
        if not (1e3 <= med <= 1e5):
            viol.append((dt, med))
    return viol


def check_temporal(sample_codes, recent_days):
    """断言②: 抽样逐股相邻有效日比值 <100"""
    viol = []
    for c in sample_codes:
        d = load_daily(c, "2026-01-01", "2026-12-31")
        if d.empty:
            continue
        d = d.sort_values("date")
        a = pd.to_numeric(d["amount"], errors="coerce")
        prev = None
        for v in a.tolist():
            if v is None or v <= 0:
                continue
            if prev is not None:
                ratio = max(v, prev) / min(v, prev)
                if ratio >= 100:
                    viol.append((c, ratio))
            prev = v
    return viol


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=5)
    args = ap.parse_args()
    # 最近 N 个交易日(上证指数日历)
    sh = load_daily("SH000001", "2026-01-01", "2026-12-31")
    cal = sorted(sh["date"].astype(str).str[:10].tolist())
    dates = cal[-args.days:]
    print(f"检查 {len(dates)} 个交易日截面 + 时序抽样")

    v1 = check_cross_section(dates)
    print(f"断言①截面量级: {'✓' if not v1 else '✗'} "
          + (f"{v1}" if v1 else ""))
    meta = load_meta("stock_info_full")
    bl = load_blacklist()
    codes = [str(c).zfill(6) for c in meta["code"].tolist()
             if str(c).zfill(6) not in bl]
    import random
    random.seed(11)
    sample = random.sample(codes, 200)
    v2 = check_temporal(sample, dates)
    print(f"断言②时序连续性(抽样200只): {'✓' if not v2 else '✗'} "
          + (f"{len(v2)}处跳变, 示例{v2[:5]}" if v2 else ""))
    if v1 or v2:
        print("⚠️ 单位一致性违规")
        sys.exit(1)
    print("单位一致性通过")


if __name__ == "__main__":
    main()
