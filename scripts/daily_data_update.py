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


# 这4个代码既是指数也是个股(000001=上证指数/平安银行)。用个股源拉会得到个股价格
# 覆盖掉指数点位 —— 2026-08-25 手工补数时踩过一次，上证指数变成了11.59元。
# 任何遍历全市场的循环都必须排除它们，指数走 _update_index_daily 的独立通道。
INDEX_CODES = {"000001", "000688", "000905", "000906"}
INDEX_SYMBOLS = [
    ("000001", "sh000001"),   # 上证指数
    ("000688", "sh000688"),   # 科创50
    ("000905", "sh000905"),   # 中证500
    ("000906", "sh000906"),   # 中证800
]
GAP_LOOKBACK = 30       # 自动补洞只看最近30个交易日，更早的用 backfill_daily_data.py
GAP_MAX_REPAIR = 400    # 单次修复上限，源故障时不空转
GAP_SKIP_PATH = Path("data_store/meta/gap_skip.json")
GAP_SKIP_MAX = 3        # 同一(代码,日期)拉3次都没有 → 判定停牌/未上市，不再重试


def _update_index_daily(target_date: str, calendar=None):
    """增量更新4个有代码冲突的指数日线（不影响个股数据）。

    calendar 传入时顺带补最近GAP_LOOKBACK个交易日的空洞 —— 指数缺bar会直接坏掉
    MA200择时和regime判断，比个股缺bar更致命。
    """
    import akshare as ak
    want = None
    if calendar:
        recent = [d for d in calendar if d <= target_date][-GAP_LOOKBACK:]
        want = set(recent)
    for code, ak_symbol in INDEX_SYMBOLS:
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

            yr = str(pd.Timestamp(target_date).year)
            out_path = Path(f"data_store/daily/{yr}/{code}.parquet")

            if want is None:
                day_rows = raw[raw["date"] == target_date]
            else:
                have = set()
                if out_path.exists():
                    old = pd.read_parquet(out_path, columns=["date"])
                    have = set(pd.to_datetime(old["date"]).astype(str).str[:10])
                need = {d for d in want if d not in have}
                day_rows = raw[raw["date"].astype(str).str[:10].isin(need)]
            if day_rows.empty:
                continue

            if out_path.exists():
                existing = pd.read_parquet(out_path)
                existing["date"] = pd.to_datetime(existing["date"])
                merged = pd.concat([existing, day_rows]).drop_duplicates(subset=["date"], keep="last")
                merged = merged.sort_values("date")
                merged.to_parquet(out_path, index=False)
            else:
                day_rows.to_parquet(out_path, index=False)
            gap_note = f" (含补洞{len(day_rows)-1}天)" if len(day_rows) > 1 else ""
            logger.info(f"  指数{code}: {str(day_rows['date'].max())[:10]} "
                        f"close={day_rows['close'].iloc[-1]:.2f}{gap_note}")
        except Exception as e:
            logger.debug(f"  指数{code}更新跳过: {e}")


# 本地日线库原先只有个股 + 4个指数，ETF 全缺 —— 588000科创50ETF 是最大持仓却没日线，
# MA10/止损监控对它一直是瞎的。东财已封本机IP，走新浪 fund_etf_hist_sina。
ETF_BASE = ["588000", "510300", "510500", "159915"]


def _etf_sina_symbol(code: str):
    """ETF代码 → 新浪symbol。转债(11/12)、发债(7x)、老三板(4x)都不会命中。"""
    if code.startswith(("50", "51", "52", "56", "58")):
        return "sh" + code
    if code.startswith("15"):
        return "sz" + code
    return None


def _etf_codes():
    """基础ETF + 持仓里出现的ETF —— 买了新ETF不用改代码。"""
    codes = set(ETF_BASE)
    try:
        import csv
        with open("config/my_holdings.csv") as f:
            for row in csv.DictReader(f):
                c = str(row.get("code", "")).strip().zfill(6)
                if _etf_sina_symbol(c):
                    codes.add(c)
    except Exception as e:
        logger.debug(f"  读持仓ETF失败: {e}")
    return sorted(codes)


