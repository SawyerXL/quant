from pathlib import Path
from dotenv import load_dotenv
import os

load_dotenv()

# ---------- 路径 ----------
ROOT = Path(__file__).parent.parent
DATA_STORE = Path(os.getenv("DATA_STORE_ROOT", ROOT / "data_store"))
LOG_DIR = Path(os.getenv("LOG_DIR", ROOT / "logs"))
LOG_DIR.mkdir(exist_ok=True)

# ---------- 数据源选择 ----------
# 切换数据源只改这一行：'akshare' | 'mcp' | 'tushare'
DATA_SOURCE = os.getenv("DATA_SOURCE", "akshare")

MCP_API_KEY  = os.getenv("MCP_API_KEY", "")
MCP_BASE_URL = os.getenv("MCP_BASE_URL", "")
TUSHARE_TOKEN = os.getenv("TUSHARE_TOKEN", "")

# ---------- QMT ----------
QMT_PATH       = os.getenv("QMT_PATH", "C:/QMT/userdata_mini")
QMT_ACCOUNT_ID = os.getenv("QMT_ACCOUNT_ID", "")

# ---------- 企业微信 ----------
WECHAT_WEBHOOK = os.getenv("WECHAT_WEBHOOK_URL", "")

# ---------- 回测基础参数 ----------
BACKTEST_INIT_CAPITAL = 1_000_000
COMMISSION_RATE = 0.00025   # 双向万2.5
STAMP_TAX       = 0.001     # 卖出印花税千1
SLIPPAGE        = 0.002     # 默认滑点千2（强势股用千5）
MIN_LOT         = 100       # A股最小手数

# ---------- 全局风控 ----------
# Track A: 每只约2万，Track B: 每只约5万，上限设10万（防止录入错误的0多打一个）
MAX_SINGLE_ORDER_VALUE   = 100_000   # 单笔订单金额上限（防错单）
MAX_ACCOUNT_DRAWDOWN     = 0.15      # 账户回撤熔断线(2026-09-02 约束引擎网格定案: -15%=约束画像黄金档, 全期触发1次/B伤害仅-0.43pp; 原25%在50万/组约束画像下永不触发)

# ---------- 环境标志 ----------
ENV = os.getenv("ENV", "development")
IS_PROD = ENV in ("production", "simulation")  # 2026-09-02: 仿真环境也走真实下单
# → 告警必须真实推送(原仅production, Windows仿真户ENV=simulation时所有告警被mock,
#   9/2 CB执行日志实锤[Alert-Mock]=团队一直没收到执行告警)
