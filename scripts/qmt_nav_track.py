"""
QMT 实盘 NAV 追踪 — 正确口径的净值曲线。

为什么要重写:
  - QMT 仿真户 total_assets≈4888万(种子资金), 但策略 notional 只有 100万 →
    用 total_assets 做基数, 收益全被稀释成噪音。
  - 旧 qmt_performance.csv 的 daily_ret 按 market_value 差值算, 被建仓/清仓污染
    (出现 +6459%、-61% 这种垃圾), 不可用。

本模块口径:
  - 完全忽略账户 total_assets / cash, 只用持仓做 mark-to-market。
  - **价格来源: 快照自带的 market_value/volume (QMT导出时实时报的, 天天都有, 不依赖本地日线)**;
    只有"快照日之间被卖掉的票"需要期末价时才用本地日线兜底, 且兜底价过期>5天会告警。
    此前只靠本地日线, 日线管道一断NAV就假持平(8/18-8/21事故)。
  - 种子 = 股票腿 100 万 + CB 腿 60 万 notional, 每个快照日之间按"期初持仓的
    价格变动"算收益, 分腿复利成 NAV。
  - 2026-10-20 修正: 快照含 CB 腿持仓, 旧实现把 CB 腿 P&L 并入 1M 股票腿
    → invested_pct 达 126.9%、单腿 NAV 被另一腿污染。现输出:
      nav / nav_a = 股票腿(1M, 剔除 CB); nav_cb = CB 腿(600k);
      nav_combined = (equity_a + equity_cb) / 1.6M。
    旧 `nav` 语义=股票腿(修正后), 组合绩效看 `nav_combined`。
  - 现金部分(未投入的 notional)按 CASH_YIELD 计息; 换手按 COMMISSION 扣费。

局限(诚实说明):
  - 快照非严格收盘时点(有的盘中导出), 期间买卖近似发生在期末 → 日内择时被忽略(影响小)。
  - 种子日把累计盈亏清零重新计时(旧追踪已坏, 无法恢复真实历史盈亏)。
  - 样本极短: 年化/夏普在 ≥20 个交易日前只是参考, 不具统计意义。

用法:
    python scripts/qmt_nav_track.py          # 重建并打印
    from qmt_nav_track import rebuild_and_save
"""
import re, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd
from loguru import logger

from data.storage import load_daily
from run_backtest_a import COMMISSION, CASH_YIELD

NOTIONAL   = 400_000
NOTIONAL_CB = 600_000
CB_PREFIX  = ("110", "111", "113", "118", "123", "127", "128")
SNAP_DIR   = Path("logs")
NAV_FILE   = Path("logs/qmt_nav_history.parquet")
SNAP_RE    = re.compile(r"qmt_positions_(\d{8})(?:_\d{4})?\.json$")
STALE_DAYS = 5  # 兜底收盘价超过5天未更新视为过期 → 告警


CUTOFF = "15:00"   # 快照自带价只有在15:00后导出才算收盘价口径
def load_snapshots() -> dict[str, dict[str, dict]]:
    """读所有 logs/qmt_positions_YYYYMMDD[_HHMM].json → {date: {code: {vol, mv}}}。
    同一天多份取导出时间最晚的一份。"""
    by_date: dict[str, tuple[str, dict]] = {}
    for f in SNAP_DIR.glob("qmt_positions_*.json"):
        m = SNAP_RE.search(f.name)
        if not m:
            continue
        d8 = m.group(1)
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        exported = data.get("exported_at", "")
        if d8 not in by_date or exported > by_date[d8][0]:
            by_date[d8] = (exported, data)
    out = {}
    for d8, (_, data) in by_date.items():
        date_str = f"{d8[:4]}-{d8[4:6]}-{d8[6:]}"
        pos = data.get("positions", {})
        out[date_str] = {
            c: {"vol": int(v.get("volume", 0)), "mv": float(v.get("market_value", 0))}
            for c, v in pos.items() if v.get("volume", 0) > 0
        }
    return dict(sorted(out.items()))


def load_snapshot_times() -> dict[str, str]:
    """date → 导出时点(HH:MM)。用于判断快照价是否为收盘口径。"""
    times = {}
    for f in SNAP_DIR.glob("qmt_positions_*.json"):
        m = SNAP_RE.search(f.name)
        if not m:
            continue
        d8 = m.group(1)
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        exported = data.get("exported_at", "")
        if len(exported) < 16:
            continue
        date_str = f"{d8[:4]}-{d8[4:6]}-{d8[6:]}"
        hhmm = exported[11:16]
        if date_str not in times or exported[11:19] > times[date_str]:
            times[date_str] = exported[11:19]
    return times


