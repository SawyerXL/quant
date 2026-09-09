"""
北向资金每日摘要 — akshare历史数据 + web新闻抓取
数据源: akshare(沪股通/深股通持股市值) + 新浪财经新闻
输出: 持股市值趋势 + 近期净买卖摘要 + 行业偏好
用法: python scripts/northbound_daily.py
"""
import sys, json, re, requests, pandas as pd, numpy as np
from pathlib import Path
from datetime import date, datetime, timedelta
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent))
logger.add("logs/northbound.log", rotation="7 days")

NB_DIR = Path("data_store")
CACHE_FILE = NB_DIR / "northbound_combined.parquet"

def sf(v):
    try: return float(v)
    except: return 0.0

def load_local():
    """加载本地北向资金数据"""
    if not CACHE_FILE.exists():
        return None, None
    combined = pd.read_parquet(CACHE_FILE)
    sh = pd.read_parquet(NB_DIR / "northbound_sh_daily.parquet") if (NB_DIR / "northbound_sh_daily.parquet").exists() else None
    sz = pd.read_parquet(NB_DIR / "northbound_sz_daily.parquet") if (NB_DIR / "northbound_sz_daily.parquet").exists() else None
    return combined, (sh, sz)

def update_local():
    """增量更新北向资金数据"""
    try:
        import akshare as ak
        today = date.today().strftime('%Y-%m-%d')

        for symbol, fname in [('沪股通', 'northbound_sh_daily'), ('深股通', 'northbound_sz_daily')]:
            df = ak.stock_hsgt_hist_em(symbol=symbol)
            df['日期'] = pd.to_datetime(df['日期'])
            df = df.rename(columns={'日期': 'date'})
            df.to_parquet(NB_DIR / f"{fname}.parquet", index=False)
            logger.info(f"{symbol}: {len(df)}行, 最新{df['date'].max().strftime('%Y-%m-%d')}")

        # Rebuild combined
        sh = pd.read_parquet(NB_DIR / 'northbound_sh_daily.parquet')
        sz = pd.read_parquet(NB_DIR / 'northbound_sz_daily.parquet')
        sh_d = sh.rename(columns={
            '当日成交净买额':'sh_net','买入成交额':'sh_buy','卖出成交额':'sh_sell',
            '历史累计净买额':'sh_cum','当日资金流入':'sh_in','持股市值':'sh_hold'})
        sz_d = sz.rename(columns={
            '当日成交净买额':'sz_net','买入成交额':'sz_buy','卖出成交额':'sz_sell',
            '历史累计净买额':'sz_cum','当日资金流入':'sz_in','持股市值':'sz_hold'})

        combined = pd.merge(
            sh_d[['date','sh_net','sh_buy','sh_sell','sh_cum','sh_hold']],
            sz_d[['date','sz_net','sz_buy','sz_sell','sz_cum','sz_hold']],
            on='date', how='outer').sort_values('date')
        combined['total_hold'] = combined['sh_hold'].fillna(0) + combined['sz_hold'].fillna(0)
        combined['total_cum'] = combined['sh_cum'].fillna(0) + combined['sz_cum'].fillna(0)
        combined.to_parquet(CACHE_FILE, index=False)
        logger.info(f"合并更新: {len(combined)}行")
        return True
    except Exception as e:
        logger.error(f"更新失败: {e}")
        return False

