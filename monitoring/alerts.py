"""
多通道告警：邮件 + 企业微信（可选）。
"""
import json
import os
import smtplib
import httpx
from email.mime.text import MIMEText
from email.header import Header
from datetime import datetime
from pathlib import Path
from loguru import logger
from config.settings import WECHAT_WEBHOOK, IS_PROD

SMTP_SERVER   = os.getenv("SMTP_SERVER",   "smtp.yeah.net")
SMTP_PORT     = int(os.getenv("SMTP_PORT",  "465"))
SMTP_USER     = os.getenv("SMTP_USER",     "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
ALERT_EMAIL   = os.getenv("ALERT_EMAIL",   SMTP_USER)

# P0 告警状态文件: 用本文件位置锚定(调用方 cwd 各不相同, 相对路径会再犯
# tunnel_watchdog 的 /root/scripts 错位类 bug)
_P0_STATE_FILE = Path(__file__).parent.parent / "logs" / "p0_alert_state.json"
# 升级阈值: 连续未确认天数(2026-09-06 用户定: P0 连续两天未确认→升级重发)
P0_ESCALATE_DAYS = 2


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


# ── P0 告警分级(2026-09-06): 数据缺口/隧道断/质量异常/执行失败 = 必须响应;
# 其余告警降级或聚合日报。P0 登记 open 状态, scripts/p0_alerts.py --sweep
# 对连续未确认的 P0 升级重发(静跑期=校准告警灵敏度的窗口, 带钱跑前校准完)。
def _p0_load() -> dict:
    if _P0_STATE_FILE.exists():
        try:
            return json.loads(_P0_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _p0_save(state: dict):
    _P0_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _P0_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                              encoding="utf-8")


def p0_alert(p0_type: str, detail: str = "") -> bool:
    """P0 告警: 即时 error 邮件/企微 + 登记 open 状态(供升级清扫)。"""
    now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sent = send_alert(f"[P0] {p0_type}" + (f": {detail}" if detail else ""),
                      level="error")
    state = _p0_load()
    entry = state.get(p0_type, {"first_seen": now_s})
    entry["last_seen"] = now_s
    entry["detail"] = detail
    state[p0_type] = entry
    _p0_save(state)
    return sent


def p0_ack(p0_type: str | None = None) -> int:
    """确认(解除) P0: 恢复/人工确认后调用。None=全部。返回清除条数。"""
    state = _p0_load()
    if p0_type is None:
        n = len(state)
        _p0_save({})
        return n
    if p0_type in state:
        del state[p0_type]
        _p0_save(state)
        return 1
    return 0


def p0_list() -> dict:
    return _p0_load()


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
