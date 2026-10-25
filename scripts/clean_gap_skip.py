"""清理 gap_skip 中已由回填补上的 (code,date)（2026-10-25）。

背景：`daily_data_update.py` 对同一 (code,date) 拉 3 次不到就写入
`data_store/meta/gap_skip.json` 永不重试；而日更只修 recent window
（GAP_LOOKBACK），**长于窗口的 outage 会永久留洞**。2026-07-13~08-21
的 30 只当前 CSI800 即此类（不在 gap_skip，纯粹是窗口外无人回看）。
回填后必须把 gap_skip 中“现在已有 bar”的键清掉，否则下一次同窗口修复
会被旧白名单挡住。

只删“本地已有该日 bar”的键；本地仍缺（真停牌/未补）保留。
"""
import json
import shutil
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from loguru import logger

from data.storage import load_daily

P = ROOT / "data_store" / "meta" / "gap_skip.json"
BAK = ROOT / "data_store" / "meta" / "gap_skip_backup_20261025.json"
OUT = ROOT / "logs" / "gap_skip_clean_20261025.json"


def main():
    if not P.exists():
        logger.warning(f"{P} 不存在")
        return
    d = json.loads(P.read_text())
    by_code = {}
    for k in d:
        code, _, day = k.partition(":")
        if len(day) == 10 and day.startswith("2026-"):
            by_code.setdefault(code, set()).add(day)
    keep, drop, no_data = {}, [], []
    for code, days in by_code.items():
        x = load_daily(code, "2026-01-01", "2026-10-23")
        have = set(pd.to_datetime(x["date"]).astype(str).str[:10]) if len(x) else set()
        for day in days:
            k = f"{code}:{day}"
            if day in have:
                drop.append(k)
            else:
                keep[k] = d[k]
                if not have:
                    no_data.append(k)
    # 非 2026 键原样保留
    for k, v in d.items():
        if k not in keep and k not in drop:
            keep[k] = v
    if not BAK.exists():
        shutil.copy2(P, BAK)
    P.write_text(json.dumps(keep, ensure_ascii=False, indent=0))
    out = {"before": len(d), "after": len(keep), "dropped": len(drop),
           "dropped_sample": drop[:30], "kept_no_2026_data": len(no_data),
           "months_dropped": dict(Counter(x.split(":")[1][:7] for x in drop))}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    logger.info(json.dumps(out, ensure_ascii=False))
    logger.info(f"-> {OUT}")


if __name__ == "__main__":
    main()
