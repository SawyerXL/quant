"""pit800 复权口径重建 (2026-10-11, 用户裁定方案D第一步: 先决策宇宙).

为什么: 全库复权口径审计(2026-10-11)坐实 pit800 成员 79.3% 旧史=不复权
原值, 库契约 qfq 仅 25% 成立。本脚本把 pit800 中非 qfq 票全史重拉覆写
为当前 as-of 前复权(源层 get_daily: 东财→新浪降级, 单位归一=库契约)。
写前守卫=最新bar锚: 前复权在前沿恒等于原值, 拉取末 bar vs 库内末 bar
(±5日共同日) close 比值必须 ≈1(±0.5%), 否则跳过并报告——该锚对所有
口径形态(含 000001 旧as-of)通用成立。备份入 backup_qfq_rebuild_20261011/。
排除: 已 qfq 票(审计分类)与 689009(B3 已腾讯 qfq 覆写)。北交所新浪
不覆盖, 失败单独列表。只写 pit800 成员文件, 不碰其余。
用法: python scripts/rebuild_pit800_qfq.py
"""
import sys
import shutil
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from loguru import logger
from data.source import get_source
from data.storage import load_daily

END = "2026-10-09"   # 工件 as_of 冻结(闸门十三)
ANCHOR_LO = "2026-09-25"  # 最新bar锚窗口
BAK = Path("data_store/backup_qfq_rebuild_20261011")


def main():
    from backfill_holes_2026 import member_codes
    aud = pd.read_csv("logs/audit_adjust_convention_20261011.csv", dtype={"code": str})
    pit = set(member_codes())
    non_qfq = set(aud[aud["分类"] != "qfq"]["code"]) & pit
    non_qfq.discard("689009")   # B3 已腾讯 qfq 覆写(新浪不可拉)
    codes = sorted(non_qfq)
    print(f"重建宇宙: {len(codes)} 只 (pit800 中非 qfq, 已排 689009)", flush=True)

    src = get_source()
    BAK.mkdir(parents=True, exist_ok=True)
    fixed, skipped, failed = [], [], []
    for i, code in enumerate(codes):
        try:
            df = src.get_daily(code, "1990-01-01", END)
            if df is None or df.empty:
                failed.append((code, "拉取空"))
                continue
            df = df.copy()
            df["date"] = pd.to_datetime(df["date"])
            df = df.dropna(subset=["date", "close"]).sort_values("date")
            if df.empty:
                failed.append((code, "清洗后空"))
                continue
            # 前沿锚: 只比锚窗口内最后共同日(前复权在数据前沿恒等于原值,
            # 与库内口径形态无关——窗内更早日期会被新除权重缩放, MAX会误伤
            # 分红密集票; 2026-10-11 实测 13 只高分红央企被 MAX 误跳过,
            # 前沿偏离全部精确 0.00000 后覆写 11 只)
            stored = load_daily(code, ANCHOR_LO, END)
            ok_anchor = False
            if stored is not None and not stored.empty:
                s = stored.set_index(pd.to_datetime(stored["date"]))["close"]
                f = df.set_index("date")["close"]
                common = s.index.intersection(f.index)
                if len(common):
                    last = common[-1]
                    dev = abs(float(f.loc[last]) / float(s.loc[last]) - 1)
                    if dev <= 0.005:
                        ok_anchor = True
                    else:
                        logger.warning(f"{code} 前沿锚不过(偏离{dev:.4f}), 跳过")
                        skipped.append((code, f"前沿偏离{dev:.4f}"))
                        continue
                else:
                    logger.warning(f"{code} 锚窗口无共同日, 跳过")
                    skipped.append((code, "锚窗口无共同日"))
                    continue
            else:
                logger.warning(f"{code} 库内锚窗口无数据, 跳过")
                skipped.append((code, "库内锚窗口无数据"))
                continue
            # 备份 + 按年直写
            for yr in range(1990, 2027):
                p = Path(f"data_store/daily/{yr}/{code}.parquet")
                if p.exists():
                    (BAK / str(yr)).mkdir(parents=True, exist_ok=True)
                    shutil.copy2(p, BAK / str(yr) / f"{code}.parquet")
            for yr, grp in df.groupby(df["date"].dt.year):
                p = Path(f"data_store/daily/{yr}")
                p.mkdir(parents=True, exist_ok=True)
                grp.to_parquet(p / f"{code}.parquet", index=False)
            fixed.append(code)
            print(f"  {code}: 全史qfq覆写 {len(df)}行", flush=True)
        except Exception as e:
            logger.warning(f"{code} 失败: {e}")
            failed.append((code, str(e)[:80]))
        if (i + 1) % 100 == 0:
            print(f"进度 {i+1}/{len(codes)}, 覆写 {len(fixed)} 跳过 {len(skipped)} "
                  f"失败 {len(failed)}", flush=True)
    print(f"\n覆写 {len(fixed)} 只 / 锚跳过 {len(skipped)} 只 / 失败 {len(failed)} 只", flush=True)
    if skipped:
        print(f"锚跳过: {skipped[:20]}{'...' if len(skipped) > 20 else ''}", flush=True)
    if failed:
        print(f"失败: {failed[:20]}{'...' if len(failed) > 20 else ''}", flush=True)


if __name__ == "__main__":
    main()
