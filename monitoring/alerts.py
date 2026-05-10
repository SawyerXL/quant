"""
企业微信机器人告警。
所有告警统一从这里发出，便于后续扩展其他渠道（短信/电话）。
"""
import httpx
from loguru import logger
from config.settings import WECHAT_WEBHOOK, IS_PROD


def send_alert(content: str, level: str = "info") -> bool:
    """
    推送企业微信告警。
    level: 'info' | 'warning' | 'error'
    非生产环境只打日志，不实际推送。
    """
    prefix = {"info": "ℹ️", "warning": "⚠️", "error": "🔴"}.get(level, "")
    message = f"{prefix} {content}"

    if not IS_PROD:
        logger.info(f"[Alert-Mock] {message}")
        return True

    if not WECHAT_WEBHOOK:
        logger.warning("WECHAT_WEBHOOK_URL 未配置，告警未推送")
        return False

    payload = {
        "msgtype": "text",
        "text": {"content": message},
    }
    try:
        resp = httpx.post(WECHAT_WEBHOOK, json=payload, timeout=5)
        ok = resp.status_code == 200 and resp.json().get("errcode") == 0
        if not ok:
            logger.error(f"企业微信推送失败: {resp.text}")
        return ok
    except Exception as e:
        logger.error(f"企业微信推送异常: {e}")
        return False


def send_daily_report(strategy_id: str, stats: dict) -> None:
    """推送每日收益报告。"""
    content = (
        f"【{strategy_id} 日报】\n"
        f"当日收益: {stats.get('daily_return', 0):.2%}\n"
        f"持仓市值: {stats.get('market_value', 0):,.0f}\n"
        f"可用现金: {stats.get('cash', 0):,.0f}\n"
        f"累计收益: {stats.get('total_return', 0):.2%}\n"
        f"当前回撤: {stats.get('current_drawdown', 0):.2%}"
    )
    send_alert(content, level="info")


def send_risk_alert(reason: str, details: dict = None) -> None:
    """推送风控告警（高优先级）。"""
    content = f"【风控告警】{reason}"
    if details:
        content += "\n" + "\n".join(f"  {k}: {v}" for k, v in details.items())
    send_alert(content, level="error")
