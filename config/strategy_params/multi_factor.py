STRATEGY_A = {
    "name": "multi_factor_v1",
    "universe": "csi800",           # 中证800成分股
    "rebalance_freq": "monthly",
    "rebalance_day": "last_trade",  # 每月最后一个交易日生成信号
    "execute_day": "next_first",    # 下月第一个交易日开盘执行
    "execute_time": "09:35",        # 开盘稳定后执行
    "n_holdings": 30,
    "weight_method": "equal",

    "factor_weights": {
        "value":    0.30,
        "quality":  0.40,
        "momentum": 0.30,
    },
    "factors": {
        "value":    ["bp", "ep"],
        "quality":  ["roe_ttm", "margin_stability"],
        "momentum": ["reversal_20d", "momentum_240d_minus_20d"],
    },

    "filters": {
        "exclude_st":               True,
        "exclude_ipo_days":         252,    # 上市不满1年排除
        "exclude_suspended_days":   60,     # 累计停牌超60天排除
        "min_mktcap_quantile":      0.10,   # 流通市值最小10%排除
    },

    "risk": {
        "max_single_position":      0.05,   # 5%
        "max_industry_position":    0.30,
        "max_drawdown_circuit":     0.15,   # Track A 独立熔断
    },

    "capital": 600_000,     # Track A 分配资金
    "slippage": 0.002,
}