def _update_etf_daily(today: str):
    """新浪一次返回全history，直接交给 save_daily 按date去重 —— 天然自愈，缺多少补多少。"""
    import akshare as ak
    codes = _etf_codes()
    ok = 0
    for code in codes:
        try:
            raw = ak.fund_etf_hist_sina(symbol=_etf_sina_symbol(code))
            if raw is None or raw.empty:
                logger.warning(f"  ETF{code}: 返回空")
                continue
            raw = raw.copy()
            raw["date"] = pd.to_datetime(raw["date"])
            raw["code"] = code
            for col in ("amount", "pct_chg"):
                if col not in raw.columns:
                    raw[col] = 0.0
            before = len(load_daily(code, "2005-01-01", today))
            save_daily(code, raw)
            after = len(load_daily(code, "2005-01-01", today))
            logger.info(f"  ETF{code}: 新增{after-before}天 → 共{after}根, 最新{raw['date'].max().date()}")
            ok += 1
        except Exception as e:
            logger.warning(f"  ETF{code} 更新失败: {e}")
    logger.info(f"ETF日线更新: {ok}/{len(codes)} 成功")


def _fill_recent_gaps(calendar, today: str, src):
    """补最近GAP_LOOKBACK个交易日的空洞。

    update_today 只抓当天，任何一次源故障/宕机都在历史里留下永久空洞，而且没人会发现。
    2026-08-25 实测：全市场缺8/11，洛钼还缺8/18~8/21五天 —— 补洞后洛钼"连破MA10"
    从6天变成1天，整张MA10触发清单是错的。缺口不报错，只会让指标悄悄算错。
    """
    import json
    recent = [d for d in calendar if d <= today][-GAP_LOOKBACK:]
    if len(recent) < 2:
        return
    want, lo = set(recent), recent[0]

    skip = {}
    if GAP_SKIP_PATH.exists():
        try:
            skip = json.loads(GAP_SKIP_PATH.read_text())
        except Exception:
            skip = {}

    info = load_meta("stock_info_full")
    if info.empty:
        info = load_meta("stock_info")
    if info.empty:
        return
    codes = [c for c in info["code"].tolist() if c not in INDEX_CODES]

    holes = []
    for code in codes:
        try:
            d = load_daily(code, lo, today)
        except Exception:
            continue
        have = set(pd.to_datetime(d["date"]).astype(str).str[:10]) if not d.empty else set()
        miss = sorted(x for x in want - have if skip.get(f"{code}:{x}", 0) < GAP_SKIP_MAX)
        if miss:
            holes.append((code, miss))
    if not holes:
        logger.info(f"空洞检查: 最近{len(recent)}个交易日无缺口")
        return

    logger.warning(f"空洞检查: {len(holes)}只有缺口，修复前{min(len(holes), GAP_MAX_REPAIR)}只")
    fixed = 0
    for code, miss in holes[:GAP_MAX_REPAIR]:
        try:
            df = src.get_daily(code, miss[0], miss[-1])
            if df is not None and not df.empty:
                save_daily(code, df)
                after = load_daily(code, lo, today)
                have = set(pd.to_datetime(after["date"]).astype(str).str[:10])
                still = [x for x in miss if x not in have]
                fixed += len(miss) - len(still)
            else:
                still = miss
        except Exception as e:
            logger.debug(f"  {code} 补洞失败: {e}")
            still = miss
        # 拉不到的记次数：停牌/未上市会永远拉不到，重试GAP_SKIP_MAX次后放弃
        for x in still:
            skip[f"{code}:{x}"] = skip.get(f"{code}:{x}", 0) + 1

    try:
        GAP_SKIP_PATH.parent.mkdir(parents=True, exist_ok=True)
        GAP_SKIP_PATH.write_text(json.dumps(skip))
    except Exception as e:
        logger.debug(f"  gap_skip写入失败: {e}")
    logger.info(f"空洞修复: 补回{fixed}个bar，跳过表{len(skip)}条")


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

    # 指数代码走独立通道，见模块级 INDEX_CODES 注释
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
                            # 不能传 None：pd.Timestamp(None)=NaT → range(nan) 抛TypeError，
                            # 被下面的 except 吞掉 → 这道脏数据防线一直是死的（2026-08-25 修）
                            old = load_daily(code, "2005-01-01", today)  # 全部历史
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

    # 3.5. 更新指数日线（用stock_zh_index_daily, 避开个股代码冲突）+ 顺带补指数空洞
    _update_index_daily(today, calendar)

    # 3.55. ETF日线（stock_info里没有ETF, 上面的全市场循环覆盖不到）
    _update_etf_daily(today)

    # 3.6. 自动补洞: 只抓当天的更新会在历史里留永久空洞, 缺口不报错只让指标算错
    _fill_recent_gaps(calendar, today, src)

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
