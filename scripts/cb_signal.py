"""
CB 引擎信号生成（2026-09-01，股票×CB 配比 40/60 部署用）。

口径与回测 backtest_cb_doublelow.py 一致：双低(价格+溢价率×100)最低25只，
等权月调仓，资金 60 万。信号输出 data_store/meta/signal_cb_latest.json，
Windows 端 fetch_and_execute --track cb 拉取执行（执行器已参数化 track）。

月调仓日：每月 15 日 + 月末倒数第二（与股票侧一致的双周节奏，但 CB 只在这些日
全量刷新；非调仓日仅提示强赎/退市）。
用法: python scripts/cb_signal.py
"""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
from loguru import logger

logger.add("logs/cb_signal_{time:YYYY-MM-DD}.log", rotation="1 day", retention="60 days")

from data.storage import load_meta
from monitoring.alerts import send_alert

CB_CAPITAL = 600_000
TOP_N = 25
SIGNAL_FILE = Path("data_store/meta/signal_cb_latest.json")
CB_DIR = Path("data_store/convertible_bonds")


def select_double_low(snap):
    """快照已含 dblow 列, 直接按双低取最低 N 只。与回测口径一致。"""
    df = snap.copy()
    df["dblow"] = pd.to_numeric(df["dblow"], errors="coerce")
    df = df.dropna(subset=["dblow"])
    df = df[~df["code"].astype(str).str.startswith("404")]   # 退市板块债, 实盘流动性买不进
    return df.sort_values("dblow").head(TOP_N)["code"].tolist()


def _load_prev():
    if not SIGNAL_FILE.exists():
        return {}
    try:
        return json.loads(SIGNAL_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _load_actual_cb():
    """读QMT快照的转债持仓(11/12/127前缀), 判断CB建仓是否已实盘成交。"""
    import json as _j
    snap = Path("logs/qmt_positions_latest.json")
    if not snap.exists():
        return None
    try:
        d = _j.loads(snap.read_text(encoding="utf-8"))
        pos = d.get("positions", {})
        cb = {str(c).split(".")[0] for c, v in pos.items()
              if isinstance(v, dict) and v.get("volume", 0) > 0 and str(c)[:3] in ("110", "111", "113", "118", "123", "127", "128")}
        return cb if cb else None
    except Exception:
        return None


def run():
    from datetime import date
    today = str(date.today())
    snaps = load_meta("cb_snapshots")
    if snaps.empty:
        logger.error("cb_snapshots 为空，请先跑 build_cb_data.py")
        return
    snaps["snap_date"] = pd.to_datetime(snaps["snap_date"])
    latest = snaps["snap_date"].max()
    if (pd.Timestamp(today) - latest).days > 35:
        send_alert(f"CB信号: 快照过期(最新{latest.date()})，跳过生成", level="warning")
        return
    snap = snaps[snaps["snap_date"] == latest]
    codes = select_double_low(snap)
    if not codes:
        logger.error("选股为空")
        return

    # 价格快照
    snap_price = {}
    for c in codes:
        row = snap[snap["code"] == c].iloc[0]
        snap_price[c] = float(row["price"]) if pd.notna(row["price"]) else None

    # 等权股数(转债一手=10张)
    budget = CB_CAPITAL / len(codes)
    shares, prices = {}, {}
    for c in codes:
        p = snap_price.get(c)
        if not p or p <= 0:
            continue
        lots = int(budget / p / 10)
        shares[c] = lots * 10
        prices[c] = round(p, 2)

    prev = _load_prev()
    prev_hold = set(prev.get("holdings", []))
    new_hold = set(shares.keys())
    actual_cb = _load_actual_cb()
    # 月度调仓日才全量轮换；非调仓日保持持仓(仅输出状态)
    from daily_signal_a_v2 import _get_trade_calendar, is_rebalance_day
    cal = _get_trade_calendar()
    if is_rebalance_day(today, cal):
        buy = sorted(new_hold - prev_hold)
        sell = sorted(prev_hold - new_hold)
    elif not prev or not actual_cb:
        # 首次运行 或 实盘CB尚未建仓: 保持全量买入信号直到成交(自愈, 防错过建仓窗口)
        buy = sorted(new_hold)
        sell = []
        if not prev:
            logger.warning("[CB] 首次信号: 全量建仓信号保持到实盘确认")
        else:
            logger.warning(f"[CB] 实盘CB持仓缺失(快照无转债), 建仓信号保持: {len(buy)}只")
    else:
        # 非调仓日: 沿用上一信号持仓, 仅刷新价格/日期
        buy, sell = [], []
        if prev_hold:
            codes = sorted(prev_hold)
            shares = {c: prev.get("shares", {}).get(c, 0) for c in codes}
            prices = {c: prev.get("prices", {}).get(c, 0) for c in codes}

    signal = {
        "signal_date": today,
        "track": "cb",
        "capital": CB_CAPITAL,
        "holdings": codes,
        "buy": buy,
        "sell": sell,
        "shares": shares,
        "prices": prices,
        "position_ratio": 1.0,
        "note": f"CB双低25只月调仓 | 资金60万 | 最新快照{latest.date()}",
    }
    SIGNAL_FILE.write_text(json.dumps(signal, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"CB信号: {today} 持仓{len(codes)}只 买入{len(buy)} 卖出{len(sell)}")
    send_alert(f"[CB] 信号更新: 持仓{len(codes)}只, 买入{len(buy)}, 卖出{len(sell)} ({latest.date()}快照)")


if __name__ == "__main__":
    run()
