"""Declarative strategy parameter metadata — auto-generates UI forms."""
from scripts.backtest_config import BacktestConfig

# Each param: {key, label, type, default, min?, max?, step?, values?, category, description}
STRATEGY_PARAMS = [
    # ── Pool & Universe ──
    {"key": "pool_size", "label": "股票池大小", "type": "int", "default": 60,
     "min": 10, "max": 200, "step": 10,
     "category": "股票池", "description": "每期从成交额排名中选取的股票数量"},

    # ── Filters ──
    {"key": "overheat_mode", "label": "过热处理", "type": "select",
     "values": ["reduce", "eliminate", "off"],
     "labels": ["减仓(保留)", "排除(去掉)", "关闭(不过滤)"],
     "default": "reduce",
     "category": "过滤", "description": "对连涨过热股票的处理方式"},

    # ── Entry ──
    {"key": "rebalance_freq", "label": "调仓频率", "type": "select",
     "values": ["weekly", "biweekly", "monthly"],
     "labels": ["每周", "双周", "每月"],
     "default": "biweekly",
     "category": "调仓", "description": "持仓调整频率"},

    {"key": "max_position_pct", "label": "单票上限(%)", "type": "int",
     "min": 1, "max": 30, "step": 1, "default": 10,
     "category": "调仓", "description": "单只股票最大权重"},

    {"key": "industry_cap", "label": "行业上限(%)", "type": "int",
     "min": 10, "max": 60, "step": 5, "default": 50,
     "category": "调仓", "description": "单一行业最大集中度"},

    # ── Exits ──
    {"key": "abs_stop", "label": "绝对止损(%)", "type": "float",
     "min": -30, "max": -3, "step": 1, "default": -12,
     "category": "止损", "description": "从成本价最大亏损比例（负数）"},

    {"key": "trailing_stop", "label": "追踪止损(%)", "type": "float",
     "min": -30, "max": -5, "step": 1, "default": -18,
     "category": "止损", "description": "从最高点最大回撤比例（负数）"},

    {"key": "ma_exit_days", "label": "MA10下穿天数", "type": "int",
     "min": 1, "max": 10, "step": 1, "default": 4,
     "category": "止损", "description": "连续跌破MA10多少天后卖出"},

    # ── Take Profit ──
    {"key": "take_profit_tiers", "label": "止盈档位(%)", "type": "text",
     "default": "25,50",
     "category": "止盈", "description": "浮盈达到后分批卖出的档位，逗号分隔。如 25,50 表示+25%卖1/3，+50%再卖1/3"},

    # ── 做T增厚 ──
    {"key": "enable_t0", "label": "做T增厚", "type": "select",
     "values": [False, True], "labels": ["关闭", "开启"],
     "default": False,
     "category": "做T", "description": "底仓T+0增厚。全市场回测(300只/14万笔/扣费): 胜率94.5%, 年化+26%。实盘保守取+20%"},

    {"key": "t0_annual_enhancement", "label": "做T年化增厚(%)", "type": "float",
     "min": 0, "max": 30, "step": 1, "default": 20,
     "category": "做T", "description": "做T的保守年化增厚假设（实盘折扣后）"},
]


def get_strategy_spec() -> dict:
    """Return full strategy metadata spec for frontend form generation."""
    categories = {}
    for p in STRATEGY_PARAMS:
        cat = p.get("category", "其他")
        if cat not in categories:
            categories[cat] = {"label": cat, "params": []}
        # Build safe param spec (no functions)
        spec = {
            "key": p["key"], "label": p["label"], "type": p["type"],
            "default": p["default"],
            "description": p.get("description", ""),
        }
        if "min" in p: spec["min"] = p["min"]
        if "max" in p: spec["max"] = p["max"]
        if "step" in p: spec["step"] = p["step"]
        if "values" in p: spec["values"] = p["values"]
        if "labels" in p: spec["labels"] = p["labels"]
        categories[cat]["params"].append(spec)

    return {
        "strategy_name": "个人策略 v3",
        "description": "成交额TOP池 → 过热过滤 → MA10附近入场 → 止损止盈退出",
        "categories": list(categories.values()),
        "defaults": {p["key"]: p["default"] for p in STRATEGY_PARAMS},
    }
