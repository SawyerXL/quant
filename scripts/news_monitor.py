"""
每小时持仓新闻检索 — 百度新闻搜索，利空/利好关键词
"""
import sys, re, requests, time, urllib.parse
from pathlib import Path
from datetime import datetime
sys.path.insert(0, str(Path(__file__).parent.parent))
from loguru import logger

HOLDINGS_FILE = Path("config/my_holdings.csv")
logger.add("logs/news_monitor.log", rotation="3 days")

BAD_KW = '减持|立案|调查|亏损|预亏|退市|制裁|处罚|诉讼|跌停|爆雷|停产|召回|下调|违规|警示函'
GOOD_KW = '增持|回购|预增|暴增|利好|涨价|补贴|中标|签约|获批|新品|扩产|收购|重组|超预期|上调'

def get_holdings():
    import pandas as pd
    df = pd.read_csv(HOLDINGS_FILE, dtype={'code': str})
    df['code'] = df['code'].str.zfill(6)
    return [(r['code'], r['name']) for _, r in df.iterrows() if r.get('monitor', True)]

def search_baidu(name, code):
    """百度新闻搜索"""
    alerts = []
    try:
        query = urllib.parse.quote(f'{name} {code} 股票')
        url = f'https://www.baidu.com/s?wd={query}&tn=news&rtt=1'
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0'}
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            return alerts

        # Extract text snippets
        text = resp.text
        # Baidu news results are in <div class="result"> or similar
        snippets = re.findall(r'<span class="content-right_[^"]*">(.*?)</span>', text, re.DOTALL)
        if not snippets:
            snippets = re.findall(r'<div class="c-abstract">(.*?)</div>', text, re.DOTALL)
        if not snippets:
            snippets = re.findall(r'class="c-font-normal[^"]*">(.*?)</', text, re.DOTALL)

        for s in snippets[:5]:
            clean = re.sub(r'<[^>]+>', '', s).replace('&nbsp;',' ').strip()
            if len(clean) < 10:
                continue
            for kw in BAD_KW.split('|'):
                if kw in clean:
                    alerts.append(f"🔴 [{code} {name}] {kw}: {clean[:120]}")
                    break
            else:
                for kw in GOOD_KW.split('|'):
                    if kw in clean:
                        alerts.append(f"🟢 [{code} {name}] {kw}: {clean[:120]}")
                        break
        return alerts
    except:
        return []

def run():
    logger.info("新闻检索开始(百度)")
    holdings = get_holdings()
    all_alerts = []

    for code, name in holdings[:10]:  # Limit to 10 to avoid rate limiting
        alerts = search_baidu(name, code)
        all_alerts.extend(alerts)
        time.sleep(3)

    seen = set(); unique = []
    for a in all_alerts:
        key = a[:60]
        if key not in seen:
            seen.add(key); unique.append(a)

    if unique:
        logger.info(f"发现 {len(unique)} 条预警")
        msg = f"【持仓新闻预警 {datetime.now().strftime('%m/%d %H:%M')}】\n" + "\n".join(unique[:6])
        try:
            from monitoring.alerts import send_alert
            send_alert(msg)
        except: pass
    else:
        logger.info("无异常新闻")

    # Print for verification
    for u in unique:
        print(u)
    if not unique:
        print("(无预警)")

if __name__ == '__main__':
    run()
