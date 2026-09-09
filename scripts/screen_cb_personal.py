#!/usr/bin/env python3
"""
个人账户可转债筛选 — 回测验证版三层漏斗
回测结论(validate_cb_filters.py): 价格<110是核心alpha(+1.2pp), 评级≥AA-防御(+0.8pp/-0.9pp回撤)
数据源: bond_zh_cov(全量,含溢价率) + bond_zh_hs_cov_spot(流动性) + bond_cb_jsl(富数据)
三层: 安全边际 → 排雷 → 加分排序
输出: 终端报告 + CSV

用法:
  python scripts/screen_cb_personal.py              # 终端报告
  python scripts/screen_cb_personal.py --save       # 同时保存CSV
  python scripts/screen_cb_personal.py --top 15     # 自定义输出数量
  python scripts/screen_cb_personal.py --verbose    # 打印过滤掉的标的(调试用)
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from loguru import logger

# ══════════════════════════════════════════════════════════════════
# 配置参数（熊市版）
# ══════════════════════════════════════════════════════════════════
MAX_PRICE = 110          # 转债价格上限，接近债底
MIN_RATING_ORDER = 3     # AA-(含)以上: AAA=0, AA+=1, AA=2, AA-=3
MIN_VOLUME_WAN = 100     # 日成交额下限(万),仅作参考不硬过滤(历史回测成交量数据不完整)
MIN_YTM = 0.0            # 到期收益率下限, 持有至少不亏
MIN_REMAIN_YEARS = 1.0   # 剩余期限下限
MAX_PREMIUM_FILTER = 100  # 溢价率上限(%),超出则股性参与度≈0,仅剩纯债价值
MIN_LIST_MONTHS = 3       # 最短上市月数,排除新上市面值债(数据不可靠)
HIGH_PRICE_THRESHOLD = 130  # 高于此价+低溢价=临近强赎

RATING_MAP = {
    'AAA': 0, 'AA+': 1, 'AA': 2, 'AA-': 3,
    'A+': 4, 'A': 5, 'A-': 6, 'BBB+': 7, 'BBB': 8, 'BBB-': 9,
    'BB': 10, 'B': 11, 'CCC': 12, 'CC': 13, 'C': 14,
}


def parse_rating(r):
    """解析评级字符串,返回数值(越小越好),解析失败返回99."""
    if pd.isna(r) or not r:
        return 99
    s = str(r).strip().upper()
    return RATING_MAP.get(s, 99)


def parse_remain_years(r):
    """解析剩余年限,兼容NaN/字符串/None."""
    if pd.isna(r) or r == '' or r is None:
        return np.nan
    try:
        return float(r)
    except (ValueError, TypeError):
        return np.nan


# ══════════════════════════════════════════════════════════════════
# Phase 1: 拉取数据
# ══════════════════════════════════════════════════════════════════

def fetch_universe():
    """拉取全量转债列表(bond_zh_cov),只保留已上市的."""
    import akshare as ak
    logger.info("拉取全量转债列表...")
    raw = ak.bond_zh_cov()
    logger.info(f"  原始: {len(raw)}只")

    # 过滤未上市: 上市时间非空且<=今天
    today = datetime.now().strftime('%Y-%m-%d')
    raw['上市时间'] = pd.to_datetime(raw['上市时间'], errors='coerce')
    listed = raw[raw['上市时间'].notna() & (raw['上市时间'] <= today)].copy()
    logger.info(f"  已上市: {len(listed)}只")

    return listed


def fetch_spot():
    """拉取活跃转债实时行情(含成交额)."""
    import akshare as ak
    logger.info("拉取活跃转债行情...")
    try:
        spot = ak.bond_zh_hs_cov_spot()
        if spot.empty:
            logger.warning("  行情为空")
            return pd.DataFrame()
        logger.info(f"  {len(spot)}只活跃交易")
        return spot
    except Exception as e:
        logger.warning(f"  行情拉取失败: {e}")
        return pd.DataFrame()


def fetch_jsl_enrichment():
    """尝试拉取集思录富数据(含PB/YTM/剩余年限),失败则返回空."""
    import akshare as ak
    logger.info("尝试拉取集思录富数据...")
    try:
        jsl = ak.bond_cb_jsl()
        if jsl.empty:
            return pd.DataFrame()
        logger.info(f"  {len(jsl)}条(首页)")
        return jsl
    except Exception as e:
        logger.warning(f"  集思录拉取失败: {e}")
        return pd.DataFrame()


# ══════════════════════════════════════════════════════════════════
# Phase 2: 三层漏斗
# ══════════════════════════════════════════════════════════════════

def layer1_safety(df, verbose=False):
    """第一层: 安全边际筛选 — 价格/评级/溢价率."""
    before = len(df)

    # 价格 < MAX_PRICE
    mask_price = df['price'] < MAX_PRICE
    if verbose:
        excluded = df[~mask_price]
        for _, r in excluded.head(10).iterrows():
            logger.debug(f"  [L1-价格] {r.get('name','?')} {r['code']}: ¥{r['price']:.1f} ≥ {MAX_PRICE}")
    df = df[mask_price].copy()
    after_price = len(df)

    # 上市≥MIN_LIST_MONTHS个月(避免新上市面值债,数据不可靠)
    if 'list_date' in df.columns:
        cutoff = datetime.now() - timedelta(days=MIN_LIST_MONTHS * 30)
        mask_listed = df['list_date'].notna() & (df['list_date'] <= cutoff)
        if verbose:
            for _, r in df[~mask_listed].head(5).iterrows():
                logger.debug(f"  [L1-上市] {r.get('name','?')} {r['code']}: {r.get('list_date','?')}")
        df = df[mask_listed].copy()

    # 评级 ≥ AA-
    df['rating_num'] = df['rating'].apply(parse_rating)
    mask_rating = df['rating_num'] <= MIN_RATING_ORDER
    if verbose:
        excluded = df[~mask_rating]
        for _, r in excluded.iterrows():
            logger.debug(f"  [L1-评级] {r.get('name','?')} {r['code']}: {r['rating']}")
    df = df[mask_rating].copy()
    after_rating = len(df)

    # 排除溢价率缺失(可能是面值占位符,非真实市价)
    mask_nan = df['premium'].isna()
    if mask_nan.any() and verbose:
        for _, r in df[mask_nan].head(5).iterrows():
            logger.debug(f"  [L1-无溢价] {r.get('name','?')} {r['code']}: ¥{r['price']:.1f} premium=NaN")
    df = df[~mask_nan].copy()

    # 排除极高溢价(>100%): 股性参与度≈0,只剩纯债价值
    mask_dead = df['premium'] > MAX_PREMIUM_FILTER
    if mask_dead.any() and verbose:
        for _, r in df[mask_dead].iterrows():
            logger.debug(f"  [L1-死债] {r.get('name','?')} {r['code']}: ¥{r['price']:.1f} +{r['premium']:.1f}%")
    df = df[~mask_dead].copy()

    logger.info(f"  第一层(安全): {before}→{after_price}(价)→{after_rating}(级)→{len(df)}(去劣)")
    return df


def layer2_exclude(df, verbose=False, min_volume=MIN_VOLUME_WAN):
    """第二层: 排雷 — ST正股/低价正股/临近强赎/流动性不足."""
    before = len(df)

    # 排雷需要 enrich 阶段添加的字段: is_st, stock_price, is_redeem, amount_wan
    if 'is_st' in df.columns:
        mask_st = ~df['is_st']
        if verbose:
            for _, r in df[df['is_st']].iterrows():
                logger.debug(f"  [L2-ST] {r.get('name','?')} {r['code']}: 正股{r.get('stock_name','?')}ST")
        df = df[mask_st].copy()

    # 正股价<2元(面值退市风险)
    if 'stock_price' in df.columns:
        mask_sp = df['stock_price'] >= 2.0
        if verbose:
            for _, r in df[~mask_sp].iterrows():
                logger.debug(f"  [L2-低价正股] {r.get('name','?')} {r['code']}: 正股¥{r['stock_price']:.2f}")
        df = df[mask_sp].copy()

    # 临近强赎: 价格>130且溢价率<5%
    if 'is_redeem' in df.columns:
        mask_redeem = ~df['is_redeem']
        if verbose:
            for _, r in df[df['is_redeem']].iterrows():
                logger.debug(f"  [L2-强赎] {r.get('name','?')} {r['code']}: ¥{r['price']:.1f} +{r['premium']:.1f}%")
        df = df[mask_redeem].copy()

    # 流动性标记(不硬过滤 — 历史回测成交量数据稀疏,硬过滤误杀严重)
    if 'amount_wan' in df.columns:
        low_liq = df['amount_wan'] < min_volume
        if low_liq.any():
            df.loc[low_liq, 'liq_warn'] = True
            if verbose:
                for _, r in df[low_liq].iterrows():
                    logger.debug(f"  [L2-流动性] {r.get('name','?')} {r['code']}: 成交额{r['amount_wan']:.0f}万<{min_volume}万")
        else:
            df['liq_warn'] = False
    else:
        df['liq_warn'] = False

    logger.info(f"  第二层(排雷): {before}→{len(df)}只")
    return df


def enrich_stock_pb(df, verbose=False):
    """补拉正股PB — 通过财报的每股净资产计算,只对已过滤的少量候选调用."""
    import akshare as ak

    if 'stock_pb' not in df.columns:
        df['stock_pb'] = np.nan

    # 只补拉PB缺失的行
    need_pb = df[df['stock_pb'].isna() & df['stock_code'].notna()]
    if need_pb.empty:
        return df

    logger.info(f"补拉正股PB({len(need_pb)}只)...")
    pb_map = {}
    for _, r in need_pb.iterrows():
        code = r['stock_code']
        try:
            fa = ak.stock_financial_abstract(symbol=code)
            # 找最新季度的每股净资产
            bv_row = fa[(fa['选项'] == '常用指标') & (fa['指标'] == '每股净资产')]
            if bv_row.empty:
                continue
            # 取最新非空列(列顺序从新到旧)
            val_cols = [c for c in fa.columns if c not in ('选项', '指标') and c != 'Unnamed: 0']
            bv = None
            for col in val_cols:
                v = bv_row.iloc[0].get(col)
                if pd.notna(v) and v != '':
                    try:
                        bv = float(v)
                        if bv > 0:
                            break
                    except (ValueError, TypeError):
                        continue
            if bv and bv > 0:
                sp = r.get('stock_price', 0)
                if sp and sp > 0:
                    pb_map[code] = round(sp / bv, 2)
                    if verbose:
                        logger.debug(f"  PB {r['stock_name']}({code}): ¥{sp:.2f}/¥{bv:.2f}={pb_map[code]:.2f}")
        except Exception as e:
            if verbose:
                logger.debug(f"  PB {r.get('stock_name', code)} 失败: {e}")
            continue

    df['stock_pb'] = df.apply(
        lambda row: pb_map.get(row['stock_code'], row.get('stock_pb', np.nan)), axis=1
    )
    logger.info(f"  补全{len(pb_map)}只PB")
    return df


def layer3_score(df):
    """第三层: 加分项排序 — 低价优先(回测已验证双低=低价)."""

    # 加分项(每项0-1分)
    # 1. 正股PB>1 (有下修空间,不受净资产限制)
    if 'stock_pb' in df.columns and df['stock_pb'].notna().any():
        df['bonus_pb'] = (df['stock_pb'].fillna(0) > 1.0).astype(int)
    else:
        df['bonus_pb'] = 0

    # 2. YTM > 0 (持有到期不亏)
    if 'ytm' in df.columns and df['ytm'].notna().any():
        df['bonus_ytm'] = (df['ytm'].fillna(-999) > 0).astype(int)
    else:
        df['bonus_ytm'] = 0

    # 3. 剩余年限 > 2年 (给下修留时间)
    if 'remain_years' in df.columns and df['remain_years'].notna().any():
        df['bonus_tenor'] = (df['remain_years'].fillna(0) > 2.0).astype(int)
    else:
        df['bonus_tenor'] = 0

    df['bonus_total'] = df['bonus_pb'] + df['bonus_ytm'] + df['bonus_tenor']

    # 主排序: 价格从低到高(回测结论: 双低=低价,溢价率冗余)
    # 同价格按加分项降序
    df = df.sort_values(['price', 'bonus_total'], ascending=[True, False]).reset_index(drop=True)

    return df


# ══════════════════════════════════════════════════════════════════
# Phase 3: 数据整合
# ══════════════════════════════════════════════════════════════════

def enrich_with_spot(df, spot):
    """合并活跃行情数据: 成交额."""
    if spot.empty:
        df['amount_wan'] = np.nan
        return df

    # spot code列是字符串(如'113708'), universe code也是字符串
    spot['code_str'] = spot['code'].astype(str).str.zfill(6)

    # amount单位是元,转为万元
    spot['amount_wan'] = pd.to_numeric(spot['amount'], errors='coerce') / 10000

    # 合并
    merged = df.merge(
        spot[['code_str', 'amount_wan', 'volume']],
        left_on='code', right_on='code_str', how='left'
    )
    merged.drop(columns=['code_str'], inplace=True, errors='ignore')
    return merged


def enrich_with_jsl(df, jsl):
    """合并集思录富数据: PB/YTM/剩余年限."""
    if jsl.empty:
        if 'stock_pb' not in df.columns:
            df['stock_pb'] = np.nan
        if 'ytm' not in df.columns:
            df['ytm'] = np.nan
        if 'remain_years' not in df.columns:
            df['remain_years'] = np.nan
        return df

    jsl['code_str'] = jsl['代码'].astype(str).str.zfill(6)

    jsl_mapped = pd.DataFrame({
        'code_str': jsl['code_str'],
        'stock_pb_jsl': pd.to_numeric(jsl['正股PB'], errors='coerce'),
        'ytm_jsl': pd.to_numeric(jsl['到期税前收益'], errors='coerce'),
        'remain_years_jsl': jsl['剩余年限'].apply(parse_remain_years),
    })

    merged = df.merge(jsl_mapped, left_on='code', right_on='code_str', how='left')
    merged.drop(columns=['code_str'], inplace=True, errors='ignore')

    # JSL数据补充(只填充原本缺失的)
    for col, jsl_col in [('stock_pb', 'stock_pb_jsl'), ('ytm', 'ytm_jsl'), ('remain_years', 'remain_years_jsl')]:
        if jsl_col in merged.columns:
            if col not in merged.columns:
                merged[col] = merged[jsl_col]
            else:
                merged[col] = merged[col].fillna(merged[jsl_col])
            merged.drop(columns=[jsl_col], inplace=True)

    return merged


def tag_redemption_flags(df):
    """标记临近强赎/退市风险."""
    # 强赎临近: 价>130且溢价<5%
    df['is_redeem'] = (df['price'] > HIGH_PRICE_THRESHOLD) & (df['premium'] < 5)
    return df


def tag_st_stocks(df):
    """标记ST正股 — 扫my_holdings和本地数据,无法穷举所有ST,仅做已知ST标记."""
    # 从本地持仓/已知ST列表标记
    # bond_zh_cov的正股简称中包含ST标记(如'STXX'/'*STXX')
    if 'stock_name' in df.columns:
        df['is_st'] = df['stock_name'].astype(str).str.contains(r'^\*?ST', case=False, na=False)
    else:
        df['is_st'] = False
    return df


# ══════════════════════════════════════════════════════════════════
# Phase 4: 报告输出
# ══════════════════════════════════════════════════════════════════

def print_report(df, top_n, min_volume=MIN_VOLUME_WAN):
    """终端报告."""
    print()
    print("═" * 90)
    print(f"  个人账户可转债筛选 — 熊市版三层漏斗  ({datetime.now().strftime('%Y-%m-%d %H:%M')})")
    print("═" * 90)

    if df.empty:
        print("\n  ⚠ 没有符合条件的结果。当前市场可能不适合CB建仓。")
        return

    print(f"\n  符合条件的标的: {len(df)}只, 展示前{min(top_n, len(df))}只")
    print()
    print(f"  {'排名':<5} {'代码':<8} {'转债名称':<10} {'价格':>7} {'溢价':>7} "
          f"{'评级':<5} {'成交额(万)':>9} {'YTM%':>7} {'PB':>6} {'剩余年':>7} "
          f"{'正股':<8} {'加分':>4}")
    print(f"  {'-'*90}")

    for i, (_, r) in enumerate(df.head(top_n).iterrows()):
        if i >= top_n:
            break
        rank = i + 1
        code = r['code']
        name = str(r.get('name', ''))[:10]
        price = f"{r['price']:.2f}"
        premium = f"{r.get('premium', 0):.1f}%"
        rating = str(r.get('rating', '?'))[:5]
        amt = f"{r.get('amount_wan', 0):.0f}" if pd.notna(r.get('amount_wan')) else '?'
        ytm = f"{r.get('ytm', np.nan):.1f}" if pd.notna(r.get('ytm')) else '?'
        pb = f"{r.get('stock_pb', np.nan):.2f}" if pd.notna(r.get('stock_pb')) else '?'
        remain = f"{r.get('remain_years', np.nan):.1f}" if pd.notna(r.get('remain_years')) else '?'
        sname = str(r.get('stock_name', ''))[:8]
        bonus = int(r.get('bonus_total', 0))

        # 风险标记
        flags = ''
        if r.get('is_redeem'):
            flags += '⚠强赎 '
        if r.get('is_st'):
            flags += '🛑ST '

        print(f"  {rank:<5} {code:<8} {name:<10} {price:>7} {premium:>7} "
              f"{rating:<5} {amt:>9} {ytm:>7} {pb:>6} {remain:>7} "
              f"{sname:<8} {bonus:>4} {flags}")

    print(f"  {'-'*90}")
    print(f"  筛选条件: 价格<{MAX_PRICE} | 评级≥AA- | 无ST/强赎")
    print(f"  回测验证: 价格<110 +1.2pp超额(夏普1.03) | +评级≥AA- +0.8pp/回撤-0.9pp")
    print(f"  排序逻辑: 低价优先(双低=低价,溢价率分量冗余)")
    print(f"  成交量仅作参考(历史数据稀疏,回测无法验证,不做硬过滤)")

    # 加分项说明
    n_pb = int(df['bonus_pb'].sum()) if 'bonus_pb' in df.columns else 0
    n_ytm = int(df['bonus_ytm'].sum()) if 'bonus_ytm' in df.columns else 0
    n_tenor = int(df['bonus_tenor'].sum()) if 'bonus_tenor' in df.columns else 0
    print(f"  加分统计: 正股PB>1: {n_pb}只 | YTM>0: {n_ytm}只 | 剩余>2年: {n_tenor}只")
    print()

    # 统计概要
    if len(df) >= 5:
        prices = df['price']
        print(f"  统计: 价格 {prices.min():.1f}~{prices.max():.1f}"
              f" 中位数{prices.median():.1f} 均值{prices.mean():.1f}")
        if 'premium' in df.columns:
            prems = df['premium'].dropna()
            if len(prems) > 0:
                print(f"        溢价率 {prems.min():.1f}%~{prems.max():.1f}%"
                      f" 中位数{prems.median():.1f}%")
        if 'amount_wan' in df.columns and df['amount_wan'].notna().any():
            amts = df['amount_wan'].dropna()
            if len(amts) > 0:
                print(f"        成交额 {amts.min():.0f}~{amts.max():.0f}万"
                      f" 中位数{amts.median():.0f}万")
    print("═" * 90)


def save_csv(df, path=None):
    """保存筛选结果到CSV."""
    if path is None:
        path = Path(__file__).parent.parent / 'config' / 'cb_candidates.csv'

    cols = ['code', 'name', 'price', 'premium', 'rating', 'amount_wan',
            'ytm', 'stock_pb', 'remain_years', 'stock_code', 'stock_name',
            'bonus_total', 'bonus_pb', 'bonus_ytm', 'bonus_tenor']

    available = [c for c in cols if c in df.columns]
    df[available].to_csv(path, index=False, encoding='utf-8-sig')
    logger.info(f"结果已保存: {path}")


# ══════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════

def screen(top_n=20, save=False, verbose=False, min_volume=MIN_VOLUME_WAN):
    """主流程: 拉取→清洗→三层筛选→输出."""

    # --- 拉取 ---
    universe = fetch_universe()

    # 标准化列名
    universe = universe.rename(columns={
        '债券代码': 'code', '债券简称': 'name',
        '正股代码': 'stock_code', '正股简称': 'stock_name',
        '债现价': 'price', '转股溢价率': 'premium',
        '正股价': 'stock_price', '转股价': 'convert_price',
        '转股价值': 'convert_value', '信用评级': 'rating',
        '发行规模': 'issue_size', '上市时间': 'list_date',
    })
    universe['code'] = universe['code'].astype(str).str.zfill(6)
    universe['stock_code'] = universe['stock_code'].astype(str).str.zfill(6)
    universe['price'] = pd.to_numeric(universe['price'], errors='coerce')
    universe['premium'] = pd.to_numeric(universe['premium'], errors='coerce')
    universe['stock_price'] = pd.to_numeric(universe['stock_price'], errors='coerce')

    # 丢弃价格为空的行, 去重(code重复保留第一条)
    universe = universe.dropna(subset=['price']).copy()
    universe = universe.drop_duplicates(subset=['code'], keep='first').copy()
    logger.info(f"标准化后: {len(universe)}只(去重)")

    # --- 预标记 ---
    universe = tag_st_stocks(universe)
    universe = tag_redemption_flags(universe)

    # --- 拉取辅助数据 ---
    spot = fetch_spot()
    jsl = fetch_jsl_enrichment()

    # --- 数据合并 ---
    df = enrich_with_spot(universe, spot)
    df = enrich_with_jsl(df, jsl)

    # --- 三层漏斗 ---
    df = layer1_safety(df, verbose=verbose)
    df = layer2_exclude(df, verbose=verbose, min_volume=min_volume)
    df = layer3_score(df)

    # 只对Top N补PB(避免538只全量API调用)
    df_top = df.head(top_n).copy()
    df_top = enrich_stock_pb(df_top, verbose=verbose)
    # 合并回全量(仅Top N有PB,剩余用于统计)
    for c in ['stock_pb', 'bonus_pb']:
        if c in df_top.columns and c not in df.columns:
            df[c] = np.nan
    df.iloc[:len(df_top)] = df_top.values

    # --- 输出 ---
    print_report(df, top_n, min_volume=min_volume)

    if save:
        save_csv(df)

    return df


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser(description='个人账户可转债筛选')
    p.add_argument('--top', type=int, default=20, help='输出前N只(默认20)')
    p.add_argument('--save', action='store_true', help='保存CSV到config/cb_candidates.csv')
    p.add_argument('--verbose', '-v', action='store_true', help='打印被过滤标的(调试)')
    args = p.parse_args()

    logger.remove()
    logger.add(sys.stderr, level="DEBUG" if args.verbose else "INFO",
               format="<level>{message}</level>")

    screen(top_n=args.top, save=args.save, verbose=args.verbose)
