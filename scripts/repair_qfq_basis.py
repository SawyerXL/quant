"""
qfq 基准断裂修复（2026-09-07）— 30只股在年份边界(qfq因子重锚定)处
出现±80%~+3800%假跳(如美的 2019-01-02 -83.5% / 2025-01-02 +3831%)。
基准断裂段内收益正确、边界日错误——美的必在TOP30池, 边界日直接污染
回测组合收益(§6.8消融也带着此污染跑, 各臂一致故四臂对比未暴露)。
修法沿用 fix_dirty_prices.py 先例: 新浪qfq整段重拉(实测内部零>50%跳变),
整体覆盖各年parquet(不merge)。带年份覆盖安全检查(新浪缺年则跳过该股)。
**单位归一内置**: 写库前 volume/amount ÷10^4 对齐主导口径(万股/万元),
否则修复过的票在成交额排名虚高10^4倍污染TOP30池(2026-09-07实测教训,
见记忆 amount-unit-scale-mix)。修复后必验: TOP30成交额排名像真实市场。
用法: python scripts/repair_qfq_basis.py
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, '/root/quant')
import pandas as pd

from loguru import logger
from data.source import get_source
from data.storage import load_meta, _daily_path

SCAN_LOG = "logs/dirty_scan_full_20260907.log"
START, END = "2005-01-01", "2026-09-04"
# 已人工确认的真借壳重组(新浪内部跳变=无涨跌幅首日真实行情, 不修)
KNOWN_LEGIT = {"000020", "000035", "000403"}


def dirty_codes() -> list:
    codes = []
    for line in open(SCAN_LOG, encoding="utf-8"):
        m = re.match(r"\s*(\d{6}): \d+次脏跳", line)
        if m:
            codes.append(m.group(1))
    return sorted(set(codes) - KNOWN_LEGIT)


def main():
    src = get_source()
    codes = dirty_codes()
    print(f"待修复 {len(codes)} 只: {codes}")
    fixed, failed, skipped = [], [], []
    for code in codes:
        df = src.get_daily(code, START, END)
        if df is None or df.empty or "close" not in df.columns:
            failed.append(code)
            print(f"  {code}: 重拉失败(空)", flush=True)
            continue
        c = pd.to_numeric(df["close"], errors="coerce")
        if (c.pct_change().abs() > 0.5).any():
            failed.append(code)
            print(f"  {code}: 新浪源内部仍有>50%跳变, 不覆盖(需人工查)", flush=True)
            continue
        # 年份覆盖安全检查: 新浪缺年则跳过, 防丢历史
        new_years = {y for y in pd.to_datetime(df["date"]).dt.year}
        old_years = {int(p.stem.split(".")[0].replace("_", ""))
                     for p in Path("data_store/daily").glob("*/")
                     if (p / f"{code}.parquet").exists()}
        missing = old_years - new_years
        if missing:
            skipped.append((code, sorted(missing)))
            print(f"  {code}: 新浪缺年份{missing}, 跳过(防丢历史)", flush=True)
            continue
        # 单位归一(2026-09-07 教训, 铁律): 库主导口径 volume=万股/amount=万元,
        # 新浪原始是股/元——原样覆盖会让该票成交额虚高10^4倍霸占TOP30池排名
        for col in ("volume", "amount"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce") / 1e4
        for year, grp in df.groupby(pd.to_datetime(df["date"]).dt.year):
            grp.to_parquet(_daily_path(code, int(year)), index=False)
        fixed.append(code)
        print(f"  {code}: 覆盖完成({len(df)}行, 单位已归一万股/万元)", flush=True)

    print(f"\n完成: 修复{len(fixed)} 失败{len(failed)} 跳过{len(skipped)}")


if __name__ == "__main__":
    main()
