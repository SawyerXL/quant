"""US 独立 track 最小链路（2026-10-22，用户裁定 A：独立 track_us）。

模式：
  --init          初始化 20 万纸面状态（已存在则不覆盖）
  --signal        计算目标/实时 IOPV 溢价闸门/拆单/风控 dry-run，写
                  data_store/meta/signal_us_latest.json（不成交）
  --paper-fill    按最新信号纸面成交（溢价闸门通过才买；3bp 费）
  --mark          按最新价更新纸面 NAV，追加 logs/us_track_daily.csv

口径：US sleeve 20 万（12.5%×160 万），513400；单笔 ≤10 万拆单；
sleeve 无 15% 熔断（核心账户级 15% 线另计）；目标 12.5%、±3pp 核心带宽。

风控（2026-10-22 用户全权授权后）：`track_us` 已正式映射进
`execution/risk.py`（`_STRATEGY_RISK` + 按 track 的 `max_drawdown_circuit`；
track_us=1.0 无 sleeve 熔断，track_a 0.15 不变；缺失配置 fail-closed）。
`USRiskGateway` 保留为显式 sleeve 红线，其余单笔/单票/现金/涨跌停检查
全部复用 `execution/risk.py`。

**本脚本只做纸面/dry-run，不发任何真单。**
"""
import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from config.strategy_params.us_track import US_TRACK
from execution.risk import RiskGateway
from qdii_premium_monitor import tencent_quote
from data.source import get_source

STATE = Path("logs/us_track_state.json")
DAILY = Path("logs/us_track_daily.csv")
SIGNAL = Path("data_store/meta/signal_us_latest.json")
VEHICLE = US_TRACK["vehicle"]
CAP = float(US_TRACK["capital"])
BAND = float(US_TRACK["rebalance_band_pp"]) * float(US_TRACK["core_capital"])
FEE = float(US_TRACK["fee"])
CASH_YIELD = float(US_TRACK["cash_yield"])


class USRiskGateway(RiskGateway):
    """独立 US track 仅在 sleeve 层关闭 15% 熔断；其余检查全部继承。"""

    def _check_account_drawdown(self, strategy_id, code, direction,
                                shares, price, value, risk):
        return True, ""   # 核心 15% 线另计；sleeve 独立回撤不冻结


def _djia_t1(today: str):
    d = pd.read_parquet("data_store/meta/us_dji_sina.parquet")
    d["date"] = pd.to_datetime(d["date"])
    d = d[d["date"] < pd.Timestamp(today)].sort_values("date")
    if d.empty:
        return None, None
    r = d.iloc[-1]
    return float(r["close"]), str(r["date"].date())


def _state_init() -> dict:
    return {"as_of": None, "vehicle": VEHICLE, "cash": CAP, "shares": 0,
            "avg_price": 0.0, "last_price": 0.0, "nav": CAP, "nav_high": CAP,
            "entry_premium_pct": None, "last_mark": None, "fills": []}


def _load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return _state_init()


def _save_state(s: dict):
    STATE.write_text(json.dumps(s, ensure_ascii=False, indent=1),
                     encoding="utf-8")


def _target_shares(value: float, price: float) -> int:
    if price <= 0:
        return 0
    return int(value / price / 100) * 100


def split_orders(shares: int, price: float,
                 max_value: float = 100_000.0) -> list[int]:
    """按单笔金额上限拆单（整手）；返回各笔股数，和=shares。"""
    if shares <= 0:
        return []
    max_qty = int(max_value / price / 100) * 100
    if max_qty < 100:
        raise ValueError(f"单笔上限买不起一手: price={price}")
    out = []
    left = int(shares)
    while left > 0:
        q = min(left, max_qty)
        out.append(q)
        left -= q
    return out


def gate_pass(premium_pct, thresh=None) -> bool:
    if premium_pct is None:
        return False
    t = (thresh if thresh is not None else US_TRACK["premium_threshold"]) * 100
    return float(premium_pct) <= t