def _close_panel(codes: set[str], start: str, end: str) -> pd.DataFrame:
    """本地日线收盘价矩阵(兜底用)。index=date, col=code。"""
    ser = {}
    for c in codes:
        df = load_daily(c, start, end)
        if df.empty or "close" not in df.columns:
            continue
        df = df.copy(); df["date"] = pd.to_datetime(df["date"])
        ser[c] = pd.to_numeric(df.set_index("date")["close"], errors="coerce")
    return pd.DataFrame(ser).sort_index() if ser else pd.DataFrame()


def build_nav() -> pd.DataFrame:
    snaps = load_snapshots()
    if len(snaps) < 1:
        logger.warning("无快照, 无法建NAV"); return pd.DataFrame()

    dates = list(snaps.keys())
    all_codes = set().union(*[set(h) for h in snaps.values()])
    px_local = _close_panel(all_codes, dates[0], dates[-1])

    # 快照自带价格: (date, code) -> mv/vol (QMT导出时的实时价, 最可靠)
    snap_price: dict[tuple[str, str], float] = {}
    for dstr, hold in snaps.items():
        for c, v in hold.items():
            if v["vol"] > 0 and v["mv"] > 0:
                snap_price[(dstr, c)] = v["mv"] / v["vol"]

    warnings: list[str] = []

    def local_close(code, dstr):
        """本地日线 <=dstr 最近收盘价, 返回 (price, last_date_str)。"""
        if code not in px_local.columns:
            return np.nan, None
        s = px_local[code].loc[:pd.Timestamp(dstr)].dropna()
        if not len(s):
            return np.nan, None
        return float(s.iloc[-1]), str(s.index[-1].date())

    snap_times = load_snapshot_times()
    repriced = 0

    def price_on(code, dstr):
        """价格: 快照自带价只在15:00后导出时才算收盘口径; 盘中快照改用本地日线收盘价重估。"""
        nonlocal repriced
        t = snap_times.get(dstr, "")
        if t and t[:5] >= CUTOFF:
            p = snap_price.get((dstr, code))
            if p is not None:
                return p
        p, last_d = local_close(code, dstr)
        if p is not None and not np.isnan(p):
            repriced += 1
            if last_d and (pd.Timestamp(dstr) - pd.Timestamp(last_d)).days > STALE_DAYS:
                warnings.append(f"{code}@{dstr}: 本地日线过期(最新{last_d}), 用旧价标值")
            return p
        return np.nan

    def is_cb(code):
        return str(code)[:3] in CB_PREFIX

    sleeves = {"a": (float(NOTIONAL), lambda c: not is_cb(c)),
               "cb": (float(NOTIONAL_CB), is_cb)}
    state = {k: {"equity": n, "prev_date": None, "prev_hold": {}}
             for k, (n, _) in sleeves.items()}
    rows = []
    prev_nav_all = None
    for dstr in dates:
        hold = snaps[dstr]
        row = {"date": pd.Timestamp(dstr)}
        for name, (notional, pred) in sleeves.items():
            hold_s = {c: v for c, v in hold.items() if pred(c)}
            prices_now = {c: price_on(c, dstr) for c in hold_s}
            invested_now = sum(v["vol"] * prices_now[c]
                               for c, v in hold_s.items()
                               if not np.isnan(prices_now[c]))
            st = state[name]
            if st["prev_date"] is None:
                ret = 0.0
            else:
                pnl = 0.0
                for c, v in st["prev_hold"].items():
                    p0 = price_on(c, st["prev_date"])
                    p1 = price_on(c, dstr)
                    if not np.isnan(p0) and not np.isnan(p1):
                        pnl += v["vol"] * (p1 - p0)
                prev_invested = 0.0
                for c, v in st["prev_hold"].items():
                    p = price_on(c, st["prev_date"])
                    if not np.isnan(p):
                        prev_invested += v["vol"] * p
                cash = max(0.0, st["equity"] - prev_invested)
                days = (pd.Timestamp(dstr) - pd.Timestamp(st["prev_date"])).days
                interest = cash * CASH_YIELD * days / 365
                traded = 0.0
                for c in set(st["prev_hold"]) | set(hold_s):
                    p1 = price_on(c, dstr)
                    if np.isnan(p1):
                        continue
                    v0 = st["prev_hold"].get(c, {}).get("vol", 0)
                    v1 = hold_s.get(c, {}).get("vol", 0)
                    traded += abs(v1 - v0) * p1
                fee = traded * COMMISSION
                eq_new = st["equity"] + pnl + interest - fee
                ret = eq_new / st["equity"] - 1
                st["equity"] = eq_new
            row[f"nav_{name}"] = st["equity"] / notional
            row[f"equity_{name}"] = round(st["equity"])
            row[f"invested_{name}"] = round(invested_now)
            row[f"n_pos_{name}"] = len(hold_s)
            row[f"ret_{name}"] = round(ret, 5)
            st["prev_date"], st["prev_hold"] = dstr, hold_s

        eq_all = state["a"]["equity"] + state["cb"]["equity"]
        inv_all = row["invested_a"] + row["invested_cb"]
        nav_all = eq_all / (NOTIONAL + NOTIONAL_CB)
        row["nav_combined"] = nav_all
        row["equity_combined"] = round(eq_all)
        row["invested_combined"] = round(inv_all)
        row["n_pos_combined"] = row["n_pos_a"] + row["n_pos_cb"]
        row["ret_combined"] = (round(nav_all / prev_nav_all - 1, 5)
                               if prev_nav_all is not None else 0.0)
        prev_nav_all = nav_all
        # 兼容旧列: nav=股票腿(1M notional, 已剔除CB), 不再混入CB盈亏
        row.update({
            "nav": row["nav_a"], "equity": row["equity_a"],
            "invested": row["invested_a"],
            "invested_pct": round(row["invested_a"] / row["equity_a"] * 100, 1),
            "n_pos": row["n_pos_a"], "ret": row["ret_a"],
        })
        rows.append(row)
    for r in rows:
        r.pop("_prev_nav_all", None)

    # 护栏: 汇总兜底价过期告警(只在有真问题时吵, 不静默)
    if warnings:
        uniq = sorted(set(warnings))
        logger.warning(f"NAV护栏: {len(uniq)}处本地日线兜底价过期: {uniq[:5]}{'...' if len(uniq)>5 else ''}")
    if repriced:
        logger.info(f"NAV口径: {repriced}处盘中快照价已改用本地日线收盘价重估(快照15:00前导出)")
    return pd.DataFrame(rows)


