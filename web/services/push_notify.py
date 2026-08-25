"""Push notifications via Feishu webhook for daily market reports and alerts."""
import json, os, requests
from datetime import datetime


FEISHU_WEBHOOK = os.getenv("FEISHU_WEBHOOK_URL", "")


def send_feishu(title: str, content: str, color: str = "blue") -> bool:
    """Send a Feishu interactive card message. Returns True on success."""
    if not FEISHU_WEBHOOK:
        return False

    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": color,
            },
            "elements": [
                {"tag": "markdown", "content": content},
                {"tag": "note", "elements": [
                    {"tag": "plain_text", "content": f"Quant Circle · {datetime.now().strftime('%Y-%m-%d %H:%M')}"}
                ]},
            ],
        },
    }

    try:
        resp = requests.post(FEISHU_WEBHOOK, json=payload, timeout=10)
        return resp.status_code == 200 and resp.json().get("code") == 0
    except Exception:
        return False


def send_text_feishu(text: str) -> bool:
    """Send a simple text message to Feishu."""
    if not FEISHU_WEBHOOK:
        return False
    try:
        resp = requests.post(FEISHU_WEBHOOK, json={
            "msg_type": "text",
            "content": {"text": text},
        }, timeout=10)
        return resp.status_code == 200 and resp.json().get("code") == 0
    except Exception:
        return False


def push_daily_market_report() -> bool:
    """Push the daily market analysis report to Feishu."""
    if not FEISHU_WEBHOOK:
        return False

    try:
        from web.services.market_analysis import get_market_analysis
        from web.services.llm_analyzer import generate_market_summary

        analysis = get_market_analysis(force=True)
        out = analysis.get("outlook", {})
        tech = analysis.get("technical", {})

        # Build report content
        lines = []
        lines.append(f"**上证**: {tech.get('sh_close', '?')} | MA200下方{tech.get('days_below_ma200', 0)}天 | 回撤{tech.get('dd_52w', 0):.1f}%")
        lines.append(f"**融资**: {tech.get('margin_chg_5d', 0):+.1f}%(5日)")
        lines.append(f"**方向**: {out.get('direction', '?')}")
        if out.get('suggestion'):
            lines.append(f"**建议**: {out['suggestion']}")

        # LLM summary
        llm = generate_market_summary(analysis)
        if llm:
            lines.append(f"\n{llm}")

        lines.append(f"\n📊 [查看完整分析](http://106.15.61.81:8000)")

        return send_feishu(
            title=f"📊 每日市场分析 {analysis.get('date', '')}",
            content="\n".join(lines),
            color="blue",
        )
    except Exception:
        return False


def push_alert(level: str, title: str, message: str) -> bool:
    """Push an alert to Feishu (danger/warning/info)."""
    if not FEISHU_WEBHOOK:
        return False
    color_map = {"danger": "red", "warning": "yellow", "info": "blue"}
    return send_feishu(
        title=f"{'🔴' if level == 'danger' else '🟡' if level == 'warning' else '🔵'} {title}",
        content=message,
        color=color_map.get(level, "blue"),
    )
