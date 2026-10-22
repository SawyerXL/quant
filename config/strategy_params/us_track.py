"""US 独立 track 参数（2026-10-22 用户裁定 A：独立 track_us）。

风险口径（用户已确认）：
- US sleeve = 20 万（12.5% × 核心 160 万），独立 total_assets/台账/NAV。
- 单 ETF 上限 100%（一个宽基载体，不做行业分散）。
- **sleeve 不设 15% 熔断**（独立回撤 −37% 会在 2020/2022 冻结）；
  核心账户级 15% 线是否含 US，按用户后续账户安排执行。
- 单笔订单仍受全局 MAX_SINGLE_ORDER_VALUE=10 万约束（拆单）。
"""
US_TRACK = {
    "name": "us_etf_djia",
    "capital": 200_000,
    "core_capital": 1_600_000,
    "vehicle": "513400",
    "us_weight": 0.125,
    "premium_threshold": 0.05,
    "rebalance_band_pp": 0.03,   # 相对核心的 3pp 带宽
    "entry_time": "14:45",
    "fee": 0.0003,
    "cash_yield": 0.014,
    "risk": {
        "max_single_position": 1.0,
        "max_industry_position": 1.0,
        "max_drawdown_circuit": 1.0,   # sleeve 无熔断；核心 15% 线另计
    },
}
