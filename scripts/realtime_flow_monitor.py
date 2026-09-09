"""
MCP 资金流秒级监控 — 每年3分钟拉一次核心持仓，发现反转立即告警
后台运行: nohup python scripts/realtime_flow_monitor.py &
"""
import sys, os, json, time
from pathlib import Path
from datetime import datetime, date
sys.path.insert(0, str(Path(__file__).parent.parent))
from loguru import logger

logger.add("logs/flow_monitor.log", rotation="1 day")

# Only 8 core stocks for speed
CORE_STOCKS = ['601138','002475','002008','601899','300059','600030','603993','600111']
# Previous day's flow for comparison
FLOW_YEST_FILE = Path("logs/cache/flow_prev.json")
POLL_SECONDS = 180  # every 3 minutes

def pull_mcp(codes):
    from data.source.mcp_source import MCPSource
    mcp = MCPSource()
    today = date.today().strftime('%Y-%m-%d')
    flow = {}
    for code in codes:
        try:
            df = mcp.get_capital_flow(code, today)
            if df is not None and not df.empty:
                r = df.iloc[0]
                flow[code] = {
                    'main': float(r.get('主力净额(万元)', 0)) / 1e4,
                    'big': float(r.get('超大单资金净额(万元)', 0)) / 1e4,
                    'time': str(r.get('交易时间', '?'))
                }
        except: pass
    return flow

def run():
    # Load yesterday's flow
    flow_yest = {}
    if FLOW_YEST_FILE.exists():
        flow_yest = json.loads(FLOW_YEST_FILE.read_text())

    prev_flow = {}
    first_run = True

    while True:
        try:
            now = datetime.now()
            # Only during trading hours
            if now.weekday() >= 5: 
                time.sleep(300); continue
            current = now.strftime('%H:%M')
            if not ('09:30' <= current <= '15:00'):
                time.sleep(60); continue

            flow = pull_mcp(CORE_STOCKS)
            if not flow: 
                time.sleep(POLL_SECONDS); continue

            if first_run:
                first_run = False
                prev_flow = flow
                logger.info(f"首轮: {len(flow)}只 {now.strftime('%H:%M:%S')}")
                time.sleep(POLL_SECONDS)
                continue

            # Check reversals vs previous poll
            alerts = []
            for code in CORE_STOCKS:
                ft = flow.get(code, {})
                fp = prev_flow.get(code, {})
                main_t = ft.get('main', 0)
                main_p = fp.get('main', 0)
                fy = flow_yest.get(code, {}).get('main', 0)

                if abs(main_t) < 0.01 or abs(main_p) < 0.01:
                    continue

                # Sudden reversal: was negative, now positive (or vice versa)
                if main_p < -0.5 and main_t > 0.5:
                    alerts.append(f'🔄 {code} 反转! {main_p:+.1f}→{main_t:+.1f}亿 (机构进场)')
                elif main_p > 0.5 and main_t < -0.5:
                    alerts.append(f'🔴 {code} 反转! {main_p:+.1f}→{main_t:+.1f}亿 (机构逃跑)')
                # Compare vs yesterday
                elif fy < -1 and main_t > 0.5:
                    alerts.append(f'🚨 {code} 昨出今进! {fy:+.0f}→{main_t:+.1f}亿')

            if alerts:
                msg = f"⚡ MCP {now.strftime('%H:%M')}\n" + "\n".join(alerts)
                logger.info(msg)
                try:
                    from monitoring.alerts import send_alert
                    send_alert(msg)
                except: pass

            # Save today's flow for next session
            prev_flow = flow
            # Cache simplified version
            simple = {c: {'main': f.get('main', 0)} for c, f in flow.items()}
            FLOW_YEST_FILE.parent.mkdir(exist_ok=True)
            FLOW_YEST_FILE.write_text(json.dumps(simple, ensure_ascii=False))

            time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"循环异常: {e}")
            time.sleep(30)

if __name__ == '__main__':
    run()
