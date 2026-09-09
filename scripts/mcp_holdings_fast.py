"""
09:31 持仓资金快报 — 仅15只持仓(跳过跟踪股), MCP实时资金流+方向判定
速度优先: Sina实时价 + MCP资金流, 1分钟内出结果
输出: 代码/名称/现价/昨主力/今日主力/操作建议
"""
import sys, os, json, requests, pandas as pd
from pathlib import Path
from datetime import datetime, date
sys.path.insert(0, str(Path(__file__).parent.parent))
from loguru import logger
logger.add("logs/mcp_fast.log", rotation="3 days")

HOLDINGS_FILE = Path("config/my_holdings.csv")
CACHE_DIR = Path("logs/cache")

def sf(v):
    try: return float(v)
    except: return 0.0

def classify_direction(main_t, main_y):
    """主力方向判定: 抢筹/流入/均衡/流出/踩踏"""
    if abs(main_t) < 0.05:
        return '—', '—'

    # 昨出今进 → 抢筹
    if main_y < -1 and main_t > 0.5:
        return '🔥抢筹(反转)', '🟢加仓'
    # 持续大幅流入 → 抢筹
    if main_t > 2:
        return '🔥抢筹', '🟢加仓'
    # 大幅流出 → 踩踏
    if main_t < -5:
        return '💀踩踏', '🔴减仓'
    # 中等流出 + 昨也流出 → 踩踏
    if main_t < -2 and main_y < -1:
        return '💀持续流出', '🔴减仓'
    # 昨进今出 → 反转流出
    if main_y > 0.5 and main_t < -1:
        return '🔴反转流出', '🔴减仓'
    # 流入
    if main_t > 0.3:
        return '🟢流入', '✅持有'
    # 流出
    if main_t < -1:
        return '🔴流出', '🟡观察'
    # 均衡
    return '→均衡', '✅持有'

