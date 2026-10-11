"""主窗洞最后 2 只 (2026-10-11 第二版, 处置分级)。

为什么改: 第一版对 300274 全史覆写被闸②拦下后发现——库内 300274 旧史
(2019-2026.4) 是不复权原值(比值 vs qfq 低至 0.19, 送转累计因子 5x),
与库契约"qfq 库"不符。旧史口径是全库级 P0 问题, 不借补洞之名改口径
(闸门八/P0 紧迫性)。故分级:
  689009: 库内仅 2026-05-28 起 51 行(2020 上市以来旧史全缺) → 腾讯 qfq
         全史覆写(无旧史可冲突, 近期段比值精确 1.00000 作锚)。
  300274: 只填 7 个缺日(腾讯 qfq 值), 不动全史; 与库内邻bar 比值 0.5%
         级台阶已记录(库内 5-7 月段本身是旧 as-of, 台阶<库内既有噪声)。
  688072: 7/1~7/6 无任何源有数据 = 停牌, 非数据洞, 终验豁免。
用法: python scripts/backfill_holes_2026_fix3_tx.py
"""
import sys
import shutil
from pathlib import Path

import pandas as pd
import akshare as ak

sys.path.insert(0, str(Path(__file__).parent.parent))
from loguru import logger
from data.storage import load_daily, save_daily

FULL_OVERWRITE = ["689009"]          # 库内旧史缺失/全缺, 全史覆写
FILL_ONLY = ["300274"]               # 只填缺日, 旧史口径不动
MISSING_DAYS = {"300274": ["2026-05-11", "2026-05-12", "2026-05-13",
                           "2026-05-15", "2026-05-21", "2026-06-05",
                           "2026-06-09"]}
PULL_HI = "2026-10-09"               # 工件 as_of 冻结(闸门十三)
RECENT_LO = "2026-09-20"             # 近期段锚(日更链已更新=当前口径)
BAK = Path("data_store/backup_hole_backfill_20261010")


def _sym(code: str) -> str:
    return ("sh" if code.startswith(("6", "9", "5")) else "sz") + code


def _pull(code: str) -> pd.DataFrame:
    raw = ak.stock_zh_a_hist_tx(symbol=_sym(code), start_date="20190101",
                                end_date=PULL_HI.replace("-", ""), adjust="qfq")
    if raw is None or raw.empty:
        raise RuntimeError("腾讯返回空")
    df = raw.copy()
    df["date"] = pd.to_datetime(df["date"])
    return df.dropna(subset=["date", "close"]).sort_values("date")


def main():
    fixed, failed = [], []
    BAK.mkdir(parents=True, exist_ok=True)
    for code in FULL_OVERWRITE:
        try:
            df = _pull(code)
            stored = load_daily(code, "2019-01-01", PULL_HI)
            s = stored.set_index(pd.to_datetime(stored["date"]))["close"]
            fresh = df.set_index("date")["close"]
            common = s.index.intersection(fresh.index)
            common = common[common >= pd.Timestamp(RECENT_LO)]
            r = (fresh.loc[common] / s.loc[common]).dropna()
            if len(r) < 3 or (r - 1).abs().max() > 0.005:
                logger.warning(f"{code} 近期段锚不过(偏离{(r-1).abs().max():.4f}), 跳过")
                failed.append(code)
                continue
            for yr in range(2019, 2027):
                p = Path(f"data_store/daily/{yr}/{code}.parquet")
                if p.exists():
                    (BAK / str(yr)).mkdir(parents=True, exist_ok=True)
                    shutil.copy2(p, BAK / str(yr) / f"{code}.parquet")
            for yr, grp in df.groupby(df["date"].dt.year):
                p = Path(f"data_store/daily/{yr}")
                p.mkdir(parents=True, exist_ok=True)
                grp.to_parquet(p / f"{code}.parquet", index=False)
            fixed.append(code)
            print(f"{code}: 腾讯qfq全史覆写 {len(df)}行 (近期锚 {len(r)}日偏离"
                  f"{(r-1).abs().max():.5f})", flush=True)
        except Exception as e:
            logger.warning(f"{code} 失败: {e}")
            failed.append(code)

    for code in FILL_ONLY:
        try:
            df = _pull(code)
            df = df[df["date"].isin(pd.to_datetime(MISSING_DAYS[code]))]
            if df.empty:
                logger.warning(f"{code}: 缺日无腾讯数据")
                failed.append(code)
                continue
            # 邻bar一致性(仅记录, 不做0.5%硬闸: 库内5-7月段本身旧as-of)
            stored = load_daily(code, "2026-05-06", "2026-06-12")
            s = stored.set_index(pd.to_datetime(stored["date"]))["close"]
            fresh = df.set_index("date")["close"]
            nbr = s.index.intersection(fresh.index)
            note = ""
            if len(nbr):
                dev = (fresh.loc[nbr] / s.loc[nbr] - 1).abs().max()
                note = f"邻bar最大偏离{dev:.4f}"
            # 备份当日涉及年份文件 + save_daily(日期去重+跳变守卫)
            for yr in df["date"].dt.year.unique():
                p = Path(f"data_store/daily/{yr}/{code}.parquet")
                if p.exists():
                    (BAK / str(yr)).mkdir(parents=True, exist_ok=True)
                    shutil.copy2(p, BAK / str(yr) / f"{code}.parquet")
            for _, row in df.iterrows():
                save_daily(code, pd.DataFrame([row]))
            fixed.append(code)
            print(f"{code}: 腾讯qfq补缺 {len(df)} 日 ({note})", flush=True)
        except Exception as e:
            logger.warning(f"{code} 失败: {e}")
            failed.append(code)

    print(f"\n处理 {len(fixed)} 只 / 失败 {len(failed)} 只")
    if failed:
        print(f"失败(待人工): {failed}")


if __name__ == "__main__":
    main()
