import akshare as ak
import pandas as pd
from loguru import logger
from .base import DataSource


class AkshareSource(DataSource):
    """Akshare 数据源实现。免费，覆盖广，作为 MCP 到位前的主数据源。"""

    # ------------------------------------------------------------------
    # 日线行情
    # ------------------------------------------------------------------
    _COLS = ["date", "code", "open", "high", "low", "close", "volume", "amount", "pct_chg"]

    def get_daily(self, code: str, start: str, end: str) -> pd.DataFrame:
        """
        code 传 '000001' 格式（不带市场后缀）。全程前复权(qfq)，与历史库一致。

        东财(stock_zh_a_hist)主用；若东财被封/超时返回空，自动落新浪(stock_zh_a_daily)。
        两端点同为 qfq，输出列契约一致，调用方无感知。
        """
        # 东财2026-07-01起封本服IP, 主源切新浪, 东财仅兜底
        df = self._daily_sina(code, start, end)
        if df.empty:
            df = self._daily_em(code, start, end)
        return df

    def _daily_em(self, code: str, start: str, end: str) -> pd.DataFrame:
        try:
            df = ak.stock_zh_a_hist(
                symbol=code, period="daily",
                start_date=start.replace("-", ""), end_date=end.replace("-", ""),
                adjust="qfq",
            )
            df = df.rename(columns={
                "日期": "date", "开盘": "open", "最高": "high",
                "最低": "low",  "收盘": "close", "成交量": "volume",
                "成交额": "amount", "涨跌幅": "pct_chg",
            })
            df["date"] = df["date"].astype(str)
            df["code"] = code
            # 2026-09-02 单位归一: 东财成交量单位=手, 全库统一存"股"
            # (新浪=股)。此前两源交错写导致volume列100倍混用污染所有量比类指标
            df["volume"] = pd.to_numeric(df["volume"], errors="coerce") * 100
            return df[self._COLS].sort_values("date")
        except Exception as e:
            logger.warning(f"get_daily(东财) {code} failed: {e} -> 转新浪")
            return pd.DataFrame()

    def _daily_sina(self, code: str, start: str, end: str) -> pd.DataFrame:
        # 新浪要带市场前缀；沪6/京489/其余深
        prefix = "sh" if code.startswith("6") else ("bj" if code[:1] in "489" else "sz")
        try:
            # 2026-09-02 修复: 多拉10个日历日算pct_chg再裁剪——单行取数时
            # pct_change首行必NaN, 增量更新路径让每个新bar的pct_chg都是NaN
            # (8.1%的2026 bars), 敞口归因/脏跳扫描全部静默失真
            import datetime as _dt
            ext_start = (_dt.datetime.strptime(start, "%Y-%m-%d")
                         - _dt.timedelta(days=10)).strftime("%Y%m%d")
            df = ak.stock_zh_a_daily(
                symbol=f"{prefix}{code}",
                start_date=ext_start, end_date=end.replace("-", ""),
                adjust="qfq",
            )
            df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
            df["code"] = code
            # 新浪不返回涨跌幅，用收盘价自算以保持契约
            df["pct_chg"] = (df["close"].astype(float).pct_change() * 100).round(4)
            df = df[df["date"] >= start].reset_index(drop=True)
            # 新浪volume单位=股, 与归一后的东财一致, 无需转换
            return df[self._COLS].sort_values("date")
        except Exception as e:
            logger.warning(f"get_daily(新浪) {code} failed: {e}")
            return pd.DataFrame()

    def get_daily_all(self, date: str) -> pd.DataFrame:
        """
        全A股截面行情。akshare 分批拉取效率较低，建议离线后用 Parquet 读取。
        此方法用于增量补数据场景。
        """
        try:
            df = ak.stock_zh_a_spot_em()
            df = df.rename(columns={
                "代码": "code", "名称": "name", "最新价": "close",
                "涨跌幅": "pct_chg", "成交量": "volume", "成交额": "amount",
                "今开": "open", "最高": "high", "最低": "low",
            })
            df["date"] = date
            # 2026-09-02 单位归一: 东财spot成交量=手 → ×100存"股"
            df["volume"] = pd.to_numeric(df["volume"], errors="coerce") * 100
            return df[["date", "code", "name", "open", "high", "low",
                        "close", "volume", "amount", "pct_chg"]]
        except Exception as e:
            logger.warning(f"get_daily_all {date} failed: {e}")
            return pd.DataFrame()

    # ------------------------------------------------------------------
    # 分时行情（Track B）
    # ------------------------------------------------------------------
    def get_intraday(self, code: str, freq: str, start: str, end: str) -> pd.DataFrame:
        freq_map = {"1min": "1", "5min": "5", "15min": "15", "30min": "30", "60min": "60"}
        try:
            df = ak.stock_zh_a_hist_min_em(
                symbol=code,
                start_date=f"{start} 09:30:00",
                end_date=f"{end} 15:00:00",
                period=freq_map.get(freq, "5"),
                adjust="hfq",
            )
            df = df.rename(columns={
                "时间": "datetime", "开盘": "open", "最高": "high",
                "最低": "low", "收盘": "close", "成交量": "volume",
            })
            return df[["datetime", "open", "high", "low", "close", "volume"]]
        except Exception as e:
            logger.warning(f"get_intraday {code} {freq} failed: {e}")
            return pd.DataFrame()

    # ------------------------------------------------------------------
    # 涨跌停（Track B）
    # ------------------------------------------------------------------
    def get_limit_up_list(self, date: str) -> pd.DataFrame:
        try:
            df = ak.stock_zt_pool_em(date=date.replace("-", ""))
            df = df.rename(columns={
                "代码": "code", "名称": "name",
                "涨停时间": "limit_up_time",
                "几天几板": "consecutive_days",
                "炸板次数": "open_times",
                "涨停封单量": "bid_volume",
                "涨停封单额": "bid_amount",
            })
            df["final_status"] = "limit_up"
            df["date"] = date
            return df
        except Exception as e:
            logger.warning(f"get_limit_up_list {date} failed: {e}")
            return pd.DataFrame()

    # ------------------------------------------------------------------
    # 龙虎榜（Track B）
    # ------------------------------------------------------------------
    def get_dragon_tiger(self, date: str) -> pd.DataFrame:
        try:
            df = ak.stock_lhb_detail_em(start_date=date, end_date=date)
            df = df.rename(columns={
                "代码": "code", "名称": "name",
                "上榜原因": "reason",
                "净买额": "net_amount",
                "买入额": "buy_amount",
                "卖出额": "sell_amount",
            })
            df["date"] = date
            return df
        except Exception as e:
            logger.warning(f"get_dragon_tiger {date} failed: {e}")
            return pd.DataFrame()

    # ------------------------------------------------------------------
    # 财务数据（Track A）
    # ------------------------------------------------------------------
    def get_financial(self, code: str) -> pd.DataFrame:
        try:
            # 利润表（获取ROE、毛利率等）
            df = ak.stock_financial_report_sina(
                stock=f"sh{code}" if code.startswith("6") else f"sz{code}",
                symbol="利润表",
            )
            df = df.rename(columns={"报告期": "report_date"})
            df["code"] = code
            return df
        except Exception as e:
            logger.warning(f"get_financial {code} failed: {e}")
            return pd.DataFrame()

    # ------------------------------------------------------------------
    # 指数成分股（Track A，避免生存者偏差关键）
    # ------------------------------------------------------------------
    def get_index_components(self, index_code: str, date: str) -> list[str]:
        """
        akshare 只能拿到当前成分股，无历史快照。
        生产环境建议用 Tushare Pro 的 index_weight 接口获取历史成分。
        此处返回当前成分股，回测时须注意。
        """
        try:
            index_map = {
                "000300": "沪深300",
                "000905": "中证500",
                "000906": "中证800",
            }
            name = index_map.get(index_code, index_code)
            df = ak.index_stock_cons(symbol=index_code)
            return df["品种代码"].tolist()
        except Exception as e:
            logger.warning(f"get_index_components {index_code} failed: {e}")
            return []

    # ------------------------------------------------------------------
    # 交易日历
    # ------------------------------------------------------------------
    def get_trade_calendar(self, start: str = "2019-01-01", end: str = None) -> list[str]:
        try:
            df = ak.tool_trade_date_hist_sina()
            dates = df["trade_date"].astype(str).tolist()
            dates = [d for d in dates if d >= start]
            if end:
                dates = [d for d in dates if d <= end]
            return sorted(dates)
        except Exception as e:
            logger.warning(f"get_trade_calendar failed: {e}")
            return []

    # ------------------------------------------------------------------
    # 股票基本信息
    # ------------------------------------------------------------------
    def get_stock_info(self) -> pd.DataFrame:
        try:
            df = ak.stock_info_a_code_name()
            df = df.rename(columns={"code": "code", "name": "name"})
            return df
        except Exception as e:
            logger.warning(f"get_stock_info failed: {e}")
            return pd.DataFrame()

    # ------------------------------------------------------------------
    # 资金流向（Track B）
    # ------------------------------------------------------------------
    def get_capital_flow(self, code: str, date: str) -> pd.DataFrame:
        try:
            df = ak.stock_individual_fund_flow(
                stock=code,
                market="sh" if code.startswith("6") else "sz",
            )
            df = df.rename(columns={
                "日期": "date",
                "主力净流入-净额": "main_net",
                "超大单净流入-净额": "big_net",
                "小单净流入-净额": "small_net",
            })
            df["date"] = df["date"].astype(str)
            return df[df["date"] == date]
        except Exception as e:
            logger.warning(f"get_capital_flow {code} {date} failed: {e}")
            return pd.DataFrame()
