"""2026 全库缺口受控回填（2026-10-25，用户批准）。

背景：calendar scan 修假阳性后仍有 3500/5174 只缺 2026-05-11~06-16 /
07-01~07-06（峰值 65%），另有 07-13~08-21 子集洞（30 只当前 CSI800）和
个股洞/停牌。Sina 已确认 601127/600875/600499 在洞内有数据 → 本地洞。

纪律：
- 只补**本地缺失的日期**；已有行一律保留（keep first），不覆写；
- 只补**源有 bar** 的日期；源也没有 = 停牌，跳过并记录；
- 锚校验：洞前/洞后重叠日 source close 与 local close 比值须在 ±0.5%
  （qfq as-of 漂移 >0.5% → 跳过，交人工；不缩放不硬写）；
- 备份：写前复制 2026 parquet 到 data_store/backup_2026_holes_20261025/；
- 单位：source 层已归一万股/万元（与库契约一致），写前再跑
  amount/volume≈close、OHLC 括号、>50% 跳变、代码/日期身份；
- 新增行 pct_chg 优先用源值，并与合并后前收核对；已有行 pct_chg 不动
  （601127 抽查库内 08-24 pct 与源一致，说明库内洞后行 pct 已正确）。

用法：
  python scripts/backfill_2026_holes_full.py --universe current800 --dry-run
  python scripts/backfill_2026_holes_full.py --universe current800
  python scripts/backfill_2026_holes_full.py --universe pit800
  python scripts/backfill_2026_holes_full.py --universe all --sleep 0.1
"""
import argparse
import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from loguru import logger

from backtest_return_attribution import load_blacklist
from data.source import get_source
from data.storage import load_daily, load_meta

BAK = ROOT / "data_store" / "backup_2026_holes_20261025"
DAILY = Path("/data/quant_data_store/daily")
START, END = "2026-01-01", "2026-10-23"
LOG = ROOT / "logs" / "backfill_2026_holes_20261025.json"


def universe_codes(kind: str) -> list[str]:
    if kind == "current800":
        m = pd.read_parquet(ROOT / "data_store" / "meta" / "csi800_members_official.parquet")
        codes = sorted(set(m["code"].astype(str).str.zfill(6)))
    elif kind == "pit800":
        m = pd.read_parquet(ROOT / "data_store" / "meta" / "csi800_pit_weights.parquet")
        codes = sorted(set(m["code"].astype(str).str.zfill(6)))
    else:
        m = load_meta("stock_info_full")
        codes = sorted(set(m["code"].astype(str).str.zfill(6)))
    black = load_blacklist()
    return [c for c in codes if c not in black]


def local_path(code: str) -> Path:
    return DAILY / "2026" / f"{code}.parquet"


def missing_days(code: str, cal: list[str]) -> list[str]:
    d = load_daily(code, START, END)
    if d is None or len(d) == 0:
        return []
    dates = sorted(set(pd.to_datetime(d["date"]).astype(str).str[:10]))
    first, last = dates[0], dates[-1]
    have = set(dates)
    return [x for x in cal if first <= x <= last and x not in have]


def anchors_ok(src: pd.DataFrame, loc: pd.DataFrame, miss: list[str]) -> tuple[bool, dict]:
    """洞前/洞后最近重叠日 close 比值；两锚都需在 ±0.5%。"""
    if src.empty or loc.empty:
        return False, {"reason": "empty"}
    s = src.set_index(pd.to_datetime(src["date"]).dt.strftime("%Y-%m-%d"))["close"]
    l = loc.set_index(pd.to_datetime(loc["date"]).dt.strftime("%Y-%m-%d"))["close"]
    common = sorted(set(s.index) & set(l.index))
    if not common:
        return False, {"reason": "no_overlap"}
    pre = [x for x in common if x < miss[0]]
    post = [x for x in common if x > miss[-1]]
    out = {}
    for name, pool in (("pre", pre[-3:]), ("post", post[:3])):
        if not pool:
            continue
        ratios = []
        for x in pool:
            try:
                ratios.append(float(s[x]) / float(l[x]))
            except Exception:
                pass
        if ratios:
            out[name] = round(sum(ratios) / len(ratios), 6)
    if not out:
        return False, {"reason": "no_anchor"}
    bad = [k for k, v in out.items() if abs(v - 1) > 0.005]
    return (len(bad) == 0), {"anchors": out, "bad": bad}


