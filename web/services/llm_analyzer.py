"""LLM-powered market analysis — uses DeepSeek (via Anthropic-compatible API) for natural language reports."""
import json, os, time
from datetime import datetime

# Use same API as Claude Code: DeepSeek Anthropic-compatible endpoint
LLM_API_KEY = os.getenv("ANTHROPIC_AUTH_TOKEN", "")
LLM_BASE_URL = os.getenv("ANTHROPIC_BASE_URL", "https://api.deepseek.com/anthropic")
LLM_MODEL = "claude-sonnet-4-20250514"  # DeepSeek maps this to their model

# Cache
_llm_cache = {"ts": 0, "text": "", "key": ""}
_CACHE_TTL = 600


def _call_llm(prompt: str) -> str:
    """Call LLM via Anthropic SDK (routed to DeepSeek). Returns text or empty."""
    global _llm_cache
    cache_key = str(hash(prompt))
    now = time.time()

    if _llm_cache["text"] and (now - _llm_cache["ts"]) < _CACHE_TTL and _llm_cache["key"] == cache_key:
        return _llm_cache["text"]

    if not LLM_API_KEY:
        return ""

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
        resp = client.messages.create(
            model=LLM_MODEL,
            max_tokens=400,
            temperature=0.3,
            messages=[{"role": "user", "content": prompt}],
        )
        # DeepSeek returns thinking blocks too — extract text from all blocks
        text = ""
        for block in resp.content:
            if hasattr(block, 'text') and block.text:
                text += block.text
        text = text.strip()
        if text:
            _llm_cache = {"ts": now, "text": text, "key": cache_key}
            return text
    except Exception:
        return ""


def generate_market_summary(analysis_data: dict) -> str:
    """Generate a natural language market summary from analysis data."""
    tech = analysis_data.get("technical", {})
    lv = analysis_data.get("key_levels", {})
    ov = analysis_data.get("overseas", {})
    outlook = analysis_data.get("outlook", {})
    recap = analysis_data.get("today_recap", {})
    is_afternoon = analysis_data.get("is_afternoon", False)

    # Build structured data for the prompt
    sh_close = tech.get("sh_close", "?")
    sh_ma20 = tech.get("sh_ma20", "?")
    above_ma20 = tech.get("sh_above_ma20", False)
    days_below = tech.get("days_below_ma200", 0)
    dd_52w = tech.get("dd_52w", 0)
    margin_chg = tech.get("margin_chg_5d", 0)
    margin_days = tech.get("margin_cons_days", 0)

    # Key levels
    supports = lv.get("support", [])
    resistances = lv.get("resistance", [])
    support_str = "、".join(f"{s['label']}{s['level']:.0f}" for s in supports[:2])
    resist_str = "、".join(f"{r['label']}{r['level']:.0f}" for r in resistances[:2])

    # Overnight
    sp_chg = ov.get("sp500_chg")
    nq_chg = ov.get("nasdaq_chg")
    overnight_str = ""
    if sp_chg is not None:
        overnight_str = f"S&P 500 {sp_chg:+.1f}%"
        if nq_chg is not None:
            overnight_str += f"、纳斯达克 {nq_chg:+.1f}%"

    # Today's recap
    today_line = recap.get("today_line", "")

    # Context
    time_label = "盘后" if is_afternoon else "早盘"

    prompt = f"""你是A股市场分析师。根据以下数据，用3-5句话做一个{time_label}市场研判。用中文。

数据：
- 上证收盘：{sh_close}{'（站上MA20 '+str(sh_ma20)+'）' if above_ma20 and sh_ma20 else ''}
- MA200下方：{days_below}天{'（熊市结构）' if days_below >= 20 else ''}
- 52周回撤：{dd_52w:.1f}%
- 融资变化(5日)：{margin_chg:+.1f}%{'（连降'+str(margin_days)+'天）' if margin_days > 0 else '（资金回补）'}
- 支撑：{support_str}
- 阻力：{resist_str}
{f"- 隔夜美股：{overnight_str}" if overnight_str else ""}
{f"- {today_line}" if today_line else ""}
{f"- 预判方向：{outlook.get('direction','?')}" if outlook.get('direction') else ""}

要求：
1. 不使用markdown格式
2. 给出明确的仓位建议（如"40-50%仓位"）
3. 指出最关键的一个风险和一个机会
4. 语气专业但不生硬"""

    result = _call_llm(prompt)
    return result if result else ""


def generate_stock_comment(code: str, name: str, analysis_data: dict) -> str:
    """Generate a brief LLM comment for a single stock."""
    tech = analysis_data.get("technicals", {})
    fin = analysis_data.get("financials", {})
    price_data = analysis_data.get("price", {})
    lite = analysis_data.get("lite", {})

    cur = price_data.get("cur", 0)
    chg = (cur / price_data.get("prev", cur) - 1) * 100 if price_data.get("prev") else 0

    prompt = f"""你是短线交易顾问。这只股票通过量化和流动性筛选，当前处于回调买入区间。请给出操作建议。

"{name}"({code})：
- 现价：{cur:.2f}，今日涨跌：{chg:+.1f}%
- MA20：{tech.get('ma20','?')}，MA60：{tech.get('ma60','?')}
- RSI14：{tech.get('rsi14','?')}
- 20日涨跌：{tech.get('ret20','?')}%
- 距20日高：{lite.get('dh','?')}%（回调幅度）
- PE：{fin.get('pe','?')}，ROE：{fin.get('roe','?')}%

用1-2句话：判断当前回调是否充分、是否接近支撑位、适合什么仓位。语气果断，不要模棱两可的观望。"""

    result = _call_llm(prompt)
    return result if result else ""
