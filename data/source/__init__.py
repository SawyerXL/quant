from config.settings import DATA_SOURCE
from .base import DataSource


def get_source() -> DataSource:
    """工厂函数：根据配置返回数据源实例。切换数据源只改 config/settings.py。"""
    if DATA_SOURCE == "akshare":
        from .akshare_source import AkshareSource
        return AkshareSource()
    elif DATA_SOURCE == "mcp":
        from .mcp_source import MCPSource
        return MCPSource()
    elif DATA_SOURCE == "tushare":
        from .tushare_source import TushareSource
        return TushareSource()
    else:
        raise ValueError(f"未知数据源: {DATA_SOURCE}，可选: akshare | mcp | tushare")