def _gw_state(s: dict, price: float, prev_close: float) -> dict:
    pos_val = s["shares"] * price
    return {
        "total_assets": CAP, "cash": float(s["cash"]),
        "positions": {VEHICLE: pos_val},
        "nav_high": float(s.get("nav_high") or CAP),
        "current_nav": float(s.get("nav") or CAP),
        "strategy_positions": {"track_us": {VEHICLE: pos_val}},
        "industry_map": {},
        "limit_prices": lambda code: {"up": prev_close * 1.10,
                                      "down": prev_close * 0.90},
    }


def _risk_checks(s: dict, orders: list[int], price: float,
                 prev_close: float, side: str) -> list[dict]:
    gw = USRiskGateway(_gw_state(s, price, prev_close))
    out = []
    for q in orders:
        ok, msg = gw.check("track_us", VEHICLE, side, q, price)
        out.append({"shares": q, "value": round(q * price, 2),
                    "ok": bool(ok), "msg": msg})
    return out


def _trading_day(today: str) -> bool:
    """查交易日历，不查当日 bar——14:45 时当日 bar 尚未落库。"""
    try:
        return today in get_source().get_trade_calendar(today, today)
    except Exception:  # noqa: BLE001
        return False


def signal() -> dict:
    today = date.today().isoformat()
    if not _trading_day(today):
        print(f"{today} 非交易日，跳过 US 信号")
        return {}
    s = _load_state()
    q = tencent_quote(VEHICLE)
    px, iopv, prem = q["price"], q["iopv"], q["premium_pct"]
    djia, djia_d = _djia_t1(today)
    cur_val = s["shares"] * px
    target_val = CAP
    side = "none"
    if s["shares"] == 0:
        side = "buy"
    elif abs(cur_val - target_val) > BAND:
        side = "buy" if cur_val < target_val else "sell"
    orders = []
    if side == "buy" and gate_pass(prem):
        # 预留手续费：买入金额 = 目标差额/(1+费率)，否则 20 万整买会被费用顶破
        need = _target_shares((target_val - cur_val) / (1 + FEE), px)
        need -= need % 100
        if need > 0:
            orders = split_orders(need, px)
    elif side == "sell":
        need = _target_shares(cur_val - target_val, px)
        need -= need % 100
        if need > 0:
            orders = split_orders(need, px)
    prev_close = q.get("prev_close") or px
    checks = _risk_checks(s, orders, px, prev_close, side) if orders else []
    total_val = sum(o * px for o in orders)
    out = {
        "as_of": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "signal_date": today, "vehicle": VEHICLE,
        "price": px, "prev_close": prev_close, "iopv": iopv,
        "premium_pct": prem, "gate_threshold_pct": US_TRACK["premium_threshold"] * 100,
        "gate_pass": bool(gate_pass(prem)),
        "djia_close": djia, "djia_date": djia_d,
        "state": {k: s.get(k) for k in ("cash", "shares", "nav", "nav_high")},
        "current_value": round(cur_val, 2), "target_value": target_val,
        "side": side, "orders": orders,
        "order_values": [round(o * px, 2) for o in orders],
        "total_order_value": round(total_val, 2),
        "risk_checks": checks, "risk_pass": bool(checks and all(c["ok"] for c in checks)),
        "dry_run": True,
        "note": "paper/dry-run only; live execution pending account wiring",
    }
    SIGNAL.parent.mkdir(parents=True, exist_ok=True)
    SIGNAL.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return out


