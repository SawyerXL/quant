"""
牛转熊紧急监控 — 每个交易日盘后运行
触发条件: CSI800/MA200<0.95, 60日回撤>15%, 或MA死叉
触发后: 连发12封邮件告警
用法: python scripts/check_regime_change.py
Cron: 0 15 * * 1-5 (收盘后立即运行)
"""
import sys, json, pandas as pd, numpy as np
from pathlib import Path
from datetime import date, datetime
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent))
logger.add("logs/regime_check.log", rotation="30 days", retention="180 days")

STATE_FILE = Path("logs/regime_state.json")
ALERT_COUNT_FILE = Path("logs/regime_alert_count.json")

# ═══════════════════════════════════════════════════
# 牛转熊判定标准 (任一触发即告警)
# ═══════════════════════════════════════════════════
BEAR_TRIGGERS = {
    "csi800_below_ma200_095": {
        "check": lambda d: d["ratio"] < 0.95,
        "msg": "🔴 CSI800跌破MA200×0.95 — 确认进入熊市",
        "level": "critical",
    },
    "csi800_below_ma200": {
        "check": lambda d: d["ratio"] < 1.0,
        "msg": "🟡 CSI800跌破MA200 — 牛熊分界线失守",
        "level": "warning",
    },
    "drawdown_60d_15pct": {
        "check": lambda d: d["dd_60d"] < -15,
        "msg": "🔴 60日回撤超过15% — 深度调整",
        "level": "critical",
    },
    "ma20_below_ma60": {
        "check": lambda d: d["ma20"] < d["ma60"],
        "msg": "🟡 MA20下穿MA60 — 中期均线死叉",
        "level": "warning",
    },
    "ret20_below_10pct": {
        "check": lambda d: d["ret20"] < -10,
        "msg": "🔴 20日跌幅超过10% — 急跌模式",
        "level": "critical",
    },
    # 创业板MA60 — 科技股领先指标, 比CSI800更敏感
    "cy_break_ma60": {
        "check": lambda d: d.get("cy_ma60_ratio", 99) < 1.0,
        "msg": "🟡 创业板跌破MA60 — 科技股中期走弱, 领先预警",
        "level": "warning",
    },
    "cy_break_ma60_deep": {
        "check": lambda d: d.get("cy_ma60_ratio", 99) < 0.95,
        "msg": "🔴 创业板跌破MA60×0.95 — 科技股深度破位, 可能引领全市场转熊",
        "level": "critical",
    },
}

def get_market_data():
    """获取CSI800+创业板技术指标。CSI800用沪深300代理(CSI800本地数据延迟严重)"""
    from data.storage import load_meta, load_daily

    # ═══ CSI800作为主基准 (000906官方指数, 每日更新) ═══
    df = load_daily('000906', '2025-01-01', '2026-07-07')
    if df.empty:
        return None

    df['date'] = pd.to_datetime(df['date'])
    df = df.set_index('date').sort_index()
    close = pd.to_numeric(df['close'], errors='coerce').dropna()

    if len(close) < 200:
        return None

    latest = close.iloc[-1]
    ma20 = close.iloc[-20:].mean()
    ma60 = close.iloc[-60:].mean()
    ma200 = close.iloc[-200:].mean()

    # 也加载CSI800做参考(用于回测等)
    try:
        idx = load_meta('csi800_index')
        idx['date'] = pd.to_datetime(idx['date'])
        idx = idx.set_index('date').sort_index()
        csi_close = pd.to_numeric(idx['close'], errors='coerce').dropna()
        csi_latest = csi_close.iloc[-1] if len(csi_close) > 0 else None
    except:
        csi_latest = None

    data = {
        "date": close.index[-1].strftime("%Y-%m-%d"),
        "close": float(latest),
        "ma20": float(ma20),
        "ma60": float(ma60),
        "ma200": float(ma200),
        "ratio": float(latest / ma200),
        "ret5": float((close.iloc[-1] / close.iloc[-6] - 1) * 100) if len(close) >= 6 else 0,
        "ret20": float((close.iloc[-1] / close.iloc[-21] - 1) * 100) if len(close) >= 21 else 0,
        "ret60": float((close.iloc[-1] / close.iloc[-61] - 1) * 100) if len(close) >= 61 else 0,
        "dd_60d": float((latest / close.iloc[-60:].max() - 1) * 100),
        "benchmark": "CSI800(000906)",
        "csi800_latest": float(csi_latest) if csi_latest else None,
    }

    # 创业板独立检查 (领先指标, 科技股代表)
    try:
        cy = load_daily('SZ399006', '2026-03-01', '2026-07-07')
        if not cy.empty:
            cy['date'] = pd.to_datetime(cy['date'])
            cy = cy.set_index('date').sort_index()
            cy_cl = pd.to_numeric(cy['close'], errors='coerce').dropna()
            if len(cy_cl) >= 60:
                cy_ma60 = cy_cl.iloc[-60:].mean()
                data["cy_close"] = float(cy_cl.iloc[-1])
                data["cy_ma60"] = float(cy_ma60)
                data["cy_ma60_ratio"] = float(cy_cl.iloc[-1] / cy_ma60)
                data["cy_ret5"] = float((cy_cl.iloc[-1] / cy_cl.iloc[-6] - 1) * 100) if len(cy_cl) >= 6 else 0
    except Exception:
        pass

    return data

