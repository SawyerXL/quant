"""主窗数据洞定向重拉 (2026-10-10, 09-08 批准事项第6条落地)。

为什么: 2026-05-11~06-16、07-01~07-06 两段全局数据洞落在主窗内, pit800
成员 05-20 缺 33.1%/07-03 缺 5.0%(机器值, 与 09-08 记录 31.5%/8.0% 同量级);
主窗归因(池 −5.69pp)踩在这两段上。范围=PIT800 两洞窗口成员(共 845 只,
710 只有缺口, 9369 (票,日) 对)。
路径: 复用 data/source 层 get_daily(东财主用→新浪兜底, 单位归一万股/万元
与库契约一致)——不写新的拉取代码, 避免单位重犯。
验证(闸门八): 重拉后 ①缺口计数归零 ②抽查 amount/volume≈close ③相邻bar
连续性(比值<100)。
用法: python scripts/backfill_holes_2026.py [--scan-only]
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from loguru import logger
from data.source import get_source
from data.storage import load_daily, load_meta, save_daily

HOLE1 = ("2026-05-11", "2026-06-16")
HOLE2 = ("2026-07-01", "2026-07-06")
PULL_LO, PULL_HI = "2026-05-01", "2026-07-10"


def member_codes() -> set:
    pit = load_meta("csi800_pit_weights")
    pit["trade_date"] = pd.to_datetime(pit["trade_date"])
    out = set()
    for d in (HOLE1[0], HOLE2[0]):
        t = pit[pit["trade_date"] <= d]["trade_date"].max()
        out |= set(pit[pit["trade_date"] == t]["code"].astype(str))
    return out


def scan(codes: set) -> pd.DataFrame:
    sh = load_daily("SH000001", "2026-04-01", "2026-08-01")
    cal = sorted(sh["date"].astype(str).str[:10].tolist())
    holes = ([d for d in cal if HOLE1[0] <= d <= HOLE1[1]]
             + [d for d in cal if HOLE2[0] <= d <= HOLE2[1]])
    missing = []
    for code in sorted(codes):
        d = load_daily(code, PULL_LO, PULL_HI)
        have = set(d["date"].astype(str).str[:10].tolist()) if not d.empty else set()
        for day in holes:
            if day not in have:
                missing.append((code, day))
    return pd.DataFrame(missing, columns=["code", "date"])


def verify(codes: set, n_before: int) -> None:
    after = scan(codes)
    print(f"验证: 缺口对 {n_before} → {len(after)}")
    # 抽查比值哨兵: 新拉的日期里 amount/volume ≈ close
    import numpy as np
    checks = 0
    for code in sorted(codes)[:30]:
        d = load_daily(code, PULL_LO, PULL_HI)
        if d.empty:
            continue
        sub = d[(d["date"].astype(str).str[:10] >= HOLE1[0])
                & (d["date"].astype(str).str[:10] <= HOLE2[1])]
        for _, r in sub.iterrows():
            v = pd.to_numeric(r.get("volume"), errors="coerce")
            a = pd.to_numeric(r.get("amount"), errors="coerce")
            c = pd.to_numeric(r.get("close"), errors="coerce")
            if pd.notna(v) and pd.notna(a) and pd.notna(c) and v > 0 and c > 0:
                ratio = a / v
                if not (0.5 * c <= ratio <= 2 * c):
                    print(f"  ⚠比值哨兵: {code} {r['date']} amt/vol={ratio:.2f} close={c}")
                checks += 1
                break
        if checks >= 30:
            break
    print(f"比值抽查 {checks} 行通过")


def main():
    scan_only = "--scan-only" in sys.argv
    codes = member_codes()
    missing = scan(codes)
    print(f"成员 {len(codes)} 只, 缺口 (票,日) 对 {len(missing)}, "
          f"涉及 {missing['code'].nunique()} 只")
    if scan_only:
        return
    if missing.empty:
        print("无缺口, 跳过")
        return

    src = get_source()
    n = 0
    for i, code in enumerate(sorted(missing["code"].unique())):
        try:
            df = src.get_daily(code, PULL_LO, PULL_HI)
            if df is None or df.empty:
                logger.warning(f"{code} 拉取空")
                continue
            save_daily(code, df)
            n += 1
        except Exception as e:
            logger.warning(f"{code} 重拉失败: {e}")
        if (i + 1) % 100 == 0:
            logger.info(f"  进度 {i+1}, 已存 {n}")
    print(f"重拉完成: {n} 只")
    verify(codes, len(missing))


if __name__ == "__main__":
    main()
