"""
代码规范化单一入口 (2026-09-09, 用户批准根因修复)。

三类 bug 同根因（2026-09-08 用户终判）：代码命名空间无交易所限定符——
000001 既是平安银行(SZ)也是上证综指(SH)；399xxx 深证指数族与 SZ 主板
股票代码段重叠；511360 漏 '5' 前缀是同一缺陷的下单侧版本。这些碰撞
不报错、数字看起来合理、能通过所有闸门。

本模块是后缀规则的唯一出处，禁止任何模块自行拼接代码/后缀：
  - normalize_code(): 裸6位 → "000001.SZ" 规范形式（可传 source market
    消歧；92→BJ / 5,6,9→SH / 110,113→SH / 其余→SZ）
  - index 命名空间: 指数数据入库必须用带 "SH"/"SZ" 前缀的键（如
    SH000001），与个股裸键物理隔离（_daily_path 按前缀分目录亦可，
    当前实现=同目录不同文件名）
  - assert_stock_code(): 个股路径硬断言——指数代码/带前缀键进入
    个股路径直接抛错，不静默
  - price_identity_ok(): 价格量级自洽校验——个股收盘>1000 仅白名单
    (茅台类)；指数点位千级不得进入个股文件

涨跌停制度自洽由调用方按板块判断（主板10%/双创20%/北交所30%），
本模块只提供板块判定 board_of()。
"""
from __future__ import annotations

# 既是指数代码段又是个股代码段的歧义码（000xxx 段内同时存在
# 上证/中证指数族与 SZ 主板股票）。这类码的个股/指数身份必须由
# 数据源的 market 字段消歧，禁止靠前缀猜。
AMBIGUOUS_FAMILIES = ("000", "399")

# 沪市股票/基金/转债前缀（指数不算个股，指数用 INDEX_KEYS 键空间）
SH_STOCK_PREFIXES = ("5", "6", "9", "110", "113")
# 北交所: 92 新号段 + 43/83/87 旧号段(2026-09-09 补, 此前 .SZ 误判)
BJ_PREFIXES = ("92", "43", "83", "87")


def normalize_code(code, market: str | None = None) -> str:
    """裸6位代码 → "XXXXXX.EX" 规范形式。已带后缀/市场前缀的原样规范化。
    market: 'SH'/'SZ'/'BJ' 由数据源提供，用于歧义码消歧；无歧义码
    可不传。禁止在调用方自行拼接后缀。"""
    c = str(code).strip().upper()
    if "." in c:
        # 已带后缀: 只规范化大小写
        base, ex = c.split(".", 1)
        return f"{base.zfill(6)}.{ex}"
    # 源格式的市场前缀键(如 sh000001 / SZ399006) → 规范后缀形式
    if len(c) == 8 and c[:2] in ("SH", "SZ", "BJ"):
        return f"{c[2:].zfill(6)}.{c[:2]}"
    c = c.zfill(6)
    if market:
        m = market.upper()
        if m not in ("SH", "SZ", "BJ"):
            raise ValueError(f"非法市场: {market}")
        return f"{c}.{m}"
    if c.startswith(BJ_PREFIXES):
        return f"{c}.BJ"
    if c.startswith(SH_STOCK_PREFIXES):
        return f"{c}.SH"
    return f"{c}.SZ"


def strip_suffix(code) -> str:
    """带后缀/市场前缀键 → 裸6位。"""
    s = str(code).strip().upper()
    if "." in s:
        return s.split(".")[0].zfill(6)
    if len(s) == 8 and s[:2] in ("SH", "SZ", "BJ"):
        return s[2:].zfill(6)
    return s.zfill(6)


def is_ambiguous(code) -> bool:
    """000xxx/399xxx 段 = 个股与指数命名空间重叠，身份必须靠源消歧。"""
    return strip_suffix(code).startswith(AMBIGUOUS_FAMILIES)


def assert_stock_code(code) -> str:
    """个股路径硬断言：拒绝指数键（SH000xxx 等带前缀形式）与 399xxx。
    000xxx 歧义段允许（源 market 已在入库前消歧）。"""
    c = str(code).strip()
    if c.startswith(("SH", "SZ", "BJ")):
        raise ValueError(f"指数键 {c} 进入个股路径（命名空间混用）")
    b = strip_suffix(c)
    if b.startswith("399"):
        raise ValueError(f"399xxx 指数代码 {c} 进入个股路径（命名空间混用）")
    return b


def index_key(index_code, market="SH") -> str:
    """指数数据的存储键：SH000001 / SZ399001 形式，与个股裸键隔离。"""
    return f"{market.upper()}{strip_suffix(index_code)}"


def board_of(code) -> str:
    """板块判定（涨跌停制度自洽校验用）: 主板/创业板/科创板/北交所。"""
    b = strip_suffix(code)
    if b.startswith(BJ_PREFIXES):
        return "北交所"
    if b.startswith("30"):
        return "创业板"
    if b.startswith("68"):
        return "科创板"
    return "主板"


PRICE_WHITELIST = {"600519"}  # 与 storage.py 保持一致（茅台等真千元股）


def price_identity_ok(code, close: float, is_index: bool = False) -> bool:
    """价格量级自洽：个股收盘 >1000 仅白名单（防指数点位混入个股库）；
    指数点位应 ≥100（千点级），低于此视为个股价格混入指数。"""
    if is_index:
        return close >= 100
    b = strip_suffix(code)
    if b in PRICE_WHITELIST:
        return True
    return close <= 1000
