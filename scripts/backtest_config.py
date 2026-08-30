"""
策略回测配置 — 所有参数集中管理，dataclass
"""
from dataclasses import dataclass, field
from typing import Tuple

@dataclass
class BacktestConfig:
    """回测引擎 + 策略参数"""

    # ── 回测区间 ──
    start_date: str = "2019-01-01"
    end_date: str = "2026-06-30"

    # ── 股票池 ──
    pool_size: int = 30          # 成交额TOP-N
    rebalance_freq: str = "biweekly"  # "biweekly" | "weekly" | "monthly"

    # ── 过热过滤（可参数化） ──
    max_20d_return: float = 50.0     # 20日涨幅上限(%)
    max_consec_up_days: int = 8       # 连涨天数上限
    max_5d_return: float = 15.0       # 5日涨幅上限(%)
    min_dist_from_high: float = 3.0   # 距20日高最小回落%(入场位置)
    max_vol20: float = 999.0          # 20日波动率上限%(拥挤度过滤, 999=关; 8/19跌停潮凶手票均>6%)
    vol20_use_today: bool = False     # False=只用T-1及以前(严格口径, 无当日look-ahead)

    # ── 止损止盈 ──
    absolute_stop: float = -0.12      # 绝对止损(从成本)
    trailing_stop: float = -0.18      # 追踪止损(从最高点)
    take_profit_1: float = 0.25       # 第一档止盈(卖1/3)
    take_profit_2: float = 0.50       # 第二档止盈(再卖1/3)
    ma_exit_days: int = 3             # MA10连续跌破天数

    # ── 交易成本 ──
    commission: float = 0.0013        # 双边(佣金+印花+滑点)
    cash_yield: float = 0.02          # 现金年化收益
    min_bars: int = 250               # 最少K线数

    # ── 仓位 ──
    max_position_pct: float = 0.10    # 新票仓位上限
    max_single: float = 0.15           # 单票绝对上限(旧持仓超标时削减)
    timing_scale: float = 1.0          # MA200择时仓位系数(1=现网; <1=熊市降仓更狠)

    # ── 账户回撤熔断(回测模拟, 实盘红线25%) ──
    halt_mode: str = "none"            # "none" | "A"全清仓 | "B"暂停开仓(持仓按MA10自然退出) | "C"触发日降至30%底仓
    halt_dd_limit: float = 0.25        # 触发线
    halt_recover_rebound: float = 0.05 # 从触发后最低点反弹5%恢复
    halt_recover_min_days: int = 10    # 触发后至少10个交易日才允许恢复

    # ── 做T增厚 (2026-08全市场回测验证: 年化+26%) ──
    enable_t0: bool = False            # 做T总开关(默认关, 验证期后开)
    t0_trigger_pct: float = 2.0        # 涨跌≥2%触发
    t0_settle_pct: float = 1.0         # 回落/反弹1%了结
    t0_position_frac: float = 1/3      # 只用1/3仓位
    t0_annual_enhancement: float = 0.20  # 保守年化增厚(实盘折扣后)

    # ── 过热处理模式 ──
    overheat_mode: str = "reduce"   # "eliminate"=直接淘汰 | "reduce"=减仓+紧止损

    # ── 消融测试开关 ──
    enable_stops: bool = True       # 止损止盈总开关
    enable_ma10_exit: bool = True   # MA10退出
    enable_trailing_stop: bool = True  # 追踪止损
    enable_absolute_stop: bool = True  # 绝对止损
    enable_take_profit: bool = True    # 止盈
    enable_entry_filter: bool = True   # MA10入场过滤(距高要求)
    ma10_entry_mode: str = "dist_from_high"  # "dist_from_high"=距20日高 | "near_ma10"=现价在MA10±5%内
    ma10_tolerance: float = 5.0              # near_ma10模式的容差(%)
    overheat_position_ratio: float = 0.5  # 过热股仓位系数(0.5=减半)
    overheat_stop_tighten: float = 0.5    # 过热股止损收紧系数(0.5=止损线收紧一半)

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if not k.startswith('_')}


