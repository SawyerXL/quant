"""
多通道告警：邮件 + 企业微信（可选）。
"""
import os
import smtplib
import httpx
from email.mime.text import MIMEText
from email.header import Header
from datetime import datetime
from loguru import logger
from config.settings import WECHAT_WEBHOOK, IS_PROD

SMTP_SERVER   = os.getenv("SMTP_SERVER",   "smtp.yeah.net")
SMTP_PORT     = int(os.getenv("SMTP_PORT",  "465"))
SMTP_USER     = os.getenv("SMTP_USER",     "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
ALERT_EMAIL   = os.getenv("ALERT_EMAIL",   SMTP_USER)


def _send_email(subject: str, body: str) -> bool:
    """通过 SMTP 发送邮件告警。"""
    if not SMTP_USER or not SMTP_PASSWORD:
        logger.debug("SMTP 未配置，跳过邮件推送")
        return False
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["From"]    = SMTP_USER
        msg["To"]      = ALERT_EMAIL
        msg["Subject"] = Header(subject, "utf-8")
        msg["Date"]    = datetime.now().strftime("%a, %d %b %Y %H:%M:%S +0800")

        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=10) as s:
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.sendmail(SMTP_USER, [ALERT_EMAIL], msg.as_string())
        logger.info(f"邮件已发送: {subject}")
        return True
    except Exception as e:
        logger.error(f"邮件发送失败: {e}")
        return False


def send_alert(content: str, level: str = "info") -> bool:
    """
    多渠道告警。邮件为主，企业微信为辅助。
    level: 'info' | 'warning' | 'error'
    非生产环境只打日志，不实际推送。
    """
    prefix = {"info": "", "warning": "⚠️", "error": "🔴"}.get(level, "")
    message = f"{prefix} {content}"

    if not IS_PROD:
        logger.info(f"[Alert-Mock] {message}")
        return True

    sent = _send_email(f"[量化{level}] {content[:40]}...", message)

    # 企业微信作为辅助通道
    if WECHAT_WEBHOOK:
        try:
            payload = {"msgtype": "text", "text": {"content": message}}
            resp = httpx.post(WECHAT_WEBHOOK, json=payload, timeout=5)
            if resp.status_code != 200 or resp.json().get("errcode") != 0:
                logger.warning(f"企微推送失败: {resp.text}")
        except Exception as e:
            logger.warning(f"企微推送异常: {e}")

    return sent


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
