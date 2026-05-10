STRATEGY_B = {
    "name": "trinity_v1",

    # ---------- 大势层 ----------
    "market_score": {
        "quant_weight": 0.70,
        "manual_weight": 0.30,      # 人工情绪周期判定占30%
        "position_map": {           # M_score → 仓位
            (80, 100): 0.90,
            (60, 80):  0.70,
            (40, 60):  0.50,
            (25, 40):  0.30,
            (0,  25):  0.10,
        },
    },

    # ---------- 板块层 ----------
    "sector_score": {
        "strength_weight": 0.60,
        "rhythm_weight":   0.25,    # 人工板块节奏标注
        "status_weight":   0.15,
        "min_score":       60,      # 低于此分不持有
        "max_sectors":     3,
        "max_sector_pct":  0.50,
    },

    # ---------- 个股层 ----------
    "stock_score": {
        "momentum_weight":      0.30,
        "capital_flow_weight":  0.25,
        "sector_rank_weight":   0.20,   # 量化版"最票"
        "technical_weight":     0.15,
        "liquidity_weight":     0.10,
        "stocks_per_sector":    2,
    },

    "filters": {
        "min_mktcap_billion":   5,      # 流通市值5亿以上
        "max_mktcap_billion":   500,
        "min_daily_turnover":   1e8,    # 日均成交额1亿以上
        "exclude_st":           True,
        "exclude_ipo_days":     120,
    },

    "risk": {
        "max_single_position":      0.08,   # 8%
        "max_sector_position":      0.50,
        "stop_loss_pct":            0.08,   # 个股-8%止损
        "sector_exit_score":        40,     # 板块分低于40清仓
        "max_drawdown_circuit":     0.20,   # Track B 独立熔断
    },

    "capital": 300_000,     # Track B 分配资金
    "slippage": 0.005,      # 强势股滑点千5
}