# ── 网格扫描预设 ──
STAGE_A_CONFIGS = [
    BacktestConfig(max_20d_return=50, max_consec_up_days=8,  max_5d_return=15, min_dist_from_high=3),
    BacktestConfig(max_20d_return=65, max_consec_up_days=10, max_5d_return=20, min_dist_from_high=5),
    BacktestConfig(max_20d_return=80, max_consec_up_days=12, max_5d_return=25, min_dist_from_high=8),
    BacktestConfig(max_20d_return=100, max_consec_up_days=15, max_5d_return=35, min_dist_from_high=12),
    BacktestConfig(max_20d_return=150, max_consec_up_days=999, max_5d_return=50, min_dist_from_high=20),
]

STAGE_B_CONFIGS = [
    (BacktestConfig(absolute_stop=-0.08,  trailing_stop=-0.08,  take_profit_1=0.20, take_profit_2=0.40), "A1"),
    (BacktestConfig(absolute_stop=-0.08,  trailing_stop=-0.12,  take_profit_1=0.25, take_profit_2=0.50), "A2"),
    (BacktestConfig(absolute_stop=-0.10,  trailing_stop=-0.10,  take_profit_1=0.25, take_profit_2=0.50), "A3"),
    (BacktestConfig(absolute_stop=-0.10,  trailing_stop=-0.15,  take_profit_1=0.30, take_profit_2=0.60), "A4"),
    (BacktestConfig(absolute_stop=-0.12,  trailing_stop=-0.10,  take_profit_1=0.25, take_profit_2=0.50), "A5"),
    (BacktestConfig(absolute_stop=-0.12,  trailing_stop=-0.18,  take_profit_1=0.25, take_profit_2=0.50), "A6"),
    (BacktestConfig(absolute_stop=-0.15,  trailing_stop=-0.12,  take_profit_1=0.30, take_profit_2=0.60), "A7"),
]

# 默认配置（当前个人策略）
# 2026-07-08 消融测试优化：砍掉入场过滤+过热过滤+追踪止损+绝对止损
# 仅保留 MA10(4天)退出 + 30%/60% 分批止盈，年化 5.7%→9.3%，回撤持平
DEFAULT_CONFIG = BacktestConfig(
    pool_size=60,
    rebalance_freq="biweekly",
    # ── 过热过滤：关闭 (测试证明入场过滤+过热过滤是纯破坏) ──
    max_20d_return=999,
    max_consec_up_days=999,
    max_5d_return=999,
    min_dist_from_high=999,
    enable_entry_filter=False,
    # ── 止损：仅MA10退出 ──
    enable_absolute_stop=False,
    enable_trailing_stop=False,
    enable_ma10_exit=True,
    ma_exit_days=4,              # 连续跌破MA10四天清仓
    # ── 止盈：30%/60%分批 ──
    enable_take_profit=True,
    take_profit_1=0.30,          # 涨30%卖1/3
    take_profit_2=0.60,          # 涨60%再卖1/3
    # ── 仓位 ──
    max_position_pct=0.10,
    max_single=1.0,              # 不设单票硬上限
    # ── 拥挤度过滤: 剔除20日波动率>5%的热门票 ──
    # 2026-08-30 四窗口A/B确认(收益+1.4~5.4pp/夏普全升/回撤全降),
    # 阈值网格4.5-5.5%平滑平台+严格口径(不含当天)复验通过。
    # 针对8/19型失血: 成交额TOP池=动量拥挤, 跌停潮凶手票事前波动率4.4~7.9%
    # vs 防御票1.0~1.7%。vol20_use_today=False=只用T-1及以前。
    max_vol20=5.0,
    vol20_use_today=False,
    # ── 成本 ──
    commission=0.0013,
    cash_yield=0.02,
    min_bars=250,
    # ── 过热模式：不适用(已关) ──
    overheat_mode="eliminate",
    overheat_position_ratio=0.5,
    overheat_stop_tighten=0.5,
)
