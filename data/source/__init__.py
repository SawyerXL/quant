from config.settings import DATA_SOURCE
from .base import DataSource


def get_source() -> DataSource:
    """工厂函数：根据配置返回数据源实例。切换数据源只改 config/settings.py。"""
    if DATA_SOURCE == "akshare":
        # 主源 akshare(内部已含东财->新浪降级)，最后兜底 MCP；MCP不可用则降级为纯akshare
        from .akshare_source import AkshareSource
        from .fallback import FallbackDataSource
        chain = [AkshareSource()]
        try:
            from .mcp_source import MCPSource
            chain.append(MCPSource())
        except Exception as e:
            from loguru import logger
            logger.warning(f"MCP兜底源不可用，仅用akshare: {e}")
        return FallbackDataSource(chain)
    elif DATA_SOURCE == "mcp":
        from .mcp_source import MCPSource
        return MCPSource()
    elif DATA_SOURCE == "tushare":
        from .tushare_source import TushareSource
        return TushareSource()
    else:
        raise ValueError(f"未知数据源: {DATA_SOURCE}，可选: akshare | mcp | tushare")
