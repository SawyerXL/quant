"""做T服务 — 盘中±2%触发检测 + 做T记录管理 (回测验证: 正T+反T年化增厚25-30%)."""
import sys, os, json
from pathlib import Path
from datetime import date, datetime

_project_root = Path(__file__).parent.parent.parent
os.chdir(_project_root)
sys.path.insert(0, str(_project_root))
sys.path.insert(0, str(_project_root / "scripts"))

from config.settings import ROOT, DATA_STORE

# 做T记录存CSV (与MA10日志同风格)
T_RECORD_FILE = ROOT / "config" / "t_trade_log.csv"
# 信号历史存CSV (每次检测到信号都记录, 漏看可回查)
T_SIGNAL_LOG = ROOT / "config" / "t_signal_log.csv"
# 验证期结算结果 (settle_t_signals.py 输出, 分钟线精确判定)
T_SETTLE_LOG = ROOT / "config" / "t_settle_log.csv"

# 回测验证参数
TRIGGER_PCT = 2.0    # 涨跌≥2%触发
SETTLE_PCT = 1.0     # 回落/反弹1%了结
POSITION_FRAC = 1/3  # 只用1/3仓位


def get_t_signals(is_admin: bool = False, user_id: str = "") -> dict:
    """扫描持仓，返回当前触发做T信号的股票。"""
    from scripts.morning_10dim_report import fetch_rt_prices
    from web.services.portfolio_service import _get_user_positions

    positions = _get_user_positions(user_id, is_admin)
    if not positions:
        return {"signals": [], "note": "无持仓"}

    codes = [p["code"] for p in positions]
    rt = fetch_rt_prices(codes)

    signals = []
    trading = _is_trading_time()
    if not trading:
        # 非交易时段不显示信号（数据是收盘快照，非实时）
        return {"signals": [], "trading": False,
                "updated_at": datetime.now().strftime("%H:%M:%S"),
                "fees": {
                    "commission": "万2.5×2",
                    "stamp_tax": "万5(卖)",
                    "slippage": "千0.5×2",
                    "total_roundtrip": "约0.2%",
                    "net_expected": "1%价差 - 0.2%成本 = 每笔净利约0.8%×1/3仓位",
                },
                "note": "非交易时段。做T信号仅在9:30-15:00实时触发。"}

    for pos in positions:
        code = pos["code"]
        p = rt.get(code, {})
        cur = p.get("cur", 0)
        prev = p.get("prev", 0)
        if cur <= 0 or prev <= 0:
            continue
        chg = (cur / prev - 1) * 100

        if chg >= TRIGGER_PCT:
            sell_target = round(prev * 1.02, 2)
            buy_target = round(sell_target * 0.99, 2)
            signals.append({
                "code": code, "name": pos.get("name", ""),
                "direction": "正T",
                "chg_pct": round(chg, 2),
                # leg1=先出手的那腿, leg2=了结腿。结构化落盘, 供 settle_t_signals.py 精确判定成交
                "leg1_price": sell_target, "leg2_price": buy_target,
                "action": f"挂卖{sell_target} 卖1/3 → 挂买{buy_target}接回",
                "tone": "up",
                "shares_frac": f"{int(POSITION_FRAC * pos['shares'])}股(1/3)",
            })
        elif chg <= -TRIGGER_PCT:
            buy_target = round(prev * 0.98, 2)
            sell_target = round(buy_target * 1.01, 2)
            signals.append({
                "code": code, "name": pos.get("name", ""),
                "direction": "反T",
                "chg_pct": round(chg, 2),
                "leg1_price": buy_target, "leg2_price": sell_target,
                "action": f"挂买{buy_target} 买1/3 → 挂卖{sell_target}卖出",
                "tone": "dn",
                "shares_frac": f"{int(POSITION_FRAC * pos['shares'])}股(1/3)",
            })

    # 记录信号历史（防漏看）
    if signals:
        _log_signals(signals)

    return {
        "signals": signals,
        "trading": _is_trading_time(),
        "updated_at": datetime.now().strftime("%H:%M:%S"),
        "fees": {
            "commission": "万2.5×2",
            "stamp_tax": "万5(卖)",
            "slippage": "千0.5×2",
            "total_roundtrip": "约0.2%",
            "net_expected": "1%价差 - 0.2%成本 = 每笔净利约0.8%×1/3仓位",
        },
        "note": "正T: 冲高≥2%抛1/3, 回落1%接回 | 反T: 急跌≥2%吸1/3, 反弹1%卖出 | 次日开盘必了结 | 已含手续费+滑点",
    }


def _log_signals(signals: list) -> None:
    """把检测到的信号追加到历史日志（同一天同票同方向去重）。"""
    import pandas as pd
    today = str(date.today())
    rows = []
    for s in signals:
        rows.append({
            "date": today,
            "time": datetime.now().strftime("%H:%M:%S"),
            "code": s["code"],
            "name": s.get("name", ""),
            "direction": s["direction"],
            "chg_pct": s["chg_pct"],
            "leg1_price": s.get("leg1_price"),
            "leg2_price": s.get("leg2_price"),
            "action": s["action"],
        })
    if not rows: return
    new_df = pd.DataFrame(rows)
    if T_SIGNAL_LOG.exists():
        old_df = pd.read_csv(T_SIGNAL_LOG, dtype={"code": str})
        # 去重: 同日同票同方向
        if not old_df.empty:
            keys_old = set(zip(old_df["date"], old_df["code"], old_df["direction"]))
            new_df = new_df[~new_df.apply(lambda r: (r["date"], r["code"], r["direction"]) in keys_old, axis=1)]
        if not new_df.empty:
            pd.concat([old_df, new_df], ignore_index=True).to_csv(T_SIGNAL_LOG, index=False)
    else:
        new_df.to_csv(T_SIGNAL_LOG, index=False)


