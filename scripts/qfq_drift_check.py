"""qfq 漂移门（P1，2026-10-22）。

问题：日更只追加当日 bar；新除权/分红后，源端 qfq 会把**全史**重锚，
而库内旧史仍是 D 重建 as_of 的锚 → 近端 vol20/MA10 会出现幻影跳变。
002250 实证：库内 10-21 close=13.76，新鲜新浪 qfq 10-21=13.66（−0.73%），
四日探针同向一致——不是写错，是源端重锚。

本脚本**只读**：对 QMT 股票持仓 + 成交额 TOP-N 候选池，比较库内 close 与
新鲜源 qfq 在最近共同日的比值；最近 5 个共同日同向一致偏移 >tol 标
`anchor_shift`（疑似新除权），非一致标 `mismatch`（疑似数据问题）。
**不写库、不自动重锚**——重锚需公告确认后走 D 重建脚本（备份+验收）。

用法：
  python scripts/qfq_drift_check.py --top 30
  python scripts/qfq_drift_check.py --codes 002250,600519
  python scripts/qfq_drift_check.py --codes 002250 --out logs/qfq_drift_002250.json
"""
import argparse
import glob
import json
import signal
import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from data.source import get_source
from data.storage import load_daily, load_meta
from backtest_data import get_top_n_by_turnover

TOL = 0.003
SPREAD = 0.002
LOOKBACK_DAYS = 45


def _handler(signum, frame):
    raise TimeoutError("source fetch timeout 90s")


def classify(ratios, tol: float = TOL, spread: float = SPREAD) -> str:
    """同向一致偏移=源端重锚；非一致=数据问题。"""
    v = [float(x) for x in ratios if x is not None]
    if not v:
        return "no_data"
    if abs(v[-1]) <= tol:
        return "ok"
    if (max(v) - min(v)) <= spread and (all(x < 0 for x in v) or all(x > 0 for x in v)):
        return "anchor_shift"
    return "mismatch"


def _holdings() -> list[str]:
    """QMT 最新持仓中的股票代码（CB/ETF 不算；股票腿 0 时为空）。"""
    try:
        files = sorted(glob.glob("logs/qmt_positions_*.json"))
        if not files:
            return []
        j = json.loads(Path(files[-1]).read_text(encoding="utf-8"))
        pos = j.get("positions", j.get("data", j))
        if isinstance(pos, dict):
            codes = list(pos.keys())
        elif isinstance(pos, list):
            codes = [x.get("code") or x.get("证券代码") for x in pos if isinstance(x, dict)]
        else:
            codes = []
        meta = load_meta("stock_info_full")
        good = set(meta["code"].astype(str).str.zfill(6)) if meta is not None and not meta.empty else set()
        return sorted({str(c).split(".")[0].zfill(6) for c in codes if c} & good)
    except Exception:
        return []


def _pool(top: int) -> list[str]:
    """与策略同源的成交额 TOP-N（get_top_n_by_turnover 唯一实现）。"""
    meta = load_meta("stock_info_full")
    if meta is None or meta.empty:
        return []
    codes = meta["code"].astype(str).str.zfill(6).tolist()
    today = date.today().isoformat()
    start = (pd.Timestamp(today) - pd.Timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    frames = {}
    for c in codes:
        try:
            d = load_daily(c, start, today)
            if d is None or d.empty or "amount" not in d:
                continue
            d = d.copy()
            d["date"] = pd.to_datetime(d["date"])
            s = pd.to_numeric(d.set_index("date")["amount"], errors="coerce").dropna()
            if len(s) >= 10:
                frames[c] = s
        except Exception:
            continue
    if not frames:
        return []
    ap = pd.DataFrame(frames).sort_index()
    return [str(c).zfill(6) for c in get_top_n_by_turnover(ap, len(ap) - 1, top)]


def _check(code: str, src, today: str) -> dict:
    stored = load_daily(code, "2025-01-01", today)
    if stored is None or stored.empty:
        return {"code": code, "status": "no_stored"}
    s = stored.copy()
    s["date"] = pd.to_datetime(s["date"])
    s = s.set_index("date")["close"].astype(float).sort_index()
    signal.signal(signal.SIGALRM, _handler)
    signal.alarm(90)
    try:
        q = src.get_daily(code, "2025-01-01", today)
    except Exception as e:  # noqa: BLE001
        return {"code": code, "status": "error", "err": str(e)[:80]}
    finally:
        signal.alarm(0)
    if q is None or q.empty:
        return {"code": code, "status": "no_source"}
    q = q.copy()
    q["date"] = pd.to_datetime(q["date"])
    q = q.set_index("date")["close"].astype(float).sort_index()
    common = s.index.intersection(q.index)
    if len(common) < 5:
        return {"code": code, "status": "no_common", "n": int(len(common))}
    last = common[-5:]
    ratios = [float(q.loc[d] / s.loc[d] - 1) for d in last]
    return {"code": code, "status": classify(ratios),
            "last_date": str(common[-1].date()),
            "stored": round(float(s.loc[common[-1]]), 4),
            "source": round(float(q.loc[common[-1]]), 4),
            "ratio_last": round(ratios[-1], 5),
            "ratios": [round(x, 5) for x in ratios]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--codes", default="")
    ap.add_argument("--tol", type=float, default=TOL)
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    today = date.today().isoformat()
    src = get_source()
    if args.codes:
        codes = [x.strip().zfill(6) for x in args.codes.split(",") if x.strip()]
        pool, holdings = [], []
    else:
        holdings = _holdings()
        pool = _pool(args.top)
        codes = sorted(set(holdings) | set(pool))
    print(f"{today} 检查 {len(codes)} 只 (持仓{len(holdings)}/池{len(pool)})", flush=True)
    results = []
    for c in codes:
        try:
            r = _check(c, src, today)
        except Exception as e:  # noqa: BLE001
            r = {"code": c, "status": "error", "err": str(e)[:80]}
        if r.get("status") not in ("ok",):
            r["status"] = r.get("status")
        results.append(r)
        if r.get("status") in ("anchor_shift", "mismatch"):
            print(f"  ⚠ {c} {r['status']} last={r.get('ratio_last')} ratios={r.get('ratios')}", flush=True)
    flags = [r["code"] for r in results
             if r.get("status") in ("anchor_shift", "mismatch")]
    out = {"ts": today, "tol": args.tol,
           "n_checked": len(results), "pool_n": len(pool),
           "holdings_n": len(holdings), "flags": flags,
           "no_write": True,
           "note": "只读；重锚需公告确认后走 D 重建(备份+验收)，本脚本不写库",
           "results": results}
    outp = Path(args.out or f"logs/qfq_drift_check_{today}.json")
    outp.parent.mkdir(parents=True, exist_ok=True)
    outp.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(f"checked={len(results)} flags={len(flags)} -> {outp}", flush=True)
    print(json.dumps({"flags": flags, "pool_n": len(pool),
                      "holdings_n": len(holdings)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
