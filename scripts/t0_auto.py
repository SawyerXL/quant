"""
做T自动执行器 — QMT仿真盘验证版
状态机: 每只持仓票独立跟踪 做T生命周期
铁律: 1/3仓位 | ±2%触发±1%了结 | 挂单 | 14:50尾盘了结(次日开盘兜底)
影子: G1止损口径(-1%含费-1.26%)只记录不执行, 验证"先触止损还是先触了结"
      (backtest_t0_tail_stop.py 两判读假设跨零轴+0.085%/-0.121%, 需实盘tick裁定)

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
SHADOW_LOG = ROOT / "config" / "t_shadow_stop_log.csv"

TRIGGER = 0.02   # ±2%触发
SETTLE = 0.01    # ±1%了结
FRAC = 1/3       # 1/3仓位
COMM = 0.0013 * 2  # 双边成本0.26%, 影子G1口径与回测一致
SHADOW_STOP = 0.01  # 影子止损: 反T买价×0.99 / 正T卖价×1.01 触到即记(不执行)
# 趋势过滤(回测最优): 跳空日跳过, 避免隔夜强平吃跳空亏损
GAP_SKIP_POS = 0.005  # 高开>0.5%不做正T
GAP_SKIP_NEG = 0.01   # 低开>1%不做反T
TAIL_CLOSE = dtime(14, 50)  # 尾盘了结时刻(机构标准做法: 不留隔夜T仓)
_last_empty_warn = 0.0  # 空持仓告警节流(秒时间戳)

# 板块涨跌停幅度(与execution/trader.py _BOARD_BANDS一致; ST票在此错算会被柜台拒,
# 次日兜底承接——持仓均为非ST白马, 风险可接受)
_BOARD_BANDS = {("60", "00"): 0.10, ("30", "68"): 0.20, ("8", "4", "92"): 0.30}


def _board_band(code: str) -> float:
    return next((b for pfx, b in _BOARD_BANDS.items() if str(code).startswith(pfx)), 0.10)

# QMT订单状态码: 48未报 49待报 50已报 51已报待撤 52撤单中 53已撤 54部撤 55部成 56已成 57废单
# Mock客户端返回字符串 "filled"，兼容两者
def _is_filled(status):
    return status in (55, 56) or str(status).lower() in ("filled", "partial", "部成", "已成")


def rt_price(code):
    """实时价+昨收+今开。优先xtdata本地QMT行情, sina兜底(绕系统代理)。
    返回 (cur, prev, opn); 开盘价拿不到时 opn=prev(按平开处理, 不过滤)。"""
    # 1. xtdata 本地行情(Windows有坏代理127.0.0.1:7892会卡死requests, 用这个最稳)
    try:
        from xtquant import xtdata
        xt = code + ('.SH' if code.startswith(('6', '5', '9', '11')) else '.SZ')
        xtdata.subscribe_quote(xt, period='tick')
        time.sleep(0.3)
        t = xtdata.get_full_tick([xt]).get(xt, {})
        cur = float(t.get('lastPrice') or 0)
        prev = float(t.get('lastClose') or 0)
        opn = float(t.get('open') or 0)
        if cur > 0 and prev > 0:
            return cur, prev, opn if opn > 0 else prev
    except Exception:
        pass
    # 2. sina 兜底(trust_env=False 绕过系统代理设置)
    exch = 'sh' if code.startswith(('5', '6', '9', '11')) else 'sz'
    try:
        r = requests.get(f'http://hq.sinajs.cn/list={exch}{code}',
                         headers={'Referer': 'https://finance.sina.com.cn'},
                         timeout=3, trust_env=False)
        r.encoding = 'gb2312'
        d = r.text.split('"')[1].split(',')
        cur = float(d[3]) if d[3] else 0
        prev = float(d[2]) if d[2] else 0
        opn = float(d[1]) if d[1] else 0
        if cur <= 0:
            cur = prev  # 未开盘用昨收
        return cur, prev, opn if opn > 0 else prev
    except Exception:
        return None, None, None


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state):
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def log_shadow(st, exit_kind, real_pnl_pct):
    """影子止损记录(G1口径, 只记不执行): 对比真实结果 vs 若加-1%止损会怎样。
    30秒轮询可能漏掉秒级折返→shadow_touched偏保守(漏记止损触发)。"""
    import pandas as pd
    touched = bool(st.get("shadow_stop_touched"))
    shadow_pnl = round(-(SHADOW_STOP + COMM) * 100, 3) if touched else round(real_pnl_pct, 3)
    rec = {
        "date": str(date.today()), "code": st.get("code", ""),
        "name": st.get("name", ""), "direction": st.get("direction", ""),
        "entry_price": st.get("price"), "shadow_touched": touched,
        "real_pnl_pct": round(real_pnl_pct, 3), "shadow_pnl_pct": shadow_pnl,
        "exit_kind": exit_kind,
    }
    if SHADOW_LOG.exists():
        df = pd.read_csv(SHADOW_LOG, dtype={"code": str})
        df = pd.concat([df, pd.DataFrame([rec])], ignore_index=True)
    else:
        df = pd.DataFrame([rec])
    df.to_csv(SHADOW_LOG, index=False)


def log_trade(code, name, direction, sell_p, buy_p, shares):
    """记录做T到CSV（与网页记录表同格式）。"""
    import pandas as pd
    pnl = (sell_p - buy_p) * shares
    rec = {
        "date": str(date.today()),
        "code": code, "name": name, "direction": direction,
        "sell_price": round(sell_p, 2), "buy_price": round(buy_p, 2),
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
    """循环有效窗口开到14:57: 14:50尾盘了结单的成交确认需要这7分钟。
    新信号扫描另有14:50闸门, 尾盘不会开新仓。"""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.time()
    return dtime(9, 30) <= t <= dtime(14, 57)


def settle_check(client, state):
    """次日强制了结：昨天没接回/没卖出的，市价平仓。
    双卖保护: 先核对实际持仓 — 多余股数已不在(卖单其实成交了)就不再卖, 只销状态。
    正T要买回(现仓<base), 反T要卖出(现仓>base)才执行。"""
    today = str(date.today())
    changed = False
    try:
        raw = client.get_positions()
        cur_vol = {x.split(".")[0]: int(v.get("volume", 0)) for x, v in raw.items()}
    except Exception:
        cur_vol = {}
    if not cur_vol:
        # 取不到持仓(连接断开)时不能做任何判断, 否则会把未卖的多余仓误判为"已卖"漂在账上
        logger.error("settle_check: 持仓读取失败, 跳过本轮了结")
        return state
    for code, st in list(state.items()):
        if st.get("date") == today:
            continue
        if st.get("phase") not in ("waiting_buyback", "waiting_sellout"):
            continue
        t_shares = int(st.get("shares", 0))
        has_base = st.get("base") is not None
        base = int(st.get("base", 0))
        # 涨跌停价限价单(2026-09-03教训: 市价单被仿真柜台废单; 跌停价=立即成交且永远合法)
        cur, prev, _ = rt_price(code)
        ref = prev if prev and prev > 0 else float(st.get("price", 0))
        band = _board_band(code)
        if st["direction"] == "反T":
            # 无base的旧条目不做跳过判断, 直接卖(宁卖勿漂)
            if has_base and cur_vol.get(code, 0) <= base:
                logger.info(f"{code} 反T多余仓已不在(卖单已成交), 只销状态不重复卖")
                st["phase"] = "force_settled"; st["settle_date"] = today; changed = True
                continue
            if ref <= 0:
                continue
            down_px = ref * (1 - band)
            if cur and cur <= down_px:  # 跌停封死禁卖(红线), 下轮再试
                logger.warning(f"{code} 开盘跌停, 隔夜卖出延后")
                continue
            oid = client.place_order(code, "sell", t_shares, down_px)
        else:  # 正T: 卖飞了没接回 → 买回
            if has_base and cur_vol.get(code, 0) >= base:
                logger.info(f"{code} 正T已接回, 只销状态")
                st["phase"] = "force_settled"; st["settle_date"] = today; changed = True
                continue
            if ref <= 0:
                continue
            up_px = ref * (1 + band)
            if cur and cur >= up_px:  # 涨停封死禁买(红线), 下轮再试
                logger.warning(f"{code} 开盘涨停, 隔夜买回延后")
                continue
            oid = client.place_order(code, "buy", t_shares, up_px)
        logger.warning(f"隔夜未了结 {code} {st['direction']} → 涨跌停价限价平仓")
        if oid and oid > 0:
            # 不直接标force_settled: 等成交后用实际成交价记账(亏损也必须入CSV)
            st["phase"] = "force_settle_pending"
            st["order_id"] = oid
            st["settle_date"] = today
            st["date"] = today  # 必须更新date, 否则第3步成交检测(date==today)会跳过它
            changed = True
    if changed:
        save_state(state)
    return state


def run_cycle(client, quiet=False):
    """一轮状态机检查。"""
    state = load_state()
    today = str(date.today())
    now = datetime.now()
    t = now.time()

    if not is_trading_time():
        return state, 0

    # 1. 隔夜未了结强制平仓(不限9:30-9:36, 任务晚启动也能了结)
    if any(st.get("date") != today and st.get("phase") in ("waiting_buyback", "waiting_sellout")
           for st in state.values()):
        state = settle_check(client, state)

    # 2. 获取持仓和今日订单
    raw = client.get_positions()
    positions = {
        x.split(".")[0]: v for x, v in raw.items()
        if v.get("volume", 0) > 0 and not x.startswith("888")  # 排除国债逆回购等
    }
    orders = client.get_today_orders()
    # 护栏: QMT连接静默断开时positions为空, 循环会空转一天不报错 → 周期性告警
    global _last_empty_warn
    if not positions:
        if time.time() - _last_empty_warn > 600:
            logger.error("持仓为空! QMT连接可能断开, 做T执行器空转中")
            _last_empty_warn = time.time()
        return state, 0

    # 2.5 清理陈旧状态(昨日force_settled/首腿未成交的), 防止封锁今日触发
    # 但首腿若其实已成交(实仓有变化), 不能删——转隔夜了结, 否则多余股数漂在账上
    pruned = False
    for code in list(state.keys()):
        st = state.get(code, {})
        if st.get("date") == today:
            continue
        base = int(st.get("base", 0))
        cur = int(positions.get(code, {}).get("volume", 0))
        if st.get("phase") == "force_settle_pending":
            # 昨尾盘了结单未确认成交: 核对实仓。未闭环→转隔夜了结兜底, 已闭环→销状态
            if st.get("direction") == "反T" and st.get("base") is not None and cur > base:
                st["phase"] = "waiting_sellout"; st["order_id"] = None
                logger.warning(f"{code} 昨尾盘了结未成交, 转隔夜卖出")
                pruned = True
                continue
            if st.get("direction") == "正T" and st.get("base") is not None and cur < base:
                st["phase"] = "waiting_buyback"; st["order_id"] = None
                logger.warning(f"{code} 昨尾盘了结未成交, 转隔夜买回")
                pruned = True
                continue
            del state[code]
            pruned = True
            continue
        if st.get("phase") == "waiting_buy" and st.get("base") is not None and cur > base:
            st["phase"] = "waiting_sellout"  # 买单已成交没检测到 → 隔夜卖出
            logger.warning(f"{code} 昨反T买单已成交未了结, 转隔夜卖出")
            pruned = True
            continue
        if st.get("phase") == "waiting_sell" and st.get("base") is not None and cur < base:
            st["phase"] = "waiting_buyback"  # 卖单已成交没检测到 → 隔夜买回
            logger.warning(f"{code} 昨正T卖单已成交未接回, 转隔夜买回")
            pruned = True
            continue
        if st.get("phase") in ("force_settled", "done_today", "waiting_buy", "waiting_sell"):
            del state[code]
            pruned = True
    if pruned:
        save_state(state)

    # 3. 检查已有挂单成交情况
    for code, st in list(state.items()):
        if st.get("date") != today:
            continue
        phase = st.get("phase")
        oid = st.get("order_id")
        # 订单终端状态: 53已撤 / 57废单 → 首腿(没成交任何股)可直接销状态重新触发
        if phase in ("waiting_sell", "waiting_buy"):
            terminal = any(o.get("order_id") == oid and o.get("status") in (53, 57) for o in orders)
            if terminal:
                logger.info(f"{code} 挂单废单/已撤, 销状态允许重触发")
                del state[code]
                save_state(state)
                continue
        if phase == "waiting_sell":
            for o in orders:
                if o.get("order_id") == oid and _is_filled(o.get("status")):
                    # 卖成交 → 挂接回买单
                    sell_p = st["price"]
                    buy_p = round(sell_p * (1 - SETTLE), 2)
                    nb = client.place_order(code, "buy", st["shares"], buy_p)
                    st["phase"] = "waiting_buyback"
                    st["buy_price"] = buy_p
                    st["order_id"] = nb
                    save_state(state)
                    logger.info(f"{code} 卖单成交@{sell_p} → 挂接回@{buy_p}")
                    break
        elif phase == "waiting_buyback":
            # 影子止损(G1口径, 只记不执行): 正T卖飞, 价涨超卖价1%→记shadow_touched
            cur_sh, _, _ = rt_price(code)
            if cur_sh and cur_sh >= st["price"] * (1 + SHADOW_STOP):
                st["shadow_stop_touched"] = True
            # 兜底: 订单状态查不到成交, 但实仓已回base → 视为已接回
            base = int(st.get("base", 0))
            filled = any(o.get("order_id") == oid and _is_filled(o.get("status")) for o in orders)
            if not filled and positions.get(code, {}).get("volume", 0) >= base:
                filled = True
            if filled:
                sell_p = st["price"]
                buy_p = st["buy_price"]
                log_trade(code, st.get("name", ""), "正T", sell_p, buy_p, st["shares"])
                log_shadow(st, "当日了结", (sell_p - buy_p) / sell_p * 100)
                state[code] = {"date": today, "code": code, "phase": "done_today"}
                save_state(state)
        elif phase == "waiting_buy":
            for o in orders:
                if o.get("order_id") == oid and _is_filled(o.get("status")):
                    buy_p = st["price"]
                    sell_p = round(buy_p * (1 + SETTLE), 2)
                    ns = client.place_order(code, "sell", st["shares"], sell_p)
                    st["phase"] = "waiting_sellout"
                    st["sell_price"] = sell_p
                    st["order_id"] = ns
                    save_state(state)
                    logger.info(f"{code} 买单成交@{buy_p} → 挂卖出@{sell_p}")
                    break
        elif phase == "waiting_sellout":
            # 影子止损(G1口径, 只记不执行): 反T买套, 价跌破买价1%→记shadow_touched
            cur_sh, _, _ = rt_price(code)
            if cur_sh and cur_sh <= st["price"] * (1 - SHADOW_STOP):
                st["shadow_stop_touched"] = True
            # 兜底: 订单状态查不到成交, 但实仓已回base → 视为已卖出
            base = int(st.get("base", 0))
            filled = any(o.get("order_id") == oid and _is_filled(o.get("status")) for o in orders)
            if not filled and positions.get(code, {}).get("volume", 0) <= base:
                filled = True
            if filled:
                sell_p = st["sell_price"]
                buy_p = st["price"]
                log_trade(code, st.get("name", ""), "反T", sell_p, buy_p, st["shares"])
                log_shadow(st, "当日了结", (sell_p - buy_p) / buy_p * 100)
                state[code] = {"date": today, "code": code, "phase": "done_today"}
                save_state(state)
        elif phase == "force_settle_pending":
            # 强平成交后按实际成交价记账(亏损也入CSV, 防止胜率虚高) + 影子对照
            for o in orders:
                if o.get("order_id") == oid and _is_filled(o.get("status")):
                    fill_p = float(o.get("price", 0) or 0)
                    if fill_p <= 0:
                        fill_p = float(o.get("filled_price", 0) or 0)
                    if st.get("direction") == "反T":
                        # 强平卖出: 卖价=实际成交, 买价=原反T买入价
                        if fill_p > 0:
                            log_trade(code, st.get("name", ""), "反T", fill_p, st["price"], st["shares"])
                            log_shadow(st, "尾盘了结" if st.get("tail_close") else "隔夜强平",
                                       (fill_p - st["price"]) / st["price"] * 100)
                    else:
                        # 强平买回: 买价=实际成交, 卖价=原正T卖出价
                        if fill_p > 0:
                            log_trade(code, st.get("name", ""), "正T", st["price"], fill_p, st["shares"])
                            log_shadow(st, "尾盘了结" if st.get("tail_close") else "隔夜强平",
                                       (st["price"] - fill_p) / st["price"] * 100)
                    st["phase"] = "force_settled"
                    save_state(state)
                    break

    # 4. 新信号扫描（只对空闲状态的票）
    # 铁律: 单日单票只做1次
    done_today = {
        st.get("code") for st in state.values()
        if st.get("phase") == "done_today"
    }
    for code, pos in positions.items():
        if t >= TAIL_CLOSE:
            break  # 14:50后不开新仓, 尾盘只做了结(了结腿有7分钟成交确认窗口)
        if code in state or code in done_today:
            continue  # 已在做T流程中 或 今日已完成
        cur, prev, opn = rt_price(code)
        if not cur or not prev or prev <= 0:
            continue
        chg = (cur / prev - 1)
        gap = (opn / prev - 1) if opn and prev else 0.0
        vol = int(pos.get("volume", 0))
        t_shares = int(vol * FRAC / 100) * 100  # 1/3仓位取整百
        if t_shares < 100:
            continue

        # 趋势过滤(回测backtest_t0_variants.py最优): 高开>0.5%跳过正T, 低开<-1%跳过反T。
        # 跳空日趋势性强, 了结腿大概率等不到→隔夜强平吃跳空亏损, 不做最划算。
        if chg >= TRIGGER and gap > GAP_SKIP_POS:
            continue
        if chg <= -TRIGGER and gap < -GAP_SKIP_NEG:
            continue

        if chg >= TRIGGER:
            # 正T: 挂卖@昨收*1.02
            sell_p = round(prev * (1 + TRIGGER), 2)
            oid = client.place_order(code, "sell", t_shares, sell_p)
            if oid and oid > 0:
                state[code] = {
                    "date": today, "name": "", "direction": "正T",
                    "phase": "waiting_sell", "price": sell_p,
                    "shares": t_shares, "order_id": oid, "base": vol,
                }
                save_state(state)
                logger.info(f"正T触发 {code} +{chg*100:.1f}% → 挂卖{t_shares}股@{sell_p}")
        elif chg <= -TRIGGER:
            # 反T: 挂买@昨收*0.98（需现金）
            buy_p = round(prev * (1 - TRIGGER), 2)
            oid = client.place_order(code, "buy", t_shares, buy_p)
            if oid and oid > 0:
                state[code] = {
                    "date": today, "name": "", "direction": "反T",
                    "phase": "waiting_buy", "price": buy_p,
                    "shares": t_shares, "order_id": oid, "base": vol,
                }
                save_state(state)
                logger.info(f"反T触发 {code} {chg*100:.1f}% → 挂买{t_shares}股@{buy_p}")

    # 5. 14:50尾盘了结(替代次日开盘强平): 未闭环T仓涨跌停价限价单立即了结,
    #    跌停/涨停封死(红线)或了结单未成交 → 次日settle_check兜底
    if t >= TAIL_CLOSE:
        for code, st in list(state.items()):
            if st.get("date") != today:
                continue
            phase = st.get("phase")
            base = int(st.get("base", 0) or 0)
            cur_vol = int(positions.get(code, {}).get("volume", 0))
            if phase in ("waiting_sell", "waiting_buy"):
                # 首腿没成交: 撤单。实仓已变化(成交未回传)→转尾盘了结; 否则销状态
                oid = st.get("order_id")
                if oid:
                    client.cancel_order(oid)
                if st["direction"] == "正T" and cur_vol < base:
                    st["phase"] = "waiting_buyback"; st["order_id"] = None
                    logger.warning(f"{code} 正T首腿已成交未检测, 14:50转尾盘买回")
                elif st["direction"] == "反T" and cur_vol > base:
                    st["phase"] = "waiting_sellout"; st["order_id"] = None
                    logger.warning(f"{code} 反T首腿已成交未检测, 14:50转尾盘卖出")
                else:
                    del state[code]
                    logger.info(f"14:50撤单销状态 {code} {phase}")
                    continue
            if st.get("phase") not in ("waiting_buyback", "waiting_sellout"):
                continue
            oid = st.get("order_id")
            if oid:
                client.cancel_order(oid)  # 撤限价了结单, 防与尾盘单双成交
            cur, prev, _ = rt_price(code)
            ref = prev if prev and prev > 0 else float(st.get("price", 0))
            if ref <= 0:
                continue
            band = _board_band(code)
            t_shares = int(st.get("shares", 0))
            if st["direction"] == "反T":
                down_px = ref * (1 - band)
                if cur and cur <= down_px:  # 跌停封死禁卖(红线), 留次日兜底
                    logger.warning(f"{code} 尾盘跌停封死, 卖出延至次日")
                    continue
                oid2 = client.place_order(code, "sell", t_shares, down_px)
            else:
                up_px = ref * (1 + band)
                if cur and cur >= up_px:  # 涨停封死禁买(红线), 留次日兜底
                    logger.warning(f"{code} 尾盘涨停封死, 买回延至次日")
                    continue
                oid2 = client.place_order(code, "buy", t_shares, up_px)
            if oid2 and oid2 > 0:
                st["phase"] = "force_settle_pending"
                st["order_id"] = oid2
                st["settle_date"] = today
                st["tail_close"] = True
                save_state(state)
                logger.warning(f"14:50尾盘了结 {code} {st['direction']} 涨跌停价限价单")

    return state, len(positions)


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
        empty_streak = 0
        while True:
            try:
                n_pos = run_cycle(client)
                # 心跳: 每轮写时间戳, 看门狗靠它发现假死(卡在QMT调用里心跳会停)
                try:
                    Path(STATE_FILE).parent.joinpath("t0_heartbeat.json").write_text(
                        json.dumps({"ts": int(time.time() * 1000)}))
                except Exception:
                    pass
                # 空持仓自愈: 连续3轮(约90秒)空持仓 → 重建QMT连接
                if n_pos == 0:
                    empty_streak += 1
                    if empty_streak >= 3:
                        logger.error("连续3轮空持仓, 重建QMT连接")
                        try: client.disconnect()
                        except Exception: pass
                        client = get_client()
                        empty_streak = 0
                else:
                    empty_streak = 0
            except Exception as e:
                logger.error(f"循环异常: {e}")
            # 15:05自行退出: /ET /K杀的是cmd壳, python子进程会变孤儿占住任务槽,
            # 导致次日9:30新实例不启动(8/27事故)。自退后任务干净结束。
            if datetime.now().time() >= dtime(15, 5):
                logger.info("收盘, 执行器自行退出")
                break
            time.sleep(args.interval)
        return

    # 默认跑一轮
    run_cycle(client)


if __name__ == "__main__":
    main()
