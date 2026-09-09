"""
实时行情守护进程 — 每30秒拉新浪行情，十维条件触发邮件告警
用法: nohup python scripts/realtime_monitor_daemon.py &
"""
import sys, os, json, time, re, requests, pandas as pd, numpy as np
from pathlib import Path
from datetime import datetime, date
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent))

HOLDINGS_FILE = Path("config/my_holdings.csv")
STATE_FILE = Path("logs/realtime_state.json")
POLL_INTERVAL = 30  # seconds
TRADE_START = "09:25"
TRADE_END = "15:05"
ALERT_COOLDOWN_MINUTES = 30  # batch alerts every 30 min, not every cycle
EMAIL_MIN_INTERVAL = 1800  # minimum seconds between emails

logger.add("logs/realtime_monitor.log", rotation="1 day", retention="3 days")

# ── Get all monitored stocks ──
def get_codes():
    df = pd.read_csv(HOLDINGS_FILE, dtype={"code": str})
    df["code"] = df["code"].str.zfill(6)
    active = df[df["monitor"] == True]
    codes = []
    for _, r in active.iterrows():
        codes.append({
            "code": r["code"], "name": r["name"],
            "cost": r["cost_price"] if pd.notna(r.get("cost_price")) else None,
            "shares": int(r["shares"])
        })
    return codes

def fetch_sina(codes):
    """批量拉取新浪行情"""
    ids = []
    for c in codes:
        exch = "sh" if c["code"].startswith("6") else "sz"
        ids.append(f"{exch}{c['code']}")

    url = f"http://hq.sinajs.cn/list={','.join(ids)}"
    try:
        resp = requests.get(url, headers={"Referer": "https://finance.sina.com.cn"}, timeout=8)
        resp.encoding = "gb2312"
    except:
        return {}

    results = {}
    for line in resp.text.strip().split("\n"):
        if not line or "=" not in line:
            continue
        try:
            sid = line.split("=")[0].split("_")[-1]
            code = sid[2:]  # strip sh/sz prefix
            data = line.split('"')[1].split(",")
            if len(data) < 10 or not data[3]:
                continue
            results[code] = {
                "name": data[0], "cur": float(data[3]),
                "prev": float(data[2]) if data[2] else 0,
                "open": float(data[1]) if data[1] else 0,
                "high": float(data[4]) if data[4] else 0,
                "low": float(data[5]) if data[5] else 0,
                "volume": int(data[8]) if data[8] and data[8].isdigit() else 0,
                "b1v": int(data[12]) if len(data) > 12 and data[12] and data[12].isdigit() else 0,
                "s1v": int(data[22]) if len(data) > 22 and data[22] and data[22].isdigit() else 0,
            }
        except:
            continue
    return results

