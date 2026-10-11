"""主窗洞残留票全史覆写 (2026-10-10, 守卫拒行的复权as-of交界票).

为什么: 第一轮窗口补丁对 9369→261 对缺口清零, 残留=库内洞前段是旧
as-of 前复权、新拉是当前 as-of 前复权, 交界比值越过守卫(>50%)被整窗
拒绝——守卫正确, 但补丁路径走不通。处置=全史 qfq 重拉+按年直写
(fix_dirty_prices.py 前例), 写前必须过**交界预检**: 新拉序列与库内
洞后近期 bar(同 as-of 段)比值≈1(±0.5%), 过检才覆写, 不过检跳过报告。
备份: data_store/backup_hole_backfill_20261010/ 各票原文件。
用法: python scripts/backfill_holes_2026_fix2.py
"""
import sys
import shutil
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from loguru import logger
from data.source import get_source
from data.storage import load_daily

HOLE1 = ("2026-05-11", "2026-06-16")
HOLE2 = ("2026-07-01", "2026-07-06")
PULL_LO, PULL_HI = "2026-05-01", "2026-07-10"
POST_LO = "2026-07-07"   # 洞后近期段(应为当前as-of, 作交界锚)
BAK = Path("data_store/backup_hole_backfill_20261010")


def remaining() -> list:
    from backfill_holes_2026 import scan, member_codes
    miss = scan(member_codes())
    return sorted(miss["code"].unique()) if not miss.empty else []


def main():
    codes = remaining()
    print(f"残留缺口票: {len(codes)} 只")
    if not codes:
        print("无残留, 结束")
        return

    src = get_source()
    fixed, skipped, failed = [], [], []
    BAK.mkdir(parents=True, exist_ok=True)
    for i, code in enumerate(codes):
        try:
            df = src.get_daily(code, "2019-01-01", PULL_HI)
            if df is None or df.empty:
                failed.append(code)
                continue
            df = df.copy()
            df["date"] = pd.to_datetime(df["date"])
            df = df.dropna(subset=["date"]).sort_values("date")
            # 交界预检: 洞后近期段 新拉 vs 库内 比值≈1(±0.5%)
            # 2026-10-11 修复: 原条件 ratio.abs().max()>0.005 恒真(|ratio|≈1),
            # 30只交界实为一致(dev=0)被整体错杀; 正确语义=偏离1超过0.5%才拦
            stored = load_daily(code, POST_LO, PULL_HI)
            if not stored.empty:
                stored = stored.set_index(pd.to_datetime(stored["date"]))["close"]
                fresh = df.set_index("date")["close"]
                common = stored.index.intersection(fresh.index)
                if len(common) >= 3:
                    ratio = (fresh.loc[common] / stored.loc[common]).dropna()
                    dev = (ratio - 1).abs().max()
                    if dev > 0.005:
                        logger.warning(f"{code} 交界预检不过(偏离1超{dev:.3f}), 跳过")
                        skipped.append(code)
                        continue
            # 备份原文件
            for yr in range(2019, 2027):
                p = Path(f"data_store/daily/{yr}/{code}.parquet")
                if p.exists():
                    (BAK / str(yr)).mkdir(parents=True, exist_ok=True)
                    shutil.copy2(p, BAK / str(yr) / f"{code}.parquet")
            # 按年直写(全史当前as-of qfq)
            for yr, grp in df.groupby(df["date"].dt.year):
                p = Path(f"data_store/daily/{yr}")
                p.mkdir(parents=True, exist_ok=True)
                grp.to_parquet(p / f"{code}.parquet", index=False)
            fixed.append(code)
            print(f"  {code}: 全史覆写 {len(df)}行", flush=True)
        except Exception as e:
            logger.warning(f"{code} 失败: {e}")
            failed.append(code)
    print(f"\n覆写 {len(fixed)} 只 / 预检跳过 {len(skipped)} 只 / 失败 {len(failed)} 只")
    if skipped:
        print(f"跳过(待人工): {skipped}")
    # 终验
    from backfill_holes_2026 import scan, member_codes
    miss = scan(member_codes())
    print(f"终验: 缺口对 {len(miss)}, 涉及 {miss['code'].nunique()} 只"
          if not miss.empty else "终验: 缺口归零 ✅")


if __name__ == "__main__":
    main()
