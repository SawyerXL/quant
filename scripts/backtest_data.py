"""
数据层 — 从本地日线parquet构建价格/成交额面板
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd, numpy as np
from pathlib import Path
from loguru import logger; logger.remove(); logger.add(sys.stderr, level='ERROR')

DAILY_DIR = Path("data_store/daily")

def _is_stock(code: str) -> bool:
    return code.startswith(('000','001','002','003','300','301','600','601','602','603','605','688'))


def get_price_panel(start: str, end: str, min_bars: int = 250):
    """
    构建价格面板和成交额面板。
    数据源：data_store/daily/{year}/{code}.parquet
    返回: (price_panel, amount_panel)
    """
    # First pass: collect all data per stock across years
    stock_data = {}  # code -> list of DataFrames

    for year_dir in sorted(DAILY_DIR.glob("20*")):
        if not year_dir.is_dir(): continue
        for fpath in year_dir.glob("*.parquet"):
            code = fpath.stem
            if not _is_stock(code): continue
            try:
                df = pd.read_parquet(fpath)
                if 'date' not in df.columns: continue
                df['date'] = pd.to_datetime(df['date'])
                df = df.set_index('date').sort_index()
                if code not in stock_data:
                    stock_data[code] = []
                stock_data[code].append(df)
            except Exception:
                continue

    # Second pass: concat per stock, filter date range, check min_bars
    prices = {}; amounts = {}
    for code, dfs in stock_data.items():
        try:
            full = pd.concat(dfs).sort_index()
            full = full[~full.index.duplicated(keep='last')]
            full = full[(full.index >= start) & (full.index <= end)]
            if len(full) < min_bars: continue

            close_s = pd.to_numeric(full['close'], errors='coerce').replace(0, np.nan).dropna()
            if len(close_s) < min_bars: continue

            prices[code] = close_s
            # Amount column
            if 'amount' in full.columns:
                amount_s = pd.to_numeric(full['amount'], errors='coerce').replace(0, np.nan).dropna()
            else:
                amount_s = close_s.copy() * 1e8 / close_s  # placeholder
            amounts[code] = amount_s
        except Exception:
            continue

    price_panel = pd.DataFrame(prices).sort_index()
    amount_panel = pd.DataFrame(amounts).sort_index()

    # 可选：限制到CSI800（与原始回测对齐）
    try:
        from data.storage import load_meta
        c800 = load_meta('csi800')
        c800_codes = set(c800['code'].tolist()) if not c800.empty else set()
        if c800_codes:
            common = [c for c in price_panel.columns if c in c800_codes]
            price_panel = price_panel[common]
            amount_panel = amount_panel[common]
            logger.info(f"限制CSI800: {price_panel.shape[1]}只")
    except Exception:
        pass

    logger.info(f"价格面板: {price_panel.shape[0]}d × {price_panel.shape[1]}只")
    return price_panel, amount_panel


def get_top_n_by_turnover(amount_panel, date_idx, n=30):
    if date_idx < 20:
        return sorted(amount_panel.columns)[:n]
    avg_amt = amount_panel.iloc[max(0, date_idx-20):date_idx].mean().dropna()
    return avg_amt.nlargest(n).index.tolist()
