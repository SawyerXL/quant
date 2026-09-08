"""
T7 退市股残留对照 (2026-09-08 overnight) — 只审计不改数据。

三个问题:
  Q1 库内是否存在退市日之后的残留数据(退市后仍有的日线=错误数据)?
  Q2 退市股在回测引擎中的出清价语义(0 / 前值 / 最后交易价)?
  Q3 退市股是否会被推进成交额TOP池(退市整理期成交额异常放大)?

退市股名单来源(无单一退市日期表, 用三源交叉):
  ① smallcap_universe_bs 14个PIT快照: 前期快照出现、后期快照永久消失
     → disappeared_after(快照区间)
  ② baostock/daily 5779个文件 vs stock_info_full(5558) 差值 → 已退市/更名
  ③ 本地库最后日期 vs baostock最后日期: 本地晚于baostock最后日=残留嫌疑
输出: logs/t7_delist_audit.log + logs/t7_delist_candidates.csv
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd

BS_DIR = Path("data_store/baostock/daily")
LOCAL_DIR = Path("data_store/daily")
OUT_LOG = Path("logs/t7_delist_audit.log")
OUT_CSV = Path("logs/t7_delist_candidates.csv")


def snap_disappeared():
    """快照消失名单: {(code, last_seen_snapshot_date)}"""
    u = pd.read_parquet("data_store/meta/smallcap_universe_bs.parquet")
    snaps = sorted(u["date"].unique())
    mem = {s: set(str(c) for c in u.loc[u["date"] == s, "codes"].iloc[0].split(","))
           for s in snaps}
    out = {}
    for i, s in enumerate(snaps[:-1]):
        for c in mem[s]:
            if all(c not in mem[later] for later in snaps[i + 1:]):
                out[c] = s
    return out


def local_last_date(code):
    last = None
    for y in range(1990, 2027):
        p = LOCAL_DIR / str(y) / f"{code}.parquet"
        if not p.exists():
            continue
        try:
            d = pd.read_parquet(p, columns=["date"])
            if len(d):
                last = pd.to_datetime(d["date"].iloc[-1])
        except Exception:
            pass
    return last


def bs_last_date(code):
    p = BS_DIR / f"{code}.parquet"
    if not p.exists():
        return None
    try:
        d = pd.read_parquet(p, columns=["date"])
        return pd.to_datetime(d["date"].iloc[-1]) if len(d) else None
    except Exception:
        return None


def main():
    lines = []
    log = lambda s="": lines.append(s)

    # ── 名单 ①: 快照消失 ──
    # 注意: 小盘快照消失≠退市, 大部分是升入CSI500/800的指数调出;
    # 仅作候选池, 真正的退市判定用 baostock末日<归档冻结 过滤(见Q1)
    disapp = snap_disappeared()
    log(f"[Q1a] 快照消失名单: {len(disapp)}只 (含指数调出, 非纯退市; "
        f"last_seen=快照日)")
    log(f"  {sorted(disapp.items())[:12]}")

    # ── 名单 ②: baostock vs 当前库 ──
    bs_codes = {p.stem for p in BS_DIR.glob("*.parquet")}
    info = pd.read_parquet("data_store/meta/stock_info_full.parquet")
    cur_codes = set(info["code"].astype(str))
    gone = bs_codes - cur_codes
    log(f"\n[Q1b] baostock有日线但不在stock_info_full(已退市/更名/合并): {len(gone)}只")
    log(f"  {sorted(gone)[:20]}")

    # ── Q1: 残留数据对照 ──
    # 注意(二轮修正): baostock归档整体冻结在2025-12-31, 不能直接当退市日;
    # 只有 baostock末日 < 2025-12-01 的消失代码才是"归档冻结前已退市",
    # 其baostock末日≈退市末日, 才有对照意义。本地晚于该日>10日=残留嫌疑。
    FREEZE = pd.Timestamp("2025-12-01")
    rows = []
    for code in sorted(disapp.keys() | gone):
        l_last, b_last = local_last_date(code), bs_last_date(code)
        if b_last is None or b_last >= FREEZE:
            continue  # 归档冻结后无法区分"退市"与"归档停止更新"
        resid = (l_last is not None and l_last > b_last + pd.Timedelta(days=10))
        rows.append({"code": code, "snapshot_last_seen": disapp.get(code, ""),
                     "local_last": str(l_last.date()) if l_last else "",
                     "baostock_last": str(b_last.date()) if b_last else "",
                     "residual_suspect": bool(resid)})
    df = pd.DataFrame(rows)
    resid = df[df["residual_suspect"]]
    log(f"\n[Q1] 归档冻结前已退市样本({len(df)}只)中, 本地晚于baostock末日>10日"
        f"(残留嫌疑): {len(resid)}只")
    if len(resid):
        log(resid.to_string())
    missing_local = df[df["local_last"] == ""]
    log(f"[Q1] 其中本地无数据(纯幸存者样本缺失): {len(missing_local)}只")
    df.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")

    # ── Q2: 引擎出清价语义(代码级判定, 不跑回测) ──
    log("\n[Q2] 引擎退市出清语义(backtest_engine.py 代码路径判定):")
    log("  Step1 MTM: cp为NaN(退市后无价)→该日持仓不计盈亏, 价格冻结在最后价")
    log("  Step2 止损/MA10: cp为NaN→continue, 永不触发卖出")
    log("  Step4 调仓: 退市票不在新池→权重从cur_weights静默消失:")
    log("    - 不按0计亏损(非-100%强平)")
    log("    - 不付卖出佣金(换手口径低估)")
    log("    - 前值也不计入: 最后可得价冻结至调仓日, 调仓日之后=白赚最后一段")
    log("  结论: 语义=『最后交易价冻结→下次调仓静默消失』, 退市亏损在回测中被低估")
    log("  (对照 backtest_smallcap.py: 按最后可得价强制清仓=如实计退市亏损, 口径更严)")

    # ── Q3: 退市整理期成交额 ──
    log("\n[Q3] 退市整理期成交额检查(真退市样本中, 抽末日最晚的5只):")
    checked = 0
    for code in sorted(df["code"], key=lambda c: df.loc[df["code"] == c,
                                                        "baostock_last"].iloc[0],
                       reverse=True)[:5]:
        b_last = bs_last_date(code)
        if b_last is None:
            continue
        p = BS_DIR / f"{code}.parquet"
        d = pd.read_parquet(p)
        d["date"] = pd.to_datetime(d["date"])
        tail = d[d["date"] >= b_last - pd.Timedelta(days=60)]
        if len(tail) and "amount" in d.columns:
            amt = pd.to_numeric(tail["amount"], errors="coerce")
            log(f"  {code} 最后60日成交额: 均值{amt.mean()/1e4:,.0f}万 峰值{amt.max()/1e4:,.0f}万"
                f" 末日{b_last.date()} 末日额{amt.iloc[-1]/1e4:,.0f}万")
            checked += 1
    if not checked:
        log("  (无足够样本, 跳过)")

    text = "\n".join(lines)
    print(text)
    OUT_LOG.write_text(text, encoding="utf-8")
    print(f"\n落盘: {OUT_LOG} / {OUT_CSV}")


if __name__ == "__main__":
    main()
