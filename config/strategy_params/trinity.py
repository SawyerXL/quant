"""
Track B「三位一体」强势股策略 — 全部参数集中配置。

三层架构：
  Layer 1: 大势 Regime Gate（regime.py）
  Layer 2: 板块强度（sector.py）
  Layer 3: 个股打分（stock_score.py）
  Layer 4: 组合执行（portfolio.py）
"""
# ── 大势层 Regime Gate ────────────────────────────────────
REGIME = {
    "benchmark_index":    "sh000932",     # 中证2000（备选 sh000852 中证1000）
    "ma_fast":            20,             # 趋势判断快线
    "ma_slow":            60,             # 趋势判断慢线
    "vol_ratio_threshold": 1.3,           # 波动率放大阈值（短期/长期）
    "advance_minus_decline_min": 20,      # 涨跌家数差 5 日均值下限
    "blowup_rate_max":    0.40,           # 炸板率上限（开板数/触板数）
    "confirm_days":       2,              # 状态切换需连续确认天数
    "state": {
        "ATTACK":  {"min_score": 3, "position_cap": 1.00, "allow_new": True},
        "NEUTRAL": {"min_score": 2, "position_cap": 0.50, "allow_new": False},
        "DEFENSE": {"min_score": 0, "position_cap": 0.00, "allow_new": False},
    },
}

# ── 板块层 Sector ─────────────────────────────────────────
SECTOR = {
    "level":             "l1",           # 申万一级（l2 数据暂缺，用 l1）
    "score_weights": {
        "momentum_5d":  0.40,            # 板块5日收益 Z
        "momentum_20d": 0.25,            # 板块20日收益 Z
        "limit_up_ratio": 0.20,          # 板块内涨停占比 Z
        "amount_ratio":  0.15,           # 成交额/60日均值 Z
    },
    "top_n":             5,               # 主线池取 Top N
    "confirm_days":      3,               # 连续 N 日 Top 10 确认主线
    "confirm_top_n":     10,
    "exit_days":         3,               # 连续 N 日跌出 Top 15 标记退出
    "exit_top_n":        15,
}

# ── 个股层 Stock Score ─────────────────────────────────────
STOCK_SCORE = {
    "weights": {
        "rps_20d":       0.35,            # 20日收益百分位排名
        "price_position": 0.25,           # 收盘/250日最高
        "vol_ratio":      0.20,           # 5日均量/60日均量
        "limit_up_gene":  0.20,           # 涨停基因：min(近60日涨停次数,3)/3
    },
    "filters": {
        "min_mktcap_billion":  30,        # 流通市值下限（亿）
        "max_mktcap_billion":  300,       # 流通市值上限（亿）
        "min_list_days":        60,       # 上市最少天数
        "min_daily_amount_wan": 200,      # 20日均成交额（万元）
        "min_price_position":  0.85,      # 收盘/250最高 下限
        "exclude_st":          True,
        "exclude_limit_up_today": True,   # 当日非一字板
    },
}

# ── 组合层 Portfolio ───────────────────────────────────────
PORTFOLIO = {
    "max_stocks":           6,
    "max_per_sector":       2,
    "max_single_pct":       0.20,
    "ma_exit_window":       10,
    "ma_exit_days":         3,            # 连续低于 MA10 天数止损
    "capital":              300_000,
    "commission":           0.00175,      # 单边 0.175%
}
