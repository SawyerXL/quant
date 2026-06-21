"""
多源自动降级：按顺序尝试各数据源，前一个抛异常或返回空就落到下一个。
用途——东财封IP/akshare整挂时，无人值守地切到备源(新浪在AkshareSource内部已处理，
此层负责 akshare 整体 -> MCP 的兜底)。
"""
import pandas as pd
from loguru import logger
from .base import DataSource


def _is_empty(result) -> bool:
    """统一判空：DataFrame 看 .empty，list 看长度，None 视为空。"""
    if result is None:
        return True
    if isinstance(result, pd.DataFrame):
        return result.empty
    if isinstance(result, (list, tuple)):
        return len(result) == 0
    return False


class FallbackDataSource(DataSource):
    def __init__(self, sources: list[DataSource]):
        if not sources:
            raise ValueError("FallbackDataSource 至少需要一个数据源")
        self.sources = sources

    def _dispatch(self, method: str, *args, **kwargs):
        last_exc = None
        for i, src in enumerate(self.sources):
            try:
                result = getattr(src, method)(*args, **kwargs)
                if not _is_empty(result):
                    if i > 0:
                        logger.warning(f"{method} 落到备源[{i}] {type(src).__name__}")
                    return result
            except Exception as e:
                last_exc = e
                logger.warning(f"{method} 源[{i}] {type(src).__name__} 失败: {e}")
        if last_exc:
            logger.error(f"{method} 所有源均失败，最后异常: {last_exc}")
        # 全部空/失败：返回与首源契约一致的空值
        return [] if method in ("get_index_components", "get_trade_calendar") else pd.DataFrame()

    def get_daily(self, code, start, end):
        return self._dispatch("get_daily", code, start, end)

    def get_daily_all(self, date):
        return self._dispatch("get_daily_all", date)

    def get_intraday(self, code, freq, start, end):
        return self._dispatch("get_intraday", code, freq, start, end)

    def get_limit_up_list(self, date):
        return self._dispatch("get_limit_up_list", date)

    def get_dragon_tiger(self, date):
        return self._dispatch("get_dragon_tiger", date)

    def get_financial(self, code):
        return self._dispatch("get_financial", code)

    def get_index_components(self, index_code, date):
        return self._dispatch("get_index_components", index_code, date)

    def get_trade_calendar(self, start="2019-01-01", end=None):
        return self._dispatch("get_trade_calendar", start, end)

    def get_stock_info(self):
        return self._dispatch("get_stock_info")

    def get_capital_flow(self, code, date):
        return self._dispatch("get_capital_flow", code, date)
