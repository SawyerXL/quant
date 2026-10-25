"""构建/刷新流通股本缓存（洗盘研究用，2026-10-25）。

宇宙 = `csi800_pit_weights` 的并集（PIT 季度成分，2019-01-31+）。
数据 = akshare `stock_value_em`（东财，2018-01-02 起），按票缓存到
`data_store/meta/float_shares/<code>.parquet`。

纪律：
- 失败要计数、要落盘，不静默当"无事件"（闸门十二）；
- 末行必须覆盖 as_of（事件窗口末端 2026-10-23）；
- 只做数据准备，不产出任何收益数字。
"""
import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from chip_structure import load_float_shares  # noqa: E402

WEIGHTS = ROOT / "data_store" / "meta" / "csi800_pit_weights.parquet"


def universe_codes() -> list[str]:
    w = pd.read_parquet(WEIGHTS)
    codes = sorted(set(w["code"].astype(str).str.zfill(6)))
    return codes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", default="2026-10-23")
    ap.add_argument("--sleep", type=float, default=0.15)
    ap.add_argument("--retry-rounds", type=int, default=2)
    args = ap.parse_args()

    codes = universe_codes()
    print(f"universe {len(codes)} codes, asof {args.asof}", flush=True)
    results = {}
    for rnd in range(args.retry_rounds + 1):
        todo = [c for c in codes if results.get(c) != "ok"]
        if rnd > 0:
            print(f"retry round {rnd}: {len(todo)}", flush=True)
            time.sleep(5)
        if not todo:
            break
        for i, code in enumerate(todo, 1):
            try:
                fs, status = load_float_shares(code, asof=args.asof)
                ok = status in ("fetched", "cache_ok") and fs is not None and len(fs) > 0
                results[code] = "ok" if ok else status
            except Exception as e:
                results[code] = f"error:{type(e).__name__}"
            if args.sleep:
                time.sleep(args.sleep)
            if i % 100 == 0:
                ok_n = sum(1 for v in results.values() if v == "ok")
                print(f"  {i}/{len(todo)} ok={ok_n}", flush=True)

    ok = [c for c, v in results.items() if v == "ok"]
    bad = {c: v for c, v in results.items() if v != "ok"}
    out = {
        "as_of": args.asof,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "universe": len(codes), "ok": len(ok), "fail": len(bad),
        "coverage": round(len(ok) / len(codes), 4) if codes else None,
        "fail_detail": dict(sorted(bad.items())),
    }
    p = ROOT / "logs" / f"float_cache_build_{args.asof}.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in out.items() if k != "fail_detail"}, ensure_ascii=False))
    print("fail sample:", list(bad.items())[:10])
    print("->", p)
    return 0 if len(bad) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
