"""
收盘后缓存TOP60成交额股票的资金流数据 (MCP)
用法: python scripts/sector_flow_cache.py
输出: logs/cache/top60_flow_YYYYMMDD.json
"""
import sys, os, json, pandas as pd, numpy as np
from pathlib import Path
from datetime import date, datetime
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent))
logger.add("logs/sector_flow.log", rotation="7 days")

CACHE_DIR = Path("logs/cache")

def fetch_top60_flow():
    """拉取TOP60成交额股票的资金流数据"""
    from data.storage import load_meta
    from scripts.run_backtest_a import load_panels

    c800 = load_meta('csi800')
    codes = sorted(c800['code'].tolist())[:800]
    _, ap = load_panels(codes, '2019-01-01', date.today().strftime('%Y-%m-%d'))

    # TOP60 by recent turnover
    top60 = ap.iloc[-20:].mean().dropna().nlargest(60).index.tolist()
    logger.info(f"TOP60成交额: {len(top60)}只, 拉取MCP资金流...")

    flow = {}
    from data.source.mcp_source import MCPSource
    mcp = MCPSource()
    # Allow date override via env var
    target_date = os.environ.get('FLOW_DATE', date.today().strftime('%Y-%m-%d'))
    logger.info(f"拉取日期: {target_date}")

    for i, code in enumerate(top60):
        try:
            df = mcp.get_capital_flow(code, target_date)
            if df is not None and not df.empty:
                r = df.iloc[0]
                # Use yesterday's data if today not available (pre-market)
                date_used = r.get('交易时间', r.get('交易日期', target_date))
                flow[code] = {
                    'main': float(r.get('主力净额(万元)', 0)) / 1e4,
                    'big': float(r.get('超大单资金净额(万元)', 0)) / 1e4,
                    'large': float(r.get('大单资金净额(万元)', 0)) / 1e4,
                    'small': float(r.get('小单资金净额(万元)', 0)) / 1e4,
                    'date': str(date_used),
                }
            if (i+1) % 10 == 0:
                logger.info(f"  进度: {i+1}/{len(top60)}")
        except Exception as e:
            logger.debug(f"  {code}: {e}")

    # Also try to get yesterday's data for stocks that failed today
    from datetime import timedelta
    target_dt = date.fromisoformat(target_date)
    yesterday = (target_dt - timedelta(days=1)).strftime('%Y-%m-%d')
    missing = [c for c in top60 if c not in flow]
    if missing:
        logger.info(f"  补拉昨日数据 for {len(missing)} stocks...")
        for code in missing:
            try:
                df = mcp.get_capital_flow(code, yesterday)
                if df is not None and not df.empty:
                    r = df.iloc[0]
                    flow[code] = {
                        'main': float(r.get('主力净额(万元)', 0)) / 1e4,
                        'big': float(r.get('超大单资金净额(万元)', 0)) / 1e4,
                        'large': float(r.get('大单资金净额(万元)', 0)) / 1e4,
                        'small': float(r.get('小单资金净额(万元)', 0)) / 1e4,
                        'date': yesterday,
                    }
            except: pass

    return flow, top60

def run():
    target_d = os.environ.get('FLOW_DATE', date.today().strftime('%Y-%m-%d'))
    short = target_d.replace('-', '')
    logger.info(f"TOP60资金流缓存 {target_d}")

    flow, top60 = fetch_top60_flow()

    CACHE_DIR.mkdir(exist_ok=True)
    output = {
        'date': target_d,
        'top60': top60,
        'flow': flow,
        'count': len(flow),
    }
    cache_file = CACHE_DIR / f"top60_flow_{short}.json"
    cache_file.write_text(json.dumps(output, ensure_ascii=False, indent=2))

    logger.info(f"已缓存: {len(flow)}/{len(top60)}只  → {cache_file}")

    # Quick sector summary
    from data.storage import load_meta
    info = load_meta('stock_info_full')
    ind_map = {}
    for _, r in info.iterrows():
        ind_map[str(r['code'])] = r.get('industry_l1', '?')

    sec_flows = {}
    for code, f in flow.items():
        ind = ind_map.get(code, '其他')
        sec_flows[ind] = sec_flows.get(ind, 0) + f['main']

    print(f"\nTOP60 板块资金流 (昨日):")
    for sec, amt in sorted(sec_flows.items(), key=lambda x: -x[1])[:8]:
        print(f"  {sec:<8} {amt:>+7.1f}亿")

if __name__ == '__main__':
    run()
