"""
补全 stock_info 元数据：is_st / industry_l1 / list_date。
一次性运行，结果存入 data_store/meta/stock_info_full.parquet。

运行：python scripts/init_stock_meta.py
耗时：约 2-3 分钟（31次行业 API + 本地文件扫描）
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import akshare as ak
import pandas as pd
from loguru import logger
from data.storage import load_meta, save_meta

logger.add("logs/init_stock_meta.log", rotation="1 day")


def build_industry_map() -> dict[str, str]:
    """返回 {code: industry_l1}，遍历申万31个一级行业。"""
    logger.info("获取申万一级行业成分股...")
    industries = ak.sw_index_first_info()
    code_to_ind = {}

    for _, row in industries.iterrows():
        ind_code = row["行业代码"].replace(".SI", "")
        ind_name = row["行业名称"]
        try:
            cons = ak.index_stock_cons(symbol=ind_code)
            for code in cons["品种代码"].tolist():
                code_to_ind[str(code).zfill(6)] = ind_name
        except Exception as e:
            logger.warning(f"行业 {ind_name}({ind_code}) 获取失败: {e}")

    logger.info(f"行业映射完成: {len(code_to_ind)} 只股票")
    return code_to_ind


def build_list_date_map() -> dict[str, str]:
    """从本地日线文件推断上市日期（最早有数据的日期）。"""
    logger.info("从本地日线推断上市日期...")
    daily_root = Path("data_store/daily")
    years = sorted([int(y.name) for y in daily_root.iterdir() if y.is_dir()])
    if not years:
        return {}

    earliest_year = years[0]
    # 先取最早年份所有股票列表
    first_year_dir = daily_root / str(earliest_year)
    codes_in_first = {f.stem for f in first_year_dir.glob("*.parquet")}

    code_to_date = {}
    for code in codes_in_first:
        code_to_date[code] = f"{earliest_year}-01-01"   # 早于数据起点，视为老股

    # 对更晚出现的股票，找最早年份
    for year in years[1:]:
        year_dir = daily_root / str(year)
        for f in year_dir.glob("*.parquet"):
            code = f.stem
            if code not in code_to_date:
                try:
                    df = pd.read_parquet(f, columns=["date"])
                    if not df.empty:
                        first_date = pd.to_datetime(df["date"]).min().strftime("%Y-%m-%d")
                        code_to_date[code] = first_date
                except Exception:
                    code_to_date[code] = f"{year}-01-01"

    logger.info(f"上市日期推断完成: {len(code_to_date)} 只")
    return code_to_date


def main():
    logger.info("开始补全 stock_info 元数据")

    # 从 Akshare 获取完整全A股列表（stock_info.parquet 存的是市场统计，不是个股列表）
    logger.info("获取全A股列表...")
    base = ak.stock_info_a_code_name()
    if base.empty:
        logger.error("全A股列表获取失败")
        return
    logger.info(f"全A股: {len(base)} 只")

    # 1. is_st（从名称判断，无需 API）
    base["is_st"] = base["name"].str.contains(r"ST|\*ST|退市", na=False, regex=True)
    logger.info(f"is_st: {base['is_st'].sum()} 只 ST/退市")

    # 2. industry_l1
    ind_map = build_industry_map()
    base["industry_l1"] = base["code"].map(ind_map)
    missing_ind = base["industry_l1"].isna().sum()
    logger.info(f"industry_l1: {base['industry_l1'].notna().sum()} 只已映射，{missing_ind} 只无行业数据")
    # 北交所等无法归类的填 "其他"
    base["industry_l1"] = base["industry_l1"].fillna("其他")

    # 3. list_date
    date_map = build_list_date_map()
    base["list_date"] = base["code"].map(date_map)
    base["list_date"] = pd.to_datetime(base["list_date"], errors="coerce")
    logger.info(f"list_date: {base['list_date'].notna().sum()} 只已填充")

    # 保存
    save_meta("stock_info_full", base)
    logger.info(f"已保存 → data_store/meta/stock_info_full.parquet")
    logger.info(f"字段: {base.columns.tolist()}")
    logger.info(base.head(5).to_string())


if __name__ == "__main__":
    main()