def fetch_web_news():
    """从新浪财经抓取北向资金相关新闻摘要"""
    items = []
    try:
        # 新浪财经-北向资金新闻
        urls = [
            "https://finance.sina.com.cn/stock/marketresearch/northbound/",
        ]
        for url in urls:
            try:
                r = requests.get(url, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
                r.encoding = 'utf-8'
                # Extract news titles
                titles = re.findall(r'<a[^>]*href="([^"]*)"[^>]*title="([^"]*)"[^>]*>', r.text)
                for href, title in titles:
                    if any(kw in title for kw in ['北向','外资','沪股通','深股通','北上','净买','净卖','流入','流出']):
                        items.append({'title': title.strip(), 'url': href.strip()})
            except Exception as e:
                logger.debug(f"URL失败 {url}: {e}")
    except Exception as e:
        logger.warning(f"新闻抓取失败: {e}")

    # Deduplicate
    seen = set()
    unique = []
    for item in items:
        if item['title'] not in seen:
            seen.add(item['title'])
            unique.append(item)
    return unique[:10]

def fetch_web_search_summary():
    """用web搜索获取近期北向资金流向摘要"""
    # This is a lightweight summary - for detailed analysis, use web search
    items = []
    try:
        # Try to get eastmoney northbound summary page
        r = requests.get(
            "https://data.eastmoney.com/hsgt/index.html",
            timeout=10,
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        )
        if r.status_code == 200:
            # Extract numbers using regex
            text = r.text

            # Net buy amounts
            net_amounts = re.findall(r'净买[入出].*?([\-\+]?\d+\.?\d*)\s*亿', text)
            if net_amounts:
                items.append(f"净买卖: {', '.join(net_amounts[:5])}亿")

            # Market values
            hold_values = re.findall(r'持股市值.*?(\d+\.?\d*)\s*亿', text)
            if hold_values:
                items.append(f"持股市值: {hold_values[0]}亿")

            # Sector preferences
            sectors = re.findall(r'(?:增持|减持).*?([一-龥]{2,6}).*?(\d+\.?\d*)\s*亿', text)
            if sectors:
                items.append(f"行业偏好: {', '.join(f'{s[0]}{s[1]}亿' for s in sectors[:5])}")
    except Exception as e:
        logger.debug(f"东财摘要失败: {e}")

    return items

def run():
    today_str = date.today().strftime('%Y-%m-%d')
    now = datetime.now().strftime('%H:%M')
    logger.info(f"北向资金日报 {today_str}")

    # Update local data
    update_ok = update_local()

    # Load
    combined, (sh, sz) = load_local()

    lines = []
    lines.append(f"🇭🇰 北向资金日报 {now}")
    lines.append("")

    # ── 持股市值趋势 ──
    if combined is not None:
        combined['date'] = pd.to_datetime(combined['date'])
        recent = combined[combined['date'] >= '2026-01-01'].dropna(subset=['total_hold'])
        if not recent.empty:
            last = recent.iloc[-1]
            # Find last valid holding value
            valid_hold = combined[combined['total_hold'] > 0]
            if not valid_hold.empty:
                last_hold = valid_hold.iloc[-1]
                # 30 days ago
                month_ago = valid_hold[valid_hold['date'] <= last_hold['date'] - timedelta(days=30)]
                if not month_ago.empty:
                    m_ago = month_ago.iloc[-1]
                    chg = (last_hold['total_hold'] / m_ago['total_hold'] - 1) * 100
                    lines.append(f"📊 持股市值: {last_hold['total_hold']/1e8:.0f}亿 (30日{chg:+.1f}%)")
                else:
                    lines.append(f"📊 持股市值: {last_hold['total_hold']/1e8:.0f}亿")
                lines.append(f"   数据日期: {last_hold['date'].strftime('%Y-%m-%d')}")
            lines.append("")

        # Show available data summary
        net_valid = combined[combined['sh_net'].notna() & (combined['sh_net'] != 0)]
        if not net_valid.empty:
            last_net = net_valid.iloc[-1]
            lines.append(f"⚠️ 每日净买额数据停在 {last_net['date'].strftime('%Y-%m-%d')}")
            lines.append(f"   之后{len(combined) - len(net_valid)}个交易日数据缺失")
        lines.append("")

    # ── Web新闻摘要 ──
    lines.append("📰 近期新闻:")
    news = fetch_web_news()
    if news:
        for n in news[:6]:
            lines.append(f"  · {n['title']}")
    else:
        lines.append(f"  (新闻抓取失败, 建议手动查看东方财富北向资金页面)")
    lines.append("")

    # ── Web数据摘要 ──
    web_data = fetch_web_search_summary()
    if web_data:
        lines.append("📈 数据摘要:")
        for d in web_data:
            lines.append(f"  {d}")
        lines.append("")

    # ── 说明 ──
    lines.append("─" * 50)
    lines.append("⚠️ 数据说明:")
    lines.append("  · akshare每日净买额自2024/8/19起缺失(东财接口变更)")
    lines.append("  · 持股市值最新到2026/3/31(季度更新)")
    lines.append("  · 实时北向需查看东方财富官网或证券APP")
    lines.append("  · 本报告仅做参考, 不构成投资建议")

    body = '\n'.join(lines)

    # Send email
    try:
        from monitoring.alerts import send_alert
        send_alert(body)
        logger.info("北向资金日报已发送")
    except Exception as e:
        logger.error(f"邮件失败: {e}")

    print(body)
    return body

if __name__ == '__main__':
    run()