def send_alert_burst(subject, body, count=12):
    """连发N封邮件确保用户看到"""
    from monitoring.alerts import _send_email
    import time

    for i in range(count):
        seq = f"[{i+1}/{count}]"
        full_subject = f"{seq} {subject}"
        full_body = f"{body}\n\n---\n告警序列: {i+1}/{count}\n时间: {datetime.now()}"
        try:
            _send_email(full_subject, full_body)
            logger.info(f"告警邮件 {i+1}/{count} 已发送")
        except Exception as e:
            logger.error(f"邮件 {i+1} 失败: {e}")
        if i < count - 1:
            import time as _t
            _t.sleep(2)  # 间隔2秒防限流

def run():
    today_str = date.today().strftime("%Y-%m-%d")
    d = get_market_data()

    if d is None:
        logger.error("无法获取CSI800数据")
        return

    # 检查触发条件
    triggered = []
    for name, cfg in BEAR_TRIGGERS.items():
        if cfg["check"](d):
            triggered.append({"name": name, "msg": cfg["msg"], "level": cfg["level"]})

    # 计算综合风险等级
    criticals = [t for t in triggered if t["level"] == "critical"]
    warnings = [t for t in triggered if t["level"] == "warning"]

    # 读取上次状态
    prev_state = {}
    if STATE_FILE.exists():
        prev_state = json.loads(STATE_FILE.read_text())

    # 保存当前状态
    state = {
        "date": today_str,
        "ratio": round(d["ratio"], 4),
        "close": round(d["close"], 0),
        "ma200": round(d["ma200"], 0),
        "dd_60d": round(d["dd_60d"], 1),
        "ret20": round(d["ret20"], 1),
        "triggered": [t["name"] for t in triggered],
        "level": "critical" if criticals else ("warning" if warnings else "normal"),
    }
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))

    # 判断是否需要告警
    prev_level = prev_state.get("level", "normal")
    new_level = state["level"]

    # 只在恶化时告警 (normal→warning→critical)
    level_order = {"normal": 0, "warning": 1, "critical": 2}

    # 每天都记录但只在级别升级或维持critical时告警
    should_alert = False
    alert_reason = ""
    is_critical = False

    if level_order.get(new_level, 0) > level_order.get(prev_level, 0):
        should_alert = True
        alert_reason = f"风险升级: {prev_level} → {new_level}"
        is_critical = (new_level == "critical")
    elif new_level == "critical":
        should_alert = True
        alert_reason = "critical持续"
        is_critical = True

    # 打印当前状态
    print(f"\n{'='*55}")
    print(f"  牛转熊监控 {today_str}")
    print(f"{'='*55}")
    print(f"  CSI800: {d['close']:.0f} | MA200: {d['ma200']:.0f} | 比率: {d['ratio']:.3f}")
    print(f"  60日回撤: {d['dd_60d']:.1f}% | 20日收益: {d['ret20']:.1f}%")
    if d.get('cy_close'):
        cy_r = d.get('cy_ma60_ratio', 0)
        icon = '🟢' if cy_r > 1.02 else ('🟡' if cy_r > 1.0 else '🔴')
        print(f"  创业板: {d['cy_close']:.0f} | MA60: {d['cy_ma60']:.0f} | 比率: {d['cy_ma60_ratio']:.3f} {icon}")
    # 仓位建议 (与策略get_position_ratio一致)
    r = d["ratio"]
    if r >= 1.05:     pos_advice = "100%"
    elif r >= 1.02:   pos_advice = "85%"
    elif r >= 0.98:   pos_advice = "70%"
    elif r >= 0.95:   pos_advice = "50%"
    else:             pos_advice = "30%"
    print(f"  建议仓位: {pos_advice} (CSI800/MA200={d['ratio']:.3f})")
    print(f"  风险等级: {new_level.upper()}")

    if triggered:
        print(f"\n  ⚠️ 触发信号:")
        for t in triggered:
            print(f"    [{t['level']}] {t['msg']}")

    if should_alert:
        if is_critical:
            # CRITICAL: 3封紧急邮件
            print(f"\n  🚨 {alert_reason} — 发送3封紧急告警")
            body_lines = [
                f"🔴 牛转熊确认 — {today_str}",
                f"",
                f"CSI800: {d['close']:.0f} / MA200: {d['ma200']:.0f} = {d['ratio']:.3f}",
                f"60日回撤: {d['dd_60d']:.1f}% | 20日收益: {d['ret20']:.1f}%",
            ]
            if d.get('cy_close'):
                body_lines.append(f"创业板: {d['cy_close']:.0f} / MA60: {d['cy_ma60']:.0f} = {d['cy_ma60_ratio']:.3f}")
            body_lines += [
                f"",
                f"策略仓位: {pos_advice}",
                f"",
                f"触发信号:",
            ]
            for t in triggered:
                body_lines.append(f"  [{t['level']}] {t['msg']}")
            body_lines += [
                "",
                "⚠️ 请按策略规则调整仓位",
            ]
            body = "\n".join(body_lines)
            send_alert_burst(body, count=3)
        else:
            # WARNING: 1封事实性邮件, 不吓人
            print(f"\n  ⚠️ {alert_reason} — 发送1封预警邮件")
            from monitoring.alerts import send_alert
            lines = [
                f"市场预警 {today_str}",
                f"",
                f"CSI800: {d['close']:.0f} / MA200: {d['ma200']:.0f} = {d['ratio']:.3f} → 仓位 {pos_advice}",
                f"60日回撤: {d['dd_60d']:.1f}% | 20日: {d['ret20']:+.1f}%",
            ]
            if d.get('cy_close'):
                cy_status = "在上方" if d['cy_ma60_ratio'] > 1.0 else "跌破"
                lines.append(f"创业板: {d['cy_close']:.0f} (MA60: {d['cy_ma60']:.0f}) → {cy_status}MA60")
            lines += [
                f"",
                f"触发:",
            ]
            for t in triggered:
                lines.append(f"  {t['msg']}")
            lines += [
                "",
                f"当前仓位策略仍为 {pos_advice}（由CSI800/MA200决定）",
                f"创业板MA60是领先预警，不是仓位决策依据",
                f"无需立即操作，保持观察",
                f"",
                f"下一步关注:",
            ]
            if d.get('cy_ma60_ratio', 1) < 1.0:
                lines.append(f"  · 创业板能否3日内收复MA60({d.get('cy_ma60',0):.0f})")
            lines.append(f"  · CSI800是否跌破MA200({d['ma200']:.0f})")
            lines.append(f"  · 60日回撤是否超过15%")
            body = "\n".join(lines)
            send_alert(body)
    else:
        print(f"\n  ✅ 无需告警 — 风险等级未升级")

    print(f"{'='*55}\n")

    # 日常摘要
    if not should_alert:
        from monitoring.alerts import send_alert
        pos = "100%" if d['ratio'] >= 1.05 else ("70%" if d['ratio'] >= 1.0 else "30%")
        summary = (
            f"市场状态 {today_str}\n"
            f"CSI800: {d['close']:.0f} / MA200: {d['ma200']:.0f} = {d['ratio']:.3f} → 仓位{pos}\n"
            f"60日回撤: {d['dd_60d']:.1f}% | 20日: {d['ret20']:+.1f}%\n"
        )
        if d.get('cy_close'):
            summary += f"创业板: {d['cy_close']:.0f} / MA60: {d['cy_ma60']:.0f}\n"
        summary += f"风险: {new_level}"
        send_alert(summary)
        logger.info("日报已发送")

if __name__ == '__main__':
    run()
