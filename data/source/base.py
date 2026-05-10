from abc import ABC, abstractmethod
import pandas as pd


class DataSource(ABC):
    """
    所有数据源的抽象基类。
    切换数据源只需在 config/settings.py 改 DATA_SOURCE 变量，
    并在 data/source/__init__.py 的 get_source() 中添加对应实例。
    """

    @abstractmethod
    def get_daily(self, code: str, start: str, end: str) -> pd.DataFrame:
        """
        单只股票日线行情。
        返回列：date, open, high, low, close, volume, amount, adj_factor
        date 为 str 'YYYY-MM-DD'，已按 date 升序排列
        """

    @abstractmethod
    def get_daily_all(self, date: str) -> pd.DataFrame:
        """
        全市场某日行情快照（用于截面因子计算）。
        返回列：code, open, high, low, close, volume, amount, pct_chg
        """

    @abstractmethod
    def get_intraday(self, code: str, freq: str, start: str, end: str) -> pd.DataFrame:
        """
        分时行情。freq: '1min' | '5min' | '15min' | '30min' | '60min'
        返回列：datetime, open, high, low, close, volume
        """

    @abstractmethod
    def get_limit_up_list(self, date: str) -> pd.DataFrame:
        """
        某日涨停股票列表（含时间戳）。
        返回列：code, name, limit_up_time, open_times（炸板次数）, final_status
        """

    @abstractmethod
    def get_dragon_tiger(self, date: str) -> pd.DataFrame:
        """
        龙虎榜数据。
        返回列：code, name, reason, buy_amount, sell_amount, net_amount, trader_type
        """

    @abstractmethod
    def get_financial(self, code: str) -> pd.DataFrame:
        """
        财务数据（季报）。
        返回列：report_date, roe, gross_margin, net_profit, revenue, total_assets, ...
        """

    @abstractmethod
    def get_index_components(self, index_code: str, date: str) -> list[str]:
        """
        指数在指定日期的成分股列表（历史快照，避免生存者偏差）。
        index_code: '000300'(沪深300) | '000905'(中证500) | '000906'(中证800)
        """

    @abstractmethod
    def get_trade_calendar(self, start: str = "2019-01-01", end: str = None) -> list[str]:
        """
        交易日历，返回 'YYYY-MM-DD' 字符串列表，升序。
        """

    @abstractmethod
    def get_stock_info(self) -> pd.DataFrame:
        """
        全市场股票基本信息。
        返回列：code, name, industry_l1, industry_l2, list_date, is_st, float_mktcap
        """

    @abstractmethod
    def get_capital_flow(self, code: str, date: str) -> pd.DataFrame:
        """
        资金流向（主力净流入等）。
        返回列：date, main_net, big_net, small_net
        """
