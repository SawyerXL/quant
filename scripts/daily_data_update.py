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
import pandas as pd
from datetime import date, timedelta
from loguru import logger
from data.source import get_source
from data.storage import save_daily, save_meta, load_meta, load_daily
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


def _update_index_daily(target_date: str):
    """增量更新4个有代码冲突的指数日线（不影响个股数据）。"""
    import akshare as ak
    indices = [
        ("000001", "sh000001"),   # 上证指数
        ("000688", "sh000688"),   # 科创50
        ("000905", "sh000905"),   # 中证500
        ("000906", "sh000906"),   # 中证800
    ]
    for code, ak_symbol in indices:
        try:
            raw = ak.stock_zh_index_daily(symbol=ak_symbol)
            if raw.empty:
                continue
            raw = raw.rename(columns={
                "date": "date", "open": "open", "high": "high",
                "low": "low", "close": "close", "volume": "volume",
            })
            raw["date"] = pd.to_datetime(raw["date"])
            raw["code"] = code
            if "amount" not in raw.columns:
                raw["amount"] = 0.0
            if "pct_chg" not in raw.columns:
                raw["pct_chg"] = 0.0
            raw = raw.sort_values("date")

            # 只写目标日期（当天增量）
            day_rows = raw[raw["date"] == target_date]
            if day_rows.empty:
                continue

            yr = str(pd.Timestamp(target_date).year)
            out_path = Path(f"data_store/daily/{yr}/{code}.parquet")
            if out_path.exists():
                existing = pd.read_parquet(out_path)
                existing["date"] = pd.to_datetime(existing["date"])
                merged = pd.concat([existing, day_rows]).drop_duplicates(subset=["date"], keep="last")
                merged = merged.sort_values("date")
                merged.to_parquet(out_path, index=False)
            else:
                day_rows.to_parquet(out_path, index=False)
            logger.info(f"  指数{code}: {target_date} close={day_rows['close'].iloc[-1]:.2f}")
        except Exception as e:
            logger.debug(f"  指数{code}更新跳过: {e}")


def _sync_csi800_index_meta():
    """把 daily/000906(干净指数日线) 的新日期同步进 csi800_index meta。
    策略择时 get_position_ratio 读的是 meta, 而 meta 一直没人写 → MA200会冻结(曾停在7/7)。
    这里做增量追加, 保留完整历史。"""
    try:
        idx = load_daily("000906", "2005-01-01", date.today().strftime("%Y-%m-%d"))
        if idx.empty:
            return
        idx = idx.copy(); idx["date"] = pd.to_datetime(idx["date"])
        meta = load_meta("csi800_index")
        if meta.empty:
            save_meta("csi800_index", idx[["date", "close"]]); return
        meta["date"] = pd.to_datetime(meta["date"])
        add = idx[~idx["date"].isin(set(meta["date"]))]
        if add.empty:
            return
        rows = []
        for _, r in add.iterrows():
            row = {c: None for c in meta.columns}
            row["date"] = r["date"]; row["close"] = r["close"]
            rows.append(row)
        merged = pd.concat([meta, pd.DataFrame(rows)], ignore_index=True)
        merged["date"] = pd.to_datetime(merged["date"])
        merged = merged.drop_duplicates(subset=["date"], keep="last").sort_values("date")
        save_meta("csi800_index", merged)
        logger.info(f"  csi800_index meta 同步: +{len(add)}天 → 最新{merged['date'].max().date()}")
    except Exception as e:
        logger.warning(f"  csi800_index meta 同步失败: {e}")


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

    # 跳过与个股代码冲突的指数代码（由 rebuild_index_data.py 通过 stock_zh_index_daily 更新）
    INDEX_CODES = {"000001", "000688", "000905", "000906"}
    codes = [c for c in stock_info["code"].tolist() if c not in INDEX_CODES]
    failed = []
    dirty_rejected = 0
    for i, code in enumerate(codes):
        try:
            df = src.get_daily(code, today, today)
            if df.empty:
                failed.append(code)
            else:
                # ── 脏数据过滤：新收盘价vs最近有效收盘价，跳变>50%拒绝 ──
                if "close" in df.columns:
                    new_close = pd.to_numeric(df["close"], errors="coerce").iloc[-1]
                    if not pd.isna(new_close) and new_close > 0:
                        try:
                            old = load_daily(code, None, today)  # 全部历史
                            if not old.empty and "close" in old.columns:
                                old_close = pd.to_numeric(old["close"], errors="coerce").dropna()
                                if len(old_close) > 0:
                                    last_valid = old_close.iloc[-1]
                                    if last_valid > 0:
                                        jump = abs(new_close / last_valid - 1)
                                        if jump > 0.5:  # 跳变>50% = 脏数据
                                            logger.warning(f"{code}: 脏数据拒绝 (新¥{new_close:.2f} vs 旧¥{last_valid:.2f}, 跳变{jump:.0%})")
                                            dirty_rejected += 1
                                            continue
                        except Exception:
                            pass  # 首次入库不检查
                save_daily(code, df)
        except Exception as e:
            logger.debug(f"{code}: 更新失败 — {e}")
            failed.append(code)
        if (i + 1) % 500 == 0:
            logger.info(f"进度: {i+1}/{len(codes)}")
    if dirty_rejected:
        logger.warning(f"脏数据拒绝: {dirty_rejected}只")

    # 3.5. 更新指数日线（用stock_zh_index_daily, 避开个股代码冲突）
    _update_index_daily(today)

    # 3.6. 同步 csi800_index meta(策略MA200择时用的基准, 否则会冻结)
    _sync_csi800_index_meta()

    # 4. 更新交易日历（先于日报，确保日历及时保存）
    cal_df = pd.DataFrame({"trade_date": calendar})
    save_meta("trade_calendar", cal_df)

    # 5. 推送日报
    msg = f"数据更新完成: {today}, 成功 {len(codes)-len(failed)}/{len(codes)}, 失败 {len(failed)} 只"
    logger.info(msg)
    send_alert(msg)

    if len(failed) > 50:
        send_alert(f"警告：失败股票数量异常 ({len(failed)} 只)，请检查数据源", level="warning")


if __name__ == "__main__":
    update_today()
