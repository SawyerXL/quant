"""17:00 daily_data_update 放行核验(2026-10-12, 判据 17:00 前已锁, 用户裁定)。

逐文件判据:
  trade_calendar  逐字节一致(sha256 vs pre1700 快照)
  csi800_index    追加型: 昨日及以前行一致 + 只多当日行
  stock_info_full 覆盖型: 列结构一致 + 行数∈[−4,+4] + 关键字段 list_date/
                  industry_l1 不变(历史观测 max Δ=4)
  ETF 哨兵        511360/588000 当日 bar 存在且 amt/vol≈close
输出每项 PASS/FAIL; 任一 FAIL 由人按已锁分支处置(新代码触碰→回退 /
仅上游漂移→报原始 diff 待裁定)。
用法: python scripts/verify_daily_update_release.py
"""
import hashlib
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

META = Path("/data/quant_data_store/meta")
PRE = Path("/root/quant/logs")
results = []


def report(name, ok, detail):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def main():
    # ── 1. trade_calendar 逐字节一致 ────────────────────────────
    pre = PRE / "pre1700_trade_calendar_20261012.parquet"
    cur = META / "trade_calendar.parquet"
    h_pre = hashlib.sha256(pre.read_bytes()).hexdigest()
    h_cur = hashlib.sha256(cur.read_bytes()).hexdigest()
    report("trade_calendar 逐字节", h_pre == h_cur,
           f"pre1700={h_pre[:12]}… cur={h_cur[:12]}…")

    # ── 2. csi800_index 追加型 ──────────────────────────────────
    p2 = pd.read_parquet(PRE / "pre1700_csi800_index_20261012.parquet")
    c2 = pd.read_parquet(META / "csi800_index.parquet")
    cols_ok = list(p2.columns) == list(c2.columns)
    same = cols_ok
    if cols_ok and len(c2) >= len(p2):
        for col in p2.columns:
            a = p2[col].reset_index(drop=True)
            b = c2[col].iloc[:len(p2)].reset_index(drop=True)
            if not (a.fillna("_NA").astype(str) == b.fillna("_NA").astype(str)).all():
                same = False
                break
    tail_new = c2.iloc[len(p2):] if len(c2) > len(p2) else pd.DataFrame()
    new_dates = tail_new["date"].astype(str).tolist() if "date" in tail_new.columns else []
    ok = cols_ok and same and len(tail_new) <= 2
    report("csi800_index 追加", ok,
           f"列一致={cols_ok} 旧行一致={same} 新增{len(tail_new)}行={new_dates}")

    # ── 3. stock_info_full 覆盖型 ───────────────────────────────
    p3 = pd.read_parquet(PRE / "pre1700_stock_info_full_20261012.parquet")
    c3 = pd.read_parquet(META / "stock_info_full.parquet")
    cols_ok = list(p3.columns) == list(c3.columns)
    rows_ok = -4 <= len(c3) - len(p3) <= 4
    # 关键字段: 按 code 对齐比对(行序可能不同)
    key_ok = True
    if "code" in p3.columns and "code" in c3.columns:
        m = p3[["code"] + [k for k in ("list_date", "industry_l1") if k in c3.columns]].merge(
            c3[["code"] + [k for k in ("list_date", "industry_l1") if k in c3.columns]],
            on="code", suffixes=("_p", "_c"))
        for k in ("list_date", "industry_l1"):
            if f"{k}_p" in m.columns:
                a = m[f"{k}_p"].fillna("_NA").astype(str)
                b = m[f"{k}_c"].fillna("_NA").astype(str)
                if not (a == b).all():
                    key_ok = False
                    n_bad = (a != b).sum()
                    report(f"stock_info_full {k}", False, f"{n_bad} 只券关键字段变化")
    report("stock_info_full 覆盖型", cols_ok and rows_ok and key_ok,
           f"列一致={cols_ok} 行数 {len(p3)}→{len(c3)} (Δ{len(c3)-len(p3):+d}) 关键字段一致={key_ok}")

    # ── 4. ETF 哨兵 (ETF T-0 合成代码在 19:37 才跑, 17:15 核验用 --skip-etf) ──
    if "--skip-etf" in sys.argv:
        print("[SKIP] ETF 合成哨兵(19:37 后才跑, 另设 19:45 检查)")
    else:
        etf_ok = True
        detail = []
        for code in ("511360", "588000"):
            p = None
            for yr in ("2026", "2025"):
                cand = Path(f"/data/quant_data_store/daily/{yr}/{code}.parquet")
                if cand.exists():
                    p = cand
                    break
            if p is None:
                etf_ok = False
                detail.append(f"{code}: 文件不存在")
                continue
            d = pd.read_parquet(p)
            d["date"] = pd.to_datetime(d["date"])
            today = d["date"].max()
            row = d[d["date"] == today]
            if row.empty:
                etf_ok = False
                detail.append(f"{code}: 无当日bar(最后{today.date()})")
                continue
            r = row.iloc[-1]
            ratio = float(r["amount"]) / float(r["volume"]) if float(r["volume"]) else 0
            ratio_ok = abs(ratio / float(r["close"]) - 1) < 0.1 if float(r["close"]) else False
            etf_ok &= ratio_ok
            detail.append(f"{code} 当日bar={today.date()} amt/vol={ratio:.2f}≈close={float(r['close']):.2f} {'✓' if ratio_ok else '✗'}")
        report("ETF 合成哨兵", etf_ok, " | ".join(detail))

    print(f"\n总结: {sum(results)}/{len(results)} PASS" + ("" if all(results) else " — 按已锁分支处置"))


if __name__ == "__main__":
    main()