def get_signal_history(limit: int = 50) -> list:
    """返回信号历史（漏看回查用）。"""
    import pandas as pd
    if not T_SIGNAL_LOG.exists():
        return []
    df = pd.read_csv(T_SIGNAL_LOG, dtype={"code": str})
    return df.tail(limit).to_dict(orient="records")


def _is_trading_time() -> bool:
    from datetime import time as dtime
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.time()
    return dtime(9, 30) <= t <= dtime(15, 0)


def load_t_records() -> list:
    """加载做T记录。"""
    import pandas as pd
    if not T_RECORD_FILE.exists():
        return []
    df = pd.read_csv(T_RECORD_FILE, dtype={"code": str})
    records = df.to_dict(orient="records")
    for r in records:
        for k, v in list(r.items()):
            if hasattr(v, "item"):
                r[k] = v.item()
            elif isinstance(v, float) and str(v) == "nan":
                r[k] = None
    return records


def save_t_record(code: str, name: str, direction: str, sell_price: float,
                  buy_price: float, shares: int, settle_date: str = "") -> dict:
    """保存一条做T记录。正T: sell先买后接回; 反T: buy先买后卖。"""
    import pandas as pd

    if direction == "正T":
        pnl = (sell_price - buy_price) * shares  # 高抛低接赚差价
    else:
        pnl = (sell_price - buy_price) * shares  # 低吸高抛赚差价

    record = {
        "date": str(date.today()),
        "code": code,
        "name": name,
        "direction": direction,
        "sell_price": round(sell_price, 3),
        "buy_price": round(buy_price, 3),
        "shares": int(shares),
        "pnl": round(pnl, 2),
        "settled": "是" if settle_date else "否",
        "settle_date": settle_date or "",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    if T_RECORD_FILE.exists():
        df = pd.read_csv(T_RECORD_FILE, dtype={"code": str})
        df = pd.concat([df, pd.DataFrame([record])], ignore_index=True)
    else:
        df = pd.DataFrame([record])
    df.to_csv(T_RECORD_FILE, index=False)

    # Stats
    records = df.to_dict(orient="records")
    settled = [r for r in records if r.get("settled") == "是"]
    wins = [r for r in settled if r.get("pnl", 0) > 0]
    return {
        "status": "saved",
        "pnl": record["pnl"],
        "stats": {
            "total": len(records),
            "settled_count": len(settled),
            "win_rate": round(len(wins)/len(settled)*100, 1) if settled else 0,
            "total_pnl": round(sum(r.get("pnl", 0) for r in settled), 2),
        }
    }


def load_t_settlements() -> list:
    """验证期结算明细(分钟线精确)。反T: buy=leg1/sell=exit; 正T: sell=leg1/buy=exit。"""
    if not T_SETTLE_LOG.exists():
        return []
    import pandas as pd
    df = pd.read_csv(T_SETTLE_LOG, dtype={"code": str})
    out = []
    for _, r in df.iterrows():
        if r.get("status") != "已结算":
            continue
        buy = r["leg1_price"] if r["direction"] == "反T" else r["exit_price"]
        sell = r["leg1_price"] if r["direction"] == "正T" else r["exit_price"]
        out.append({
            "date": str(r["date"]), "code": str(r["code"]).zfill(6),
            "name": r.get("name", ""), "direction": r["direction"],
            "sell_price": sell, "buy_price": buy,
            "pnl_pct": r.get("pnl_pct"), "exit_kind": r.get("exit_kind", ""),
            "method": r.get("method", ""), "settled": "是", "auto": True,
        })
    return out


def get_t_stats() -> dict:
    """做T统计：手工记录 + 验证期自动结算战报。"""
    records = load_t_records()
    settled = [r for r in records if r.get("settled") == "是"]
    wins = [r for r in settled if r.get("pnl", 0) > 0]

    auto = [s for s in load_t_settlements()]
    auto_pnl = [float(s["pnl_pct"]) for s in auto if s.get("pnl_pct") not in (None, "")]
    auto_wins = [p for p in auto_pnl if p > 0]
    return {
        "total": len(records),
        "pending": len(records) - len(settled),
        "settled_count": len(settled),
        "win_rate": round(len(wins)/len(settled)*100, 1) if settled else 0,
        "total_pnl": round(sum(r.get("pnl", 0) for r in settled), 2),
        # 验证期战报(基准: 胜率≥90%, 单次≥+0.15%)
        "v_count": len(auto_pnl),
        "v_win_rate": round(len(auto_wins)/len(auto_pnl)*100, 1) if auto_pnl else 0,
        "v_avg": round(sum(auto_pnl)/len(auto_pnl), 3) if auto_pnl else 0,
        "v_forced": sum(1 for s in auto if s.get("exit_kind", "").startswith("次日")),
    }
