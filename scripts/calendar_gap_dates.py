"""2026 日级缺口频次诊断（只读，2026-10-25）。

回答：coverage scan 的 3500 只候选，是同几日期的全局洞，还是个股停牌/个股洞？
口径：对每只股票，仅在其 [first_bar,last_bar] 内统计缺失日；按日累计缺失只数。
不改数据；只输出频次，供人工核停牌/独立源确认。
"""
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import pandas as pd
from loguru import logger

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from data.storage import load_daily, load_meta
from backtest_return_attribution import load_blacklist

OUT = ROOT / "logs" / "calendar_gap_dates_20261025.json"


def main():
    meta = load_meta("stock_info_full")
    blacklist = load_blacklist()
    codes = [str(c).zfill(6) for c in meta["code"].tolist()
             if str(c).zfill(6) not in blacklist]
    sh = load_daily("SH000001", "2026-01-01", "2026-10-23")
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10]))
    miss = Counter()
    active = Counter()
    n_codes = 0
    codes_any = codes_ge5 = codes_ge20 = 0
    for i, c in enumerate(codes):
        d = load_daily(c, "2026-01-01", "2026-10-23")
        if d is None or len(d) == 0:
            continue
        n_codes += 1
        dates = sorted(set(pd.to_datetime(d["date"]).astype(str).str[:10]))
        first, last = dates[0], dates[-1]
        have = set(dates)
        cm = 0
        for x in cal:
            if first <= x <= last:
                active[x] += 1
                if x not in have:
                    miss[x] += 1
                    cm += 1
        codes_any += int(cm > 0)
        codes_ge5 += int(cm >= 5)
        codes_ge20 += int(cm >= 20)
        if i % 1000 == 0:
            print(f"  {i}/{len(codes)}", flush=True)
    rows = []
    for x in cal:
        rows.append({"date": x, "missing": miss[x], "active": active[x],
                     "missing_pct": round(miss[x] / active[x], 4) if active[x] else None})
    df = pd.DataFrame(rows).sort_values("missing", ascending=False)
    df.to_csv(ROOT / "logs" / "calendar_gap_dates_20261025.csv", index=False)
    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "n_codes": n_codes, "calendar_days": len(cal),
        "codes_with_any_missing": int(codes_any),
        "codes_ge_5_missing_days": int(codes_ge5),
        "codes_ge_20_missing_days": int(codes_ge20),
        "top_dates": df.head(20).to_dict("records"),
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print("->", OUT)


if __name__ == "__main__":
    main()
