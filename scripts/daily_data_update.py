"""
每日数据更新脚本。
Linux 服务器 cron 配置：
  0 17 * * 1-5 cd /root/quant && /root/quant/.venv/bin/python scripts/daily_data_update.py
（周一至周五 17:00，收盘后1.5小时确保数据稳定）

更新频率：
  每日   - 全市场日线增量、交易日历
  每周一 - stock_info_full（行业/ST/上市日期）、CSI 800/1000 成分股
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import akshare as ak
from datetime import date, timedelta
from loguru import logger
from data.source import get_source
from data.storage import save_daily, save_meta, load_meta
from data.cleaner import validate_data_completeness
from monitoring.alerts import send_alert

logger.add("logs/data_update_{time:YYYY-MM-DD}.log", rotation="1 day", retention="30 days")


def _update_stock_meta_full():
    """
    每周一更新 stock_info_full（行业分类/ST状态/上市日期）及 CSI 成分股。
    耗时约 2-3 分钟，不影响当日日线更新。
    """
    logger.info("── 周更：stock_info_full + CSI 成分股 ──")

    # 全A股列表（code + name）
    base = ak.stock_info_a_code_name()
    if base.empty:
        logger.warning("获取全A股列表失败，跳过周更")
        return

    # ST 状态（从名称判断）
    base["is_st"] = base["name"].str.contains(r"ST|\*ST|退市", na=False, regex=True)

    # 申万一级行业映射
    try:
        industries = ak.sw_index_first_info()
        code_to_ind = {}
        for _, row in industries.iterrows():
            ind_code = row["行业代码"].replace(".SI", "")
            ind_name = row["行业名称"]
            try:
                cons = ak.index_stock_cons(symbol=ind_code)
                for c in cons["品种代码"].tolist():
                    code_to_ind[str(c).zfill(6)] = ind_name
            except Exception:
                pass
        base["industry_l1"] = base["code"].map(code_to_ind).fillna("其他")
        logger.info(f"行业映射: {base['industry_l1'].notna().sum()} 只")
    except Exception as e:
        logger.warning(f"行业映射获取失败: {e}，保留上次数据")
        old = load_meta("stock_info_full")
        if not old.empty and "industry_l1" in old.columns:
            base = base.merge(old[["code", "industry_l1"]], on="code", how="left")
            base["industry_l1"] = base["industry_l1"].fillna("其他")
        else:
            base["industry_l1"] = "其他"

    # 上市日期（从本地日线推断，只补新股）
    old_full = load_meta("stock_info_full")
    if not old_full.empty and "list_date" in old_full.columns:
        base = base.merge(old_full[["code", "list_date"]], on="code", how="left")
    else:
        base["list_date"] = None

    import pandas as pd
    base["list_date"] = pd.to_datetime(base["list_date"], errors="coerce")
    save_meta("stock_info_full", base)
    logger.info(f"stock_info_full 更新完成: {len(base)} 只，ST={base['is_st'].sum()} 只")

    # CSI 800 / CSI 1000 成分股
    for symbol, name in [("000906", "csi800"), ("000852", "csi1000")]:
        try:
            import pandas as pd
            df = ak.index_stock_cons_weight_csindex(symbol=symbol)
            df = df.rename(columns={"成分券代码": "code", "成分券名称": "name",
                                     "权重": "weight", "日期": "date"})
            df = df[["code", "name", "weight", "date"]]
            save_meta(name, df)
            logger.info(f"{name} 更新: {len(df)} 只")
        except Exception as e:
            logger.warning(f"{name} 更新失败: {e}")


def update_today():
    today = date.today().strftime("%Y-%m-%d")
    src   = get_source()

    # 1. 确认是交易日
    calendar = src.get_trade_calendar()
    if today not in calendar:
        logger.info(f"{today} 非交易日，跳过")
        return

    logger.info(f"开始更新 {today} 数据")

    # 2. 每周一：更新 stock_info_full + CSI 成分股
    if date.today().weekday() == 0:
        _update_stock_meta_full()

    # 3. 更新全市场日线（增量）
    # 优先用 stock_info_full（含行业），降级用 stock_info（仅代码+名称）
    stock_info = load_meta("stock_info_full")
    if stock_info.empty:
        stock_info = load_meta("stock_info")
    if stock_info.empty:
        logger.error("stock_info 为空，请先运行 scripts/init_stock_meta.py")
        send_alert("数据更新失败：stock_info 为空，请检查", level="error")
        return

    codes = stock_info["code"].tolist()
    failed = []
    for i, code in enumerate(codes):
        df = src.get_daily(code, today, today)
        if df.empty:
            failed.append(code)
        else:
            save_daily(code, df)
        if (i + 1) % 500 == 0:
            logger.info(f"进度: {i+1}/{len(codes)}")

    # 4. 更新交易日历
    cal_df = __import__("pandas").DataFrame({"trade_date": calendar})
    save_meta("trade_calendar", cal_df)

    # 5. 推送日报
    msg = f"数据更新完成: {today}, 成功 {len(codes)-len(failed)}/{len(codes)}, 失败 {len(failed)} 只"
    logger.info(msg)
    send_alert(msg)

    if len(failed) > 50:
        send_alert(f"警告：失败股票数量异常 ({len(failed)} 只)，请检查数据源", level="warning")


if __name__ == "__main__":
    update_today()
