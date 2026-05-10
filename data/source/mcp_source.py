"""
恒生聚源金融数据 MCP 数据源实现。
协议：Streamable HTTP（JSON-RPC 2.0）
API：FinQuery（自然语言问数）/ SmartStockSelection（智能选股）
"""
import re
import httpx
import pandas as pd
from io import StringIO
from loguru import logger
from .base import DataSource


class MCPSource(DataSource):
    """
    恒生聚源 MCP 数据源。
    通过自然语言查询获取结构化金融数据，响应为 Markdown 表格，自动解析成 DataFrame。
    批量历史数据拉取用 get_daily 逐只下载后存 Parquet；
    截面快照从 Parquet 读取（见 data/storage.py），不依赖 MCP 实时接口。
    """

    TOOL_ENDPOINT = "https://api.gildata.com/mcp-servers/aidata-assistant-srv-tool"
    HEADERS = {
        "Accept":           "application/json,text/event-stream",
        "Content-Type":     "application/json",
        "MCP-Protocol-Version": "2025-06-18",
    }

    def __init__(self):
        from config.settings import MCP_API_KEY
        if not MCP_API_KEY:
            raise ValueError("MCP_API_KEY 未配置，请检查 .env 文件")
        self.url = f"{self.TOOL_ENDPOINT}?token={MCP_API_KEY}"
        self._initialized = False

    # ------------------------------------------------------------------
    # 内部：HTTP 调用
    # ------------------------------------------------------------------
    def _call(self, tool: str, query: str, timeout: int = 30) -> list[dict]:
        """
        调用 MCP 工具，返回 results 列表。
        每次调用前先发 initialize（服务端无状态，每次独立）。
        """
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            client.post(self.url, headers=self.HEADERS, json={
                "jsonrpc": "2.0", "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "quant", "version": "1.0"}},
                "id": 1,
            })
            r = client.post(self.url, headers=self.HEADERS, json={
                "jsonrpc": "2.0", "method": "tools/call",
                "params": {"name": tool, "arguments": {"query": query}},
                "id": 2,
            })
        content = r.json().get("result", {}).get("content", [])
        if not content:
            return []
        import json
        raw = json.loads(content[0].get("text", "{}"))
        if raw.get("code") != "0":
            logger.warning(f"MCP 返回异常: {raw}")
            return []
        return raw.get("results", [])

    # ------------------------------------------------------------------
    # 内部：Markdown 表格解析
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_markdown_table(table_md: str) -> pd.DataFrame:
        """
        将 MCP 返回的 Markdown 表格字符串解析成 DataFrame。
        格式示例：
          |列A|列B|列C|
          |---|---|---|
          |v1 |v2 |v3 |
        """
        lines = [l.strip() for l in table_md.strip().split("\n") if l.strip()]
        if len(lines) < 3:
            return pd.DataFrame()
        # 去掉分隔行（全是 --- 的那一行）
        data_lines = [l for l in lines if not re.match(r"^\|[\s\-|]+\|$", l)]
        if len(data_lines) < 2:
            return pd.DataFrame()
        csv_text = "\n".join(data_lines)
        try:
            df = pd.read_csv(
                StringIO(csv_text), sep="|",
                skipinitialspace=True, engine="python",
            )
            # read_csv 会把 | 开头结尾变成两列空列，去掉
            df = df.iloc[:, 1:-1].reset_index(drop=True)
            df.columns = [c.strip() for c in df.columns]
            for col in df.columns:
                df[col] = df[col].map(lambda x: x.strip() if isinstance(x, str) else x)
            return df
        except Exception as e:
            logger.warning(f"Markdown 表格解析失败: {e}")
            return pd.DataFrame()

    def _first_table(self, results: list[dict]) -> pd.DataFrame:
        """取 results 中第一个 table_markdown 并解析。"""
        for r in results:
            md = r.get("table_markdown", "")
            if md:
                return self._parse_markdown_table(md)
        return pd.DataFrame()

    # ------------------------------------------------------------------
    # 内部：模糊列名匹配（处理全角/半角括号不一致问题）
    # ------------------------------------------------------------------
    @staticmethod
    def _rename_fuzzy(df: pd.DataFrame, mapping: dict) -> pd.DataFrame:
        """
        按关键词模糊匹配列名并重命名。
        mapping: {关键词: 目标列名}，列名包含关键词即匹配。
        """
        rename = {}
        for col in df.columns:
            col_clean = col.replace("（", "(").replace("）", ")").replace(" ", "")
            for keyword, target in mapping.items():
                kw_clean = keyword.replace("（", "(").replace("）", ")").replace(" ", "")
                if kw_clean in col_clean and target not in rename.values():
                    rename[col] = target
                    break
        return df.rename(columns=rename)

    # ------------------------------------------------------------------
    # 日线行情
    # ------------------------------------------------------------------
    def get_daily(self, code: str, start: str, end: str) -> pd.DataFrame:
        query = (
            f"查询A股股票代码 {code} 从 {start} 到 {end} 的日K线行情数据，"
            f"包含交易日、今开盘价、最高价、最低价、收盘价、成交量（万股）、成交额（万元）、涨跌幅(%)、换手率(%)、是否停牌。"
        )
        results = self._call("FinQuery", query)
        df = self._first_table(results)
        if df.empty:
            return pd.DataFrame()

        keyword_map = {
            "交易日": "date", "今开盘": "open", "最高价": "high",
            "最低价": "low", "收盘价": "close",
            "成交量": "volume", "成交额": "amount",
            "涨跌幅": "pct_chg", "换手率": "turnover",
            "是否停牌": "is_suspended",
        }
        df = self._rename_fuzzy(df, keyword_map)
        df["code"] = code
        if "date" in df.columns:
            df["date"] = df["date"].astype(str)
            df = df.sort_values("date").reset_index(drop=True)
        for col in ["open", "high", "low", "close", "volume", "amount", "pct_chg"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df

    def get_daily_all(self, date: str) -> pd.DataFrame:
        """
        MCP 不适合一次拉取全市场快照（5000+只逐个查询太慢）。
        实际使用请从本地 Parquet 读取（data/storage.load_daily_cross_section）。
        此方法仅返回空 DataFrame 并记录警告。
        """
        logger.warning("MCPSource.get_daily_all 不支持，请从 Parquet 缓存读取截面数据")
        return pd.DataFrame()

    # ------------------------------------------------------------------
    # 分时行情（Track B）
    # ------------------------------------------------------------------
    def get_intraday(self, code: str, freq: str, start: str, end: str) -> pd.DataFrame:
        freq_cn = {"1min": "1分钟", "5min": "5分钟", "15min": "15分钟",
                   "30min": "30分钟", "60min": "60分钟"}.get(freq, "5分钟")
        query = (
            f"查询股票代码 {code} 从 {start} 到 {end} 的 {freq_cn} 分时行情，"
            f"包含时间、开盘价、最高价、最低价、收盘价、成交量。"
        )
        results = self._call("FinQuery", query)
        df = self._first_table(results)
        if df.empty:
            return pd.DataFrame()
        col_map = {"时间": "datetime", "开盘": "open", "最高": "high",
                   "最低": "low",  "收盘": "close", "成交量": "volume"}
        return df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})

    # ------------------------------------------------------------------
    # 涨跌停（Track B）
    # ------------------------------------------------------------------
    def get_limit_up_list(self, date: str) -> pd.DataFrame:
        query = (
            f"查询 {date} 当天A股市场的涨停股票列表，"
            f"包含股票代码、股票名称、涨停时间、炸板次数、涨停封单额、连续涨停天数。"
        )
        results = self._call("FinQuery", query)
        df = self._first_table(results)
        if df.empty:
            return pd.DataFrame()
        col_map = {
            "股票代码": "code", "股票名称": "name",
            "涨停时间": "limit_up_time", "炸板次数": "open_times",
            "涨停封单额": "bid_amount", "连续涨停天数": "consecutive_days",
        }
        df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
        df["date"] = date
        df["final_status"] = "limit_up"
        return df

    # ------------------------------------------------------------------
    # 龙虎榜（Track B）
    # ------------------------------------------------------------------
    def get_dragon_tiger(self, date: str) -> pd.DataFrame:
        query = (
            f"查询 {date} 的龙虎榜数据，"
            f"包含股票代码、股票名称、上榜原因、买入金额、卖出金额、净买入金额。"
        )
        results = self._call("FinQuery", query)
        df = self._first_table(results)
        if df.empty:
            return pd.DataFrame()
        col_map = {
            "股票代码": "code", "股票名称": "name", "上榜原因": "reason",
            "买入金额": "buy_amount", "卖出金额": "sell_amount", "净买入金额": "net_amount",
        }
        df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
        df["date"] = date
        return df

    # ------------------------------------------------------------------
    # 财务数据（Track A）
    # ------------------------------------------------------------------
    def get_financial(self, code: str) -> pd.DataFrame:
        query = (
            f"查询股票代码 {code} 最近8个季度的财务数据，"
            f"包含报告期、净资产收益率ROE、毛利率、净利润、营业收入、总资产、每股净资产、每股收益。"
        )
        results = self._call("FinQuery", query)
        df = self._first_table(results)
        if df.empty:
            return pd.DataFrame()
        col_map = {
            "报告期": "report_date", "时间": "report_date",
            "净资产收益率-摊薄ROE": "roe", "财务分析指标数额": "value",
            "毛利率": "gross_margin", "净利润": "net_profit",
            "营业收入": "revenue", "每股净资产": "book_value_per_share",
            "每股收益": "eps",
        }
        df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
        df["code"] = code
        return df

    # ------------------------------------------------------------------
    # 指数成分股（Track A，避免生存者偏差关键）
    # ------------------------------------------------------------------
    def get_index_components(self, index_code: str, date: str) -> list[str]:
        """
        注意：MCP 返回的是当前最新成分股，无历史快照能力。
        回测中若需历史成分股，需配合 Tushare Pro 的 index_weight 接口。
        """
        index_name = {
            "000300": "沪深300", "000905": "中证500", "000906": "中证800",
        }.get(index_code, index_code)
        query = f"查询{index_name}指数（{index_code}）当前的全部成分股列表，返回股票代码。"
        results = self._call("FinQuery", query)
        df = self._first_table(results)
        if df.empty:
            return []
        # 尝试找股票代码列
        for col in ["股票代码", "证券代码", "code"]:
            if col in df.columns:
                codes = df[col].dropna().tolist()
                # 去掉交易所后缀（如 000001.SZ → 000001）
                return [str(c).split(".")[0] for c in codes if c]
        return []

    # ------------------------------------------------------------------
    # 交易日历
    # ------------------------------------------------------------------
    def get_trade_calendar(self, start: str = "2019-01-01", end: str = None) -> list[str]:
        end_str = end or "2026-12-31"
        query = f"查询A股从 {start} 到 {end_str} 的交易日历，返回所有交易日期列表。"
        results = self._call("FinQuery", query, timeout=60)
        df = self._first_table(results)
        if df.empty:
            # 降级：从 akshare 获取
            logger.warning("MCP 交易日历查询失败，降级到 akshare")
            try:
                import akshare as ak
                df2 = ak.tool_trade_date_hist_sina()
                dates = df2["trade_date"].astype(str).tolist()
                return sorted([d for d in dates if start <= d <= end_str])
            except Exception:
                return []
        # 找日期列
        for col in df.columns:
            if "日期" in col or "date" in col.lower():
                return sorted(df[col].astype(str).tolist())
        return []

    # ------------------------------------------------------------------
    # 股票基本信息
    # ------------------------------------------------------------------
    def get_stock_info(self) -> pd.DataFrame:
        query = (
            "查询A股全部上市股票的基本信息，"
            "包含股票代码、股票名称、申万行业一级分类、申万行业二级分类、上市日期、是否ST、流通市值。"
        )
        results = self._call("FinQuery", query, timeout=60)
        df = self._first_table(results)
        if df.empty:
            return pd.DataFrame()
        col_map = {
            "股票代码": "code", "股票名称": "name",
            "一级行业": "industry_l1", "二级行业": "industry_l2",
            "上市日期": "list_date", "是否ST": "is_st",
            "流通市值": "float_mktcap",
        }
        return df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})

    # ------------------------------------------------------------------
    # 资金流向（Track B）
    # ------------------------------------------------------------------
    def get_capital_flow(self, code: str, date: str) -> pd.DataFrame:
        query = (
            f"查询股票代码 {code} 在 {date} 的资金流向数据，"
            f"包含主力净流入、超大单净流入、小单净流入。"
        )
        results = self._call("FinQuery", query)
        df = self._first_table(results)
        if df.empty:
            return pd.DataFrame()
        col_map = {
            "主力净流入": "main_net", "超大单净流入": "big_net", "小单净流入": "small_net",
        }
        df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
        df["code"] = code
        df["date"] = date
        return df

    # ------------------------------------------------------------------
    # 工具方法：批量下载历史日线（init_history 脚本使用）
    # ------------------------------------------------------------------
    def batch_download_daily(
        self,
        codes: list[str],
        start: str,
        end: str,
        save_fn=None,
        max_workers: int = 5,
    ) -> dict:
        """
        并行下载多只股票历史日线，存入 Parquet。
        save_fn: callable(code, df)，默认用 data.storage.save_daily
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        if save_fn is None:
            from data.storage import save_daily
            save_fn = save_daily

        results = {"success": 0, "failed": []}

        def _download(code):
            try:
                df = self.get_daily(code, start, end)
                if not df.empty:
                    save_fn(code, df)
                    return code, True
                return code, False
            except Exception as e:
                logger.warning(f"下载失败 {code}: {e}")
                return code, False

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_download, c): c for c in codes}
            for i, future in enumerate(as_completed(futures)):
                code, ok = future.result()
                if ok:
                    results["success"] += 1
                else:
                    results["failed"].append(code)
                if (i + 1) % 100 == 0:
                    logger.info(f"进度: {i+1}/{len(codes)}")

        return results