def paper_fill() -> dict:
    if not SIGNAL.exists():
        raise RuntimeError("先运行 --signal")
    sig = json.loads(SIGNAL.read_text(encoding="utf-8"))
    s = _load_state()
    orders = sig.get("orders") or []
    if not orders or not sig.get("risk_pass"):
        print("无成交: orders空或风控未过")
        return s
    side = sig["side"]
    px = float(sig["price"])
    today = date.today().isoformat()
    fills = s.get("fills") or []
    if (side == "buy" and s.get("shares", 0) > 0 and fills
            and fills[-1].get("date") == today):
        print("今日已纸面买入，跳过重复 fill")
        return s
    sig_hm = str(sig.get("as_of", ""))[11:16]
    if side == "buy" and sig_hm and sig_hm < "14:40":
        raise RuntimeError(f"买入信号时点 {sig_hm} 早于 14:40，拒绝纸面成交"
                           f"（14:45 规则；测试用 --signal 后手动 --paper-fill 需 14:45 后）")
    if side == "buy":
        for q in orders:
            cost = q * px * (1 + FEE)
            if cost > s["cash"]:
                raise RuntimeError(f"纸面现金不足: {cost:.0f}>{s['cash']:.0f}")
            s["cash"] -= cost
            s["shares"] += q
            s["avg_price"] = ((s.get("avg_price", 0) or 0) *
                              (s["shares"] - q) + px * q) / s["shares"]
        if s.get("entry_premium_pct") is None:
            s["entry_premium_pct"] = sig.get("premium_pct")
    else:
        sell = min(sum(orders), s["shares"])
        s["cash"] += sell * px * (1 - FEE)
        s["shares"] -= sell
    s["last_price"] = px
    s["nav"] = s["cash"] + s["shares"] * px
    s["nav_high"] = max(float(s.get("nav_high") or 0), s["nav"])
    s["as_of"] = date.today().isoformat()
    s["fills"] = (s.get("fills") or [])[-49:] + [{
        "date": s["as_of"], "side": side, "shares": sum(orders),
        "price": px, "premium_pct": sig.get("premium_pct"),
        "orders": orders}]
    _save_state(s)
    print(json.dumps(s, ensure_ascii=False, indent=1))
    return s


def mark() -> dict:
    today = date.today().isoformat()
    if not _trading_day(today):
        print(f"{today} 非交易日，跳过 US mark")
        return {}
    s = _load_state()
    q = tencent_quote(VEHICLE)
    px = float(q["price"])
    # 现金按 1.4%/年 计息（两次 mark 之间的天数）
    last = s.get("last_mark") or today
    try:
        days = max((pd.Timestamp(today) - pd.Timestamp(last)).days, 0)
    except Exception:  # noqa: BLE001
        days = 0
    if s["shares"] == 0 and days > 0:
        s["cash"] *= (1 + CASH_YIELD * days / 365)
    s["last_price"] = px
    prev_high = float(s.get("nav_high") or CAP)
    s["nav"] = s["cash"] + s["shares"] * px
    s["nav_high"] = max(prev_high, s["nav"])
    s["as_of"] = today
    s["last_mark"] = today
    _save_state(s)
    row = {"date": today, "price": px, "iopv": q.get("iopv"),
           "premium_pct": q.get("premium_pct"), "shares": s["shares"],
           "cash": round(s["cash"], 2), "nav": round(s["nav"], 2),
           "nav_high": round(s["nav_high"], 2)}
    if DAILY.exists():
        df = pd.read_csv(DAILY)
        df = df[df["date"] != today]
        df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    else:
        df = pd.DataFrame([row])
    df.to_csv(DAILY, index=False)
    print(json.dumps(s, ensure_ascii=False, indent=1))
    return s


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--signal", action="store_true")
    ap.add_argument("--paper-fill", action="store_true")
    ap.add_argument("--mark", action="store_true")
    args = ap.parse_args()
    if args.init:
        if STATE.exists():
            print("状态已存在，不覆盖:", STATE)
        else:
            _save_state(_state_init())
            print("初始化:", STATE, CAP)
    if args.signal:
        signal()
    if args.paper_fill:
        paper_fill()
    if args.mark:
        mark()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