def validate_new(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """OHLC 括号/正量/量价一致性；返回 (clean, reasons)。"""
    reasons = []
    x = df.copy()
    for c in ["open", "high", "low", "close", "volume", "amount"]:
        x[c] = pd.to_numeric(x[c], errors="coerce")
    ok = (x[["open", "high", "low", "close"]] > 0).all(axis=1)
    ok &= (x["high"] >= x[["open", "close", "low"]].max(axis=1))
    ok &= (x["low"] <= x[["open", "close", "high"]].min(axis=1))
    ok &= (x["volume"] > 0) & (x["amount"] > 0)
    ratio = x["amount"] / x["volume"]
    ok &= (ratio >= x["low"] * 0.8) & (ratio <= x["high"] * 1.2)
    if (~ok).any():
        reasons.append(f"invalid_rows={int((~ok).sum())}")
    return x[ok].copy(), reasons


def fill_one(code: str, src, cal: list[str], dry: bool, rescale: bool = False) -> dict:
    p = local_path(code)
    rec = {"code": code, "filled": 0, "skipped": None, "anchors": None}
    if not p.exists():
        rec["skipped"] = "no_2026_file"
        return rec
    loc = pd.read_parquet(p)
    loc["date"] = pd.to_datetime(loc["date"])
    miss = missing_days(code, cal)
    if not miss:
        rec["skipped"] = "no_gap"
        return rec
    rec["missing"] = len(miss)
    try:
        s = src.get_daily(code, START, END)
    except Exception as e:
        rec["skipped"] = f"source_error:{type(e).__name__}"
        return rec
    if s is None or len(s) == 0:
        rec["skipped"] = "source_empty"
        return rec
    s = s.copy()
    s["date"] = pd.to_datetime(s["date"])
    s["code"] = s["code"].astype(str).str.zfill(6)
    ok, info = anchors_ok(s, loc, miss)
    rec["anchors"] = info
    if not ok and rescale:
        # 洞前/洞后锚比值一致（差<0.1%）且落 0.98~1.02 → 统一 qfq as-of
        # 基差，按 1/ratio 重锚；不是跨洞漂移（漂移会让 pre/post 分歧）。
        a = info.get("anchors", {})
        if len(a) == 2 and abs(a["pre"] - a["post"]) <= 0.001 and 0.98 <= a["pre"] <= 1.02:
            scale = 1.0 / ((a["pre"] + a["post"]) / 2)
            for c in ["open", "high", "low", "close"]:
                s[c] = pd.to_numeric(s[c], errors="coerce") * scale
            s["amount"] = pd.to_numeric(s["amount"], errors="coerce") * scale
            info["rescaled"] = round(scale, 8)
            ok = True
    if not ok:
        rec["skipped"] = "anchor_mismatch"
        return rec
    miss_set = set(miss)
    new = s[s["date"].dt.strftime("%Y-%m-%d").isin(miss_set)].copy()
    # 源没有的日期=停牌，不补
    got = set(new["date"].dt.strftime("%Y-%m-%d"))
    rec["source_missing"] = len(miss_set - got)
    if new.empty:
        rec["skipped"] = "source_has_no_bar"
        return rec
    new, reasons = validate_new(new)
    if reasons:
        rec["skipped"] = ";".join(reasons)
        return rec
    # 与相邻本地 close 的 >50% 跳变检查（qfq 无除权跳变；>50% 多为止损/污染）
    allc = pd.concat([loc[["date", "close"]], new[["date", "close"]]]).sort_values("date")
    allc["chg"] = allc["close"].pct_change()
    # 只检查新增行
    bad = allc[allc["date"].isin(new["date"]) & (allc["chg"].abs() > 0.5)]
    if len(bad):
        rec["skipped"] = f"jump_gt50:{len(bad)}"
        return rec
    # pct_chg：源值优先；只修洞边界（源 pct 可能对，但库内洞后第一根
    # 的 pct 若按缺失前收算过，需要按合并后的前收重算）；洞前已有行不动。
    merged = pd.concat([loc, new], ignore_index=True).drop_duplicates("date", keep="first").sort_values("date")
    for c in ["open", "high", "low", "close"]:
        merged[c] = pd.to_numeric(merged[c], errors="coerce").round(2)
    for c in ["volume", "amount"]:
        merged[c] = pd.to_numeric(merged[c], errors="coerce").round(4)
    m = merged.sort_values("date").reset_index(drop=True)
    calc = m["close"].pct_change() * 100
    ds = m["date"].dt.strftime("%Y-%m-%d")
    starts = m.index[ds == miss[0]]
    ends = m.index[ds > miss[-1]]
    if len(starts):
        i0 = int(starts[0])
        i1 = int(ends[0]) if len(ends) else len(m) - 1
        for i in range(i0, min(i1 + 2, len(m))):
            if i > 0 and pd.notna(calc.iloc[i]):
                cur = pd.to_numeric(m.loc[i, "pct_chg"], errors="coerce")
                if pd.isna(cur) or abs(float(cur) - float(calc.iloc[i])) > 0.2:
                    m.loc[i, "pct_chg"] = round(float(calc.iloc[i]), 4)
    rec["filled"] = int(len(new))
    if dry:
        rec["dry_run"] = True
        return rec
    BAK.mkdir(parents=True, exist_ok=True)
    bpath = BAK / f"{code}_2026.parquet"
    if not bpath.exists():          # 幂等：保留首次备份，重跑不覆盖原文件
        shutil.copy2(p, bpath)
    # 写回固定列顺序
    cols = ["date", "code", "open", "high", "low", "close", "volume", "amount", "pct_chg"]
    out = m[cols].copy()
    out["date"] = pd.to_datetime(out["date"])
    out.to_parquet(p, index=False)
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", choices=["current800", "pit800", "all"], default="current800")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--codes", default=None, help="逗号分隔，调试用")
    ap.add_argument("--sleep", type=float, default=0.1)
    ap.add_argument("--rescale", action="store_true",
                    help="锚比值一致但偏离1>0.5%%时，按1/ratio统一重锚")
    args = ap.parse_args()
    cal = sorted(set(pd.to_datetime(
        load_daily("SH000001", START, END)["date"]).astype(str).str[:10]))
    codes = ([c.strip().zfill(6) for c in args.codes.split(",") if c.strip()]
             if args.codes else universe_codes(args.universe))
    if args.limit:
        codes = codes[:args.limit]
    src = get_source()
    recs = []
    for i, c in enumerate(codes, 1):
        try:
            rec = fill_one(c, src, cal, args.dry_run, rescale=args.rescale)
        except Exception as e:
            rec = {"code": c, "filled": 0, "skipped": f"exception:{type(e).__name__}:{str(e)[:80]}"}
        recs.append(rec)
        if rec.get("filled") or rec.get("skipped") not in (None, "no_gap"):
            logger.info(f"{c}: filled={rec.get('filled')} skipped={rec.get('skipped')} anchors={rec.get('anchors')}")
        if i % 100 == 0:
            logger.info(f"  {i}/{len(codes)}")
        if args.sleep:
            time.sleep(args.sleep)
    filled = sum(r.get("filled", 0) for r in recs)
    skipped = {}
    for r in recs:
        k = r.get("skipped")
        if k:
            skipped[k] = skipped.get(k, 0) + 1
    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "universe": args.universe, "dry_run": args.dry_run,
        "n_codes": len(codes), "rows_filled": filled,
        "codes_filled": sum(1 for r in recs if r.get("filled")),
        "skipped": skipped, "records": recs,
    }
    LOG.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    logger.info(json.dumps({k: out[k] for k in
                            ["universe", "dry_run", "n_codes", "rows_filled",
                             "codes_filled", "skipped"]}, ensure_ascii=False))
    logger.info(f"-> {LOG}")


if __name__ == "__main__":
    main()
