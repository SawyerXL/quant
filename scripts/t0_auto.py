"""
做T自动执行器 — QMT仿真盘验证版
状态机: 每只持仓票独立跟踪 做T生命周期
铁律: 1/3仓位 | ±2%触发±1%了结 | 挂单 | 次日开盘必了结

用法:
  python scripts/t0_auto.py --once    # 跑一轮检查
  python scripts/t0_auto.py --loop    # 盘中循环(默认30秒)
"""
import sys, os, json, time, requests, argparse
from pathlib import Path
from datetime import date, datetime, time as dtime

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from loguru import logger

STATE_FILE = ROOT / "logs" / "t0_state.json"
RECORD_FILE = ROOT / "config" / "t_trade_log.csv"

TRIGGER = 0.02   # ±2%触发
SETTLE = 0.01    # ±1%了结
FRAC = 1/3       # 1/3仓位

# QMT订单状态码: 48未报 49待报 50已报 51已报待撤 52撤单中 53已撤 54部撤 55部成 56已成 57废单
# Mock客户端返回字符串 "filled"，兼容两者
def _is_filled(status):
    return status in (55, 56) or str(status).lower() in ("filled", "partial", "部成", "已成")


def rt_price(code):
    """实时价格+昨收。"""
    exch = 'sh' if code.startswith(('5', '6', '9', '11')) else 'sz'
    try:
        r = requests.get(f'http://hq.sinajs.cn/list={exch}{code}',
                         headers={'Referer': 'https://finance.sina.com.cn'}, timeout=3)
        r.encoding = 'gb2312'
        d = r.text.split('"')[1].split(',')
        cur = float(d[3]) if d[3] else 0
        prev = float(d[2]) if d[2] else 0
        if cur <= 0:
            cur = prev  # 未开盘用昨收
        return cur, prev
    except Exception:
        return None, None


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state):
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def log_trade(code, name, direction, sell_p, buy_p, shares):
    """记录做T到CSV（与网页记录表同格式）。"""
    import pandas as pd
    pnl = (sell_p - buy_p) * shares
    rec = {
        "date": str(date.today()),
        "code": code, "name": name, "direction": direction,
        "sell_price": round(sell_p, 3), "buy_price": round(buy_p, 3),
        "shares": int(shares), "pnl": round(pnl, 2),
        "settled": "是", "settle_date": str(date.today()),
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    if RECORD_FILE.exists():
        df = pd.read_csv(RECORD_FILE, dtype={"code": str})
        df = pd.concat([df, pd.DataFrame([rec])], ignore_index=True)
    else:
        df = pd.DataFrame([rec])
    df.to_csv(RECORD_FILE, index=False)
    logger.info(f"做T完成: {name} {direction} 盈亏¥{pnl:.0f}")


def is_trading_time():
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.time()
    return dtime(9, 30) <= t <= dtime(14, 50)


def settle_check(client, state):
    """次日开盘强制了结：昨天没接回/没卖出的，开盘市价平仓。"""
    today = str(date.today())
    changed = False
    for code, st in list(state.items()):
        if st.get("date") == today:
            continue
        # 隔夜未了结
        if st.get("phase") in ("waiting_buyback", "waiting_sellout"):
            logger.warning(f"隔夜未了结 {code} {st['direction']} → 开盘市价平仓")
            if st["direction"] == "正T":
                # 卖飞了没接回 → 市价买回
                oid = client.place_order(code, "buy", st["shares"], -1, "market")
            else:
                # 低吸了没卖出 → 市价卖出
                oid = client.place_order(code, "sell", st["shares"], -1, "market")
            if oid and oid > 0:
                st["phase"] = "force_settled"
                st["settle_date"] = today
                changed = True
    if changed:
        save_state(state)
    return state


def run_cycle(client, quiet=False):
    """一轮状态机检查。"""
    state = load_state()
    today = str(date.today())

    # 1. 开盘时点检查隔夜单（9:30-9:35）
    now = datetime.now()
    t = now.time()
    if dtime(9, 30) <= t <= dtime(9, 36):
        state = settle_check(client, state)

    if not is_trading_time():
        return state

    # 2. 获取持仓和今日订单
    raw = client.get_positions()
    positions = {
        x.split(".")[0]: v for x, v in raw.items()
        if v.get("volume", 0) > 0 and not x.startswith("888")  # 排除国债逆回购等
    }
    orders = client.get_today_orders()

    # 3. 检查已有挂单成交情况
    for code, st in list(state.items()):
        if st.get("date") != today:
            continue
        phase = st.get("phase")
        if phase == "waiting_sell":
            # 检查卖单是否成交
            oid = st.get("order_id")
            for o in orders:
                if o.get("order_id") == oid and _is_filled(o.get("status")):
                    # 卖成交 → 挂接回买单
                    sell_p = st["price"]
                    buy_p = round(sell_p * (1 - SETTLE), 3)
                    nb = client.place_order(code, "buy", st["shares"], buy_p)
                    st["phase"] = "waiting_buyback"
                    st["buy_price"] = buy_p
                    st["order_id"] = nb
                    save_state(state)
                    logger.info(f"{code} 卖单成交@{sell_p} → 挂接回@{buy_p}")
                    break
        elif phase == "waiting_buyback":
            oid = st.get("order_id")
            for o in orders:
                if o.get("order_id") == oid and _is_filled(o.get("status")):
                    sell_p = st["price"]
                    buy_p = st["buy_price"]
                    log_trade(code, st.get("name", ""), "正T", sell_p, buy_p, st["shares"])
                    # 标记今日已完成, 防重复触发
                    state[code] = {"date": today, "code": code, "phase": "done_today"}
                    save_state(state)
                    break
        elif phase == "waiting_buy":
            oid = st.get("order_id")
            for o in orders:
                if o.get("order_id") == oid and _is_filled(o.get("status")):
                    buy_p = st["price"]
                    sell_p = round(buy_p * (1 + SETTLE), 3)
                    ns = client.place_order(code, "sell", st["shares"], sell_p)
                    st["phase"] = "waiting_sellout"
                    st["sell_price"] = sell_p
                    st["order_id"] = ns
                    save_state(state)
                    logger.info(f"{code} 买单成交@{buy_p} → 挂卖出@{sell_p}")
                    break
        elif phase == "waiting_sellout":
            oid = st.get("order_id")
            for o in orders:
                if o.get("order_id") == oid and _is_filled(o.get("status")):
                    sell_p = st["sell_price"]
                    buy_p = st["price"]
                    log_trade(code, st.get("name", ""), "反T", sell_p, buy_p, st["shares"])
                    state[code] = {"date": today, "code": code, "phase": "done_today"}
                    save_state(state)
                    break

    # 4. 新信号扫描（只对空闲状态的票）
    # 铁律: 单日单票只做1次
    done_today = {
        st.get("code") for st in state.values()
        if st.get("phase") == "done_today"
    }
    for code, pos in positions.items():
        if code in state or code in done_today:
            continue  # 已在做T流程中 或 今日已完成
        cur, prev = rt_price(code)
        if not cur or not prev or prev <= 0:
            continue
        chg = (cur / prev - 1)
        vol = int(pos.get("volume", 0))
        t_shares = int(vol * FRAC / 100) * 100  # 1/3仓位取整百
        if t_shares < 100:
            continue

        if chg >= TRIGGER:
            # 正T: 挂卖@昨收*1.02
            sell_p = round(prev * (1 + TRIGGER), 3)
            oid = client.place_order(code, "sell", t_shares, sell_p)
            if oid and oid > 0:
                state[code] = {
                    "date": today, "name": "", "direction": "正T",
                    "phase": "waiting_sell", "price": sell_p,
                    "shares": t_shares, "order_id": oid,
                }
                save_state(state)
                logger.info(f"正T触发 {code} +{chg*100:.1f}% → 挂卖{t_shares}股@{sell_p}")
        elif chg <= -TRIGGER:
            # 反T: 挂买@昨收*0.98（需现金）
            buy_p = round(prev * (1 - TRIGGER), 3)
            oid = client.place_order(code, "buy", t_shares, buy_p)
            if oid and oid > 0:
                state[code] = {
                    "date": today, "name": "", "direction": "反T",
                    "phase": "waiting_buy", "price": buy_p,
                    "shares": t_shares, "order_id": oid,
                }
                save_state(state)
                logger.info(f"反T触发 {code} {chg*100:.1f}% → 挂买{t_shares}股@{buy_p}")

    # 5. 收盘前撤单（14:50后撤销所有未成交挂单，避免隔夜）
    if t >= dtime(14, 50):
        for code, st in list(state.items()):
            if st.get("date") == today and st.get("phase") in ("waiting_sell", "waiting_buyback", "waiting_buy", "waiting_sellout"):
                oid = st.get("order_id")
                if oid:
                    client.cancel_order(oid)
                    logger.info(f"14:50撤单 {code} {st['phase']}")

    return state


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="跑一轮后退出")
    parser.add_argument("--loop", action="store_true", help="盘中循环")
    parser.add_argument("--interval", type=int, default=30, help="循环间隔秒")
    args = parser.parse_args()

    from execution.qmt_client import get_client
    client = get_client()
    logger.info(f"做T执行器启动 (QMT {'仿真' if os.getenv('ENV') != 'production' else '生产'})")

    if args.once:
        run_cycle(client)
        return

    if args.loop:
        while True:
            try:
                run_cycle(client)
            except Exception as e:
                logger.error(f"循环异常: {e}")
            time.sleep(args.interval)
        return

    # 默认跑一轮
    run_cycle(client)


if __name__ == "__main__":
    main()