def check_alerts(snapshots, codes_meta, prev_state, total_nav=280000):
    """十维条件检查——触发告警"""
    alerts = []
    new_state = {}

    for meta in codes_meta:
        code = meta["code"]; name = meta["name"]
        snap = snapshots.get(code)
        if not snap:
            continue

        cur = snap["cur"]; prev_close = snap["prev"]
        cost = meta.get("cost"); shares = meta["shares"]
        chg = (cur / prev_close - 1) * 100 if prev_close > 0 else 0
        pnl_pct = (cur / cost - 1) * 100 if cost else 0

        prev = prev_state.get(code, {})
        prev_chg = prev.get("chg", 0)
        new_state[code] = {"cur": cur, "chg": chg, "pnl_pct": pnl_pct, "time": datetime.now().strftime("%H:%M:%S")}

        # ── 止损触发 ──
        if cost and pnl_pct <= -12:
            if not prev.get("stop_alerted"):
                alerts.append(f"🔴 [{code} {name}] 止损触发! 成本¥{cost:.2f} 现价¥{cur:.2f} ({pnl_pct:+.1f}%)")
                new_state[code]["stop_alerted"] = True

        # ── 涨跌停 ──
        if chg >= 9.8:
            b1v = snap.get("b1v", 0)
            if not prev.get("limit_up_alerted"):
                seal_strength = "极强" if b1v > 100000 else ("强" if b1v > 50000 else "弱")
                alerts.append(f"🔥 [{code} {name}] 涨停封板! ¥{cur:.2f} 封单{b1v}手({seal_strength})")
                new_state[code]["limit_up_alerted"] = True
            # Check board opening
            was_limit = prev.get("was_limit_up", False)
            if was_limit and chg < 9.5:
                alerts.append(f"⚠️ [{code} {name}] 涨停开板! ¥{cur:.2f} ({chg:+.1f}%) — 建议立即卖出")
                new_state[code]["was_limit_up"] = False
            else:
                new_state[code]["was_limit_up"] = True
        else:
            new_state[code]["was_limit_up"] = False
            new_state[code]["limit_up_alerted"] = prev.get("limit_up_alerted", False)

        if chg <= -9.8 and not prev.get("limit_down_alerted"):
            alerts.append(f"🔻 [{code} {name}] 跌停! ¥{cur:.2f}")
            new_state[code]["limit_down_alerted"] = True

        # ── 振幅异常 ──
        amp = (snap["high"] / snap["low"] - 1) * 100 if snap["low"] > 0 else 0
        if amp > 10 and not prev.get("amp_alerted"):
            alerts.append(f"⚡ [{code} {name}] 极端振幅{amp:.1f}%! 高¥{snap['high']:.2f} 低¥{snap['low']:.2f}")
            new_state[code]["amp_alerted"] = True

        # ── 仓位再平衡告警 ──
        if shares > 0 and cur > 0:
            pct = cur * shares / total_nav * 100 if total_nav > 0 else 0
            if pct > 20 and not prev.get("rebalance_20"):
                alerts.append(f"⚖️ [{code} {name}] 仓位{pct:.0f}%严重超标(>20%)! 建议减仓")
                new_state[code]["rebalance_20"] = True
            elif pct > 15 and not prev.get("rebalance_15"):
                alerts.append(f"🟡 [{code} {name}] 仓位{pct:.0f}%偏重(>15%) 注意集中度风险")
                new_state[code]["rebalance_15"] = True

        # ── 大单异动 (秒级) ──
        amount = cur * snap['volume'] if snap['volume']>0 and cur>0 else 0
        prev_amt = prev.get('last_amount', 0)
        if amount > 0 and prev_amt > 0:
            burst = (amount - prev_amt) / prev_amt
            # 成交量突增 50%+ → 可能有机构
            if burst > 0.5 and not prev.get('bigorder_alerted'):
                if chg > 0: alerts.append(f"📈 [{code} {name}] 放量+{burst*100:.0f}%上涨 ¥{cur:.2f} — 疑似机构买入")
                elif chg < -1: alerts.append(f"📉 [{code} {name}] 放量+{burst*100:.0f}%下跌 ¥{cur:.2f} — 疑似机构卖出")
                new_state[code]['bigorder_alerted'] = True
        new_state[code]['last_amount'] = amount

        # ── 盘口突变 ──
        prev_b1 = prev.get('last_b1', 0)
        prev_s1 = prev.get('last_s1', 0)
        if b1v > 0 and s1v > 0:
            ratio = b1v / max(s1v, 1)
            prev_ratio = prev_b1 / max(prev_s1, 1) if prev_s1 > 0 else 1
            # 买盘突然增强 2x+ → 机构可能在吃货
            if ratio > 3 and prev_ratio < 1.5 and not prev.get('bidwall_alerted'):
                alerts.append(f"🧱 [{code} {name}] 买盘暴增! ¥{cur:.2f} 买一{b1v}手/卖一{s1v}手")
                new_state[code]['bidwall_alerted'] = True
        new_state[code]['last_b1'] = b1v
        new_state[code]['last_s1'] = s1v

        # ── 日内反转 ──
        if prev_chg < -3 and chg > 2:
            alerts.append(f"🔄 [{code} {name}] 日内V型反转! ({prev_chg:+.1f}%→{chg:+.1f}%) — 主力可能在抄底")

        if prev_chg > 5 and chg < -2:
            alerts.append(f"📉 [{code} {name}] 高开低走! ({prev_chg:+.1f}%→{chg:+.1f}%) — 警惕出货")

    return alerts, new_state

def should_run():
    """只在交易时段运行"""
    now = datetime.now()
    if now.weekday() >= 5:  # 周末
        return False
    current = now.strftime("%H:%M")
    # 集合竞价前5分钟到收盘后5分钟
    if "09:20" <= current <= "15:05":
        return True
    return False

def run():
    logger.info("实时监控守护进程启动")
    codes_meta = get_codes()
    logger.info(f"监测股票: {len(codes_meta)}只")

    # Load previous state
    prev_state = {}
    if STATE_FILE.exists():
        try:
            prev_state = json.loads(STATE_FILE.read_text())
        except:
            pass

    alert_cooldown = {}  # 防重复告警：同类型告警60分钟内不重复
    last_email_time = 0  # throttle emails

    while True:
        try:
            if should_run():
                snapshots = fetch_sina(codes_meta)
                if snapshots:
                    alerts, new_state = check_alerts(snapshots, codes_meta, prev_state)
                    prev_state = new_state

                    # Filter cooldown
                    now = time.time()
                    real_alerts = []
                    for a in alerts:
                        akey = a[:50]  # key by prefix
                        last = alert_cooldown.get(akey, 0)
                        if now - last > 3600:  # 60 min cooldown
                            real_alerts.append(a)
                            alert_cooldown[akey] = now

                    if real_alerts:
                        logger.info(f"告警: {len(real_alerts)}条")

                        # Throttle: only send email every EMAIL_MIN_INTERVAL seconds
                        if now - last_email_time >= EMAIL_MIN_INTERVAL:
                            # Save state
                            STATE_FILE.parent.mkdir(exist_ok=True)
                            STATE_FILE.write_text(json.dumps(prev_state, ensure_ascii=False))

                            # Batch all alerts into one email
                            msg = f"【实时监控 {datetime.now().strftime('%H:%M')}】\n" + "\n".join(real_alerts)
                            try:
                                from monitoring.alerts import send_alert
                                send_alert(msg)
                                last_email_time = now
                                logger.info("邮件已发送")
                            except Exception as e:
                                logger.error(f"邮件发送失败: {e}")
                        else:
                            logger.info(f"已抑制邮件 (距上次{int(now-last_email_time)}秒)")

            time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            logger.info("守护进程已停止")
            break
        except Exception as e:
            logger.error(f"循环异常: {e}")
            time.sleep(30)

if __name__ == "__main__":
    run()
