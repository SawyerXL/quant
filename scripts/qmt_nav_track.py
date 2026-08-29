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
  - 种子 = 100万 notional, 每个快照日之间按"期初持仓的价格变动"算收益, 复利成 NAV。
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

NOTIONAL   = 1_000_000
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

    rows = []
    equity = float(NOTIONAL)
    prev_date, prev_hold = None, {}
    for dstr in dates:
        hold = snaps[dstr]
        invested_now = 0.0
        for c, v in hold.items():
            p = price_on(c, dstr)
            if not np.isnan(p):
                invested_now += v["vol"] * p

        if prev_date is None:
            ret = 0.0
        else:
            # 期初(prev)持仓的价格 P&L
            pnl = 0.0
            for c, v in prev_hold.items():
                p0 = price_on(c, prev_date)
                p1 = price_on(c, dstr)
                if not np.isnan(p0) and not np.isnan(p1):
                    pnl += v["vol"] * (p1 - p0)
            prev_invested = 0.0
            for c, v in prev_hold.items():
                p = price_on(c, prev_date)
                if not np.isnan(p):
                    prev_invested += v["vol"] * p
            cash = max(0.0, equity - prev_invested)
            days = (pd.Timestamp(dstr) - pd.Timestamp(prev_date)).days
            interest = cash * CASH_YIELD * days / 365
            # 换手手续费(期初vs期末持仓变动 × 期末价的近似)
            traded = 0.0
            for c in set(prev_hold) | set(hold):
                p1 = price_on(c, dstr)
                if np.isnan(p1):
                    continue
                v0 = prev_hold.get(c, {}).get("vol", 0)
                v1 = hold.get(c, {}).get("vol", 0)
                traded += abs(v1 - v0) * p1
            fee = traded * COMMISSION
            equity_new = equity + pnl + interest - fee
            ret = equity_new / equity - 1
            equity = equity_new

        rows.append({
            "date": pd.Timestamp(dstr), "nav": equity / NOTIONAL, "equity": round(equity),
            "invested": round(invested_now), "invested_pct": round(invested_now / equity * 100, 1),
            "n_pos": len(hold), "ret": round(ret, 5),
        })
        prev_date, prev_hold = dstr, hold

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
    print("  QMT 实盘 NAV (mark-to-market, 100万notional口径)")
    print("=" * 72)
    show = df.copy()
    show["date"] = show["date"].dt.date
    show["ret"] = (show["ret"] * 100).round(2).astype(str) + "%"
    print(show.to_string(index=False))
    print("\n── 绩效(口径见脚本docstring, 样本极短仅供参考) ──")
    for k, v in metrics(df).items():
        print(f"  {k}: {v}")
    print(f"\nNAV已存 → {NAV_FILE}")


if __name__ == "__main__":
    main()
