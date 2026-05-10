"""
Track A：多因子月度选股策略。
逻辑：每月末生成信号，下月初开盘等权买入中证800内排名前30只股票。
"""
import pandas as pd
import numpy as np
from loguru import logger

from config.strategy_params.multi_factor import STRATEGY_A
from data.storage import load_daily, load_financial, load_meta
from data.cleaner import filter_universe
from factors.value import compute_value_factors
from factors.quality import compute_quality_factors
from factors.momentum import compute_momentum_factors
from factors.utils import industry_zscore, winsorize, composite_score


class MultiFactor:

    def __init__(self, params: dict = None):
        self.p = params or STRATEGY_A

    # ------------------------------------------------------------------
    # 信号生成（每月末调用）
    # ------------------------------------------------------------------
    def generate_signal(self, date: str) -> list[str]:
        """
        返回目标持仓股票代码列表（等权，约30只）。
        date: 信号生成日 'YYYY-MM-DD'（通常为月末最后一个交易日）
        """
        logger.info(f"[Track A] 生成信号: {date}")

        # 1. 获取股票池（中证800历史成分，避免生存者偏差）
        components = self._get_universe(date)
        if len(components) < 50:
            logger.warning(f"股票池不足50只 ({len(components)})，信号跳过")
            return []

        # 2. 过滤（ST、新股、停牌、小市值）
        daily_cs  = self._load_cross_section(date)
        stock_info = load_meta("stock_info")
        if daily_cs.empty or stock_info.empty:
            logger.warning("截面数据或基本信息为空，信号跳过")
            return []

        daily_cs = daily_cs[daily_cs["code"].isin(components)]
        universe = filter_universe(
            daily_cs, stock_info, date,
            exclude_st=self.p["filters"]["exclude_st"],
            exclude_ipo_days=self.p["filters"]["exclude_ipo_days"],
            min_mktcap_quantile=self.p["filters"]["min_mktcap_quantile"],
        )
        logger.info(f"过滤后股票池: {len(universe)} 只")

        # 3. 计算因子
        factors_df = self._compute_factors(universe, date)
        if factors_df.empty:
            return []

        # 4. 行业内标准化
        industry_map = stock_info.set_index("code")["industry_l2"]
        for col in ["bp", "ep", "roe_ttm", "margin_stability", "reversal_20d", "momentum_240d_minus_20d"]:
            if col in factors_df.columns:
                factors_df[col] = industry_zscore(
                    winsorize(factors_df.set_index("code")[col]),
                    industry_map.reindex(factors_df["code"].values),
                ).values

        # 5. 合成得分
        fw = self.p["factor_weights"]
        value_score   = composite_score(
            {c: factors_df.set_index("code")[c] for c in ["bp", "ep"]},
            {"bp": 1, "ep": 1}
        )
        quality_score = composite_score(
            {c: factors_df.set_index("code")[c] for c in ["roe_ttm", "margin_stability"]},
            {"roe_ttm": 1, "margin_stability": 1}
        )
        momentum_score = composite_score(
            {c: factors_df.set_index("code")[c] for c in ["reversal_20d", "momentum_240d_minus_20d"]},
            {"reversal_20d": 1, "momentum_240d_minus_20d": 1}
        )
        total = (
            value_score    * fw["value"] +
            quality_score  * fw["quality"] +
            momentum_score * fw["momentum"]
        )

        # 6. 选前 N 只
        selected = total.nlargest(self.p["n_holdings"]).index.tolist()
        logger.info(f"[Track A] 信号股票: {selected[:5]}...（共{len(selected)}只）")
        return selected

    # ------------------------------------------------------------------
    # 权重计算（等权）
    # ------------------------------------------------------------------
    def compute_weights(self, codes: list[str]) -> dict[str, float]:
        n = len(codes)
        if n == 0:
            return {}
        w = round(1.0 / n, 6)
        return {c: w for c in codes}

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------
    def _get_universe(self, date: str) -> list[str]:
        from data.source import get_source
        src = get_source()
        return src.get_index_components(
            "000906" if self.p["universe"] == "csi800" else "000300",
            date
        )

    def _load_cross_section(self, date: str) -> pd.DataFrame:
        from data.storage import load_daily_cross_section
        return load_daily_cross_section(date)

    def _compute_factors(self, universe: list[str], date: str) -> pd.DataFrame:
        """加载并计算所有因子，返回合并后的截面 DataFrame。"""
        end = date
        start = pd.Timestamp(date) - pd.DateOffset(years=2)
        start = start.strftime("%Y-%m-%d")

        # 价格序列（用于动量因子）
        price_frames = {}
        for code in universe:
            df = load_daily(code, start, end)
            if len(df) >= 250:
                price_frames[code] = df.set_index("date")["close"]

        if len(price_frames) < 20:
            logger.warning("有效价格序列不足20只，跳过")
            return pd.DataFrame()

        close_pivot = pd.DataFrame(price_frames)
        momentum_df = compute_momentum_factors(close_pivot)

        # 财务数据（用于价值/质量因子）
        fin_frames = []
        for code in universe:
            fin = load_financial(code)
            if not fin.empty:
                fin["code"] = code
                fin_frames.append(fin)

        if not fin_frames:
            logger.warning("财务数据为空")
            return pd.DataFrame()

        financial_df = pd.concat(fin_frames)
        quality_df = compute_quality_factors(financial_df)

        # 截面估值因子（需要 book_value_per_share 和 eps_ttm，从财务数据合并）
        latest_fin = financial_df.sort_values("report_date").groupby("code").last().reset_index()
        cs_df = self._load_cross_section(date).merge(
            latest_fin[["code", "book_value_per_share", "eps_ttm"]],
            on="code", how="inner"
        )
        cs_df = cs_df[cs_df["code"].isin(universe)]
        value_df = compute_value_factors(cs_df)

        # 合并所有因子
        result = (
            value_df
            .merge(quality_df, on="code", how="outer")
            .merge(momentum_df, on="code", how="outer")
        )
        return result[result["code"].isin(universe)].reset_index(drop=True)