def run():
    today_str = date.today().strftime('%Y-%m-%d')
    now = datetime.now().strftime('%H:%M')
    logger.info(f"持仓快报 {today_str} {now}")

    # ── 仅持仓股票(shares>0, monitor=true), 跳过跟踪股 ──
    df = pd.read_csv(HOLDINGS_FILE, dtype={'code': str})
    df['code'] = df['code'].str.zfill(6)
    held = df[(df['monitor'] == True) & (df['shares'] > 0)]
    codes = held['code'].tolist()
    names = dict(zip(held['code'], held['name']))
    logger.info(f"持仓{len(codes)}只: {', '.join(codes)}")

    # ── 昨日资金流 ──
    yesterday_key = (date.today().replace(day=date.today().day - 1)).strftime('%Y%m%d')
    flow_yest = {}
    yf = CACHE_DIR / f"top60_flow_{yesterday_key}.json"
    if yf.exists():
        flow_yest = json.loads(yf.read_text()).get('flow', {})
        logger.info(f"昨日缓存: {len(flow_yest)}只")
    else:
        logger.warning(f"昨日缓存缺失: {yf}")

    # ── 今日MCP资金流(仅持仓, 不拉跟踪) ──
    from data.source.mcp_source import MCPSource
    mcp = MCPSource()
    flow_today = {}
    for code in codes:
        try:
            df_f = mcp.get_capital_flow(code, today_str)
            if df_f is not None and not df_f.empty:
                r = df_f.iloc[0]
                flow_today[code] = {
                    'main': sf(r.get('主力净额(万元)', 0)) / 1e4,  # 亿
                    'big': sf(r.get('超大单资金净额(万元)', 0)) / 1e4,
                    'large': sf(r.get('大单资金净额(万元)', 0)) / 1e4,
                    'small': sf(r.get('小单资金净额(万元)', 0)) / 1e4,
                }
        except Exception as e:
            logger.warning(f"MCP拉取失败 {code}: {e}")
    logger.info(f"今日MCP: {len(flow_today)}/{len(codes)}只")

    # ── Sina实时价格(批量, 最快) ──
    rt = {}
    for code in codes:
        exch = 'sh' if code.startswith(('6', '68')) else 'sz'
        try:
            r = requests.get(f'http://hq.sinajs.cn/list={exch}{code}',
                headers={'Referer': 'https://finance.sina.com.cn'}, timeout=3)
            flds = r.text.split('"')[1].split(',')
            if len(flds) >= 4 and sf(flds[3]) > 0:
                rt[code] = {
                    'cur': sf(flds[3]),
                    'chg': (sf(flds[3]) / sf(flds[2]) - 1) * 100 if sf(flds[2]) > 0 else 0,
                    'open': sf(flds[1]),
                    'high': sf(flds[4]),
                    'low': sf(flds[5]),
                }
        except Exception as e:
            logger.warning(f"新浪价格失败 {code}: {e}")

    # ── 构建邮件 ──
    lines = []
    lines.append(f"⚡ 持仓资金快报 {now}")
    lines.append("")

    # 大盘
    try:
        for sid, name in [('s_sh000001', '上证'), ('s_sz399006', '创业板')]:
            r_p = requests.get(f'http://hq.sinajs.cn/list={sid}',
                headers={'Referer': 'https://finance.sina.com.cn'}, timeout=3)
            flds = r_p.text.split('"')[1].split(',')
            lines.append(f"  {name}: {float(flds[1]):,.0f} ({float(flds[3]):+.2f}%)")
    except:
        pass
    lines.append("")

    # ── 持仓明细表 ──
    lines.append(f"{'代码':<8} {'名称':<8} {'现价':>7} {'昨主力':>8} {'今主力':>8} {'方向':<14} {'操作':<8}")
    lines.append(f"{'─' * 70}")

    actions = {'🟢加仓': [], '🔴减仓': [], '✅持有': [], '🟡观察': [], '—': []}

    for _, r in held.iterrows():
        code = r['code']
        name = r['name']
        shares = int(r['shares'])

        ft = flow_today.get(code, {})
        fy = flow_yest.get(code, {})
        main_t = ft.get('main', 0)
        main_y = fy.get('main', 0)

        ri = rt.get(code, {})
        cur = ri.get('cur', 0)
        chg = ri.get('chg', 0)

        direction, action = classify_direction(main_t, main_y)

        y_str = f'{main_y:+.1f}亿' if abs(main_y) > 0.01 else '—'
        t_str = f'{main_t:+.1f}亿' if abs(main_t) > 0.01 else '—'

        lines.append(f"{code:<8} {name:<8} {cur:>7.2f} {y_str:>8} {t_str:>8} {direction:<14} {action:<8}")

        actions[action].append(f'{code} {name}')

    # ── 操作汇总 ──
    lines.append("")
    lines.append(f"{'─' * 70}")
    if actions['🔴减仓']:
        lines.append(f"  🔴 减仓: {', '.join(actions['🔴减仓'])}")
    if actions['🟡观察']:
        lines.append(f"  🟡 观察: {', '.join(actions['🟡观察'])}")
    if actions['🟢加仓']:
        lines.append(f"  🟢 加仓: {', '.join(actions['🟢加仓'])}")
    if actions['✅持有']:
        lines.append(f"  ✅ 持有: {', '.join(actions['✅持有'])}")
    no_data = actions.get('—', [])
    if no_data:
        lines.append(f"  ⚠️ 无数据: {', '.join(no_data)}")

    lines.append("")
    lines.append(f"  ⏱️ 09:31快报 — 比0905跟踪分析快, 先决定持仓怎么动")

    body = '\n'.join(lines)

    # ── 发送邮件 ──
    try:
        from monitoring.alerts import send_alert
        send_alert(body)
        logger.info("持仓快报邮件已发送")
    except Exception as e:
        logger.error(f"邮件发送失败: {e}")

    print(body)
    return body

if __name__ == '__main__':
    run()