def rebuild_and_save() -> pd.DataFrame:
    df = build_nav()
    if not df.empty:
        NAV_FILE.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(NAV_FILE, index=False)
    return df


def metrics(df: pd.DataFrame) -> dict:
    if df.empty or len(df) < 2:
        return {"note": "样本不足"}
    nav = df.set_index("date")["nav"]
    total = nav.iloc[-1] - 1
    days = (nav.index[-1] - nav.index[0]).days or 1
    ann = (nav.iloc[-1]) ** (365 / days) - 1
    mdd = ((nav - nav.cummax()) / nav.cummax()).min()
    n = len(df)
    out = {
        "观测数": n, "起止": f"{nav.index[0].date()}→{nav.index[-1].date()} ({days}天)",
        "累计收益": f"{total:+.2%}", "年化(外推,参考)": f"{ann:+.1%}", "最大回撤": f"{mdd:.2%}",
    }
    if n >= 20:
        rets = df["ret"].iloc[1:]
        sharpe = rets.mean() / rets.std() * np.sqrt(252) if rets.std() > 0 else 0
        out["夏普"] = f"{sharpe:.2f}"
    else:
        out["夏普"] = f"样本不足(N={n}, 需≥20交易日)"
    return out


def main():
    df = rebuild_and_save()
    if df.empty:
        print("无法建NAV(缺快照或本地价)"); return
    print("\n" + "=" * 72)
    print("  QMT NAV (分腿 mark-to-market: 股票1M / CB 600k / 合计1.6M)")
    print("=" * 72)
    show = df.copy()
    show["date"] = show["date"].dt.date
    show["ret"] = (show["ret"] * 100).round(2).astype(str) + "%"
    print(show.to_string(index=False))
    print("\n── 股票腿绩效(1M, 口径见脚本docstring, 样本极短仅供参考) ──")
    for k, v in metrics(df).items():
        print(f"  {k}: {v}")
    comb = df[["date", "nav_combined", "ret_combined"]].rename(
        columns={"nav_combined": "nav", "ret_combined": "ret"})
    print("\n── 组合绩效(股票1M+CB 600k=1.6M) ──")
    for k, v in metrics(comb).items():
        print(f"  {k}: {v}")
    print(f"\nNAV已存 → {NAV_FILE}")


if __name__ == "__main__":
    main()
