"""
QMT持仓同步 — 从Windows拉取QMT实时持仓,供监测使用
"""
import json, subprocess, sys
from pathlib import Path
from datetime import datetime
from loguru import logger

SSH_KEY = "~/.ssh/id_rsa"
SSH_PORT = 2222
SSH_USER = "Administrator"
SSH_HOST = "127.0.0.1"
WIN_QUANT = "H:/quant"
LOCAL_CACHE = Path("logs/qmt_positions_cache.json")

def pull_qmt_positions() -> dict:
    """从Windows QMT拉取最新持仓,缓存到本地。返回dict或空。

    2026-09-02 修复: 旧版SCP失败时把LOCAL_CACHE(可能几天前的旧快照)当
    新鲜数据返回——隧道断时 reconcile/日报消费过期持仓毫无感知。
    修复: 只有本次拉取成功才返回, 失败返回空并告警(消费方按空处理)。
    """
    try:
        # 1. 让Windows导出最新持仓
        export_cmd = (
            f'powershell -NoProfile -Command '
            f'"cd {WIN_QUANT}; python scripts/export_qmt_positions.py"'
        )
        r1 = subprocess.run(
            ["ssh", "-i", SSH_KEY, "-p", str(SSH_PORT),
             f"{SSH_USER}@{SSH_HOST}", export_cmd],
            capture_output=True, timeout=30
        )
        if r1.returncode != 0:
            logger.warning(f"QMT同步失败(SSH导出): {r1.stderr.strip()[:200]}")
            return {}

        # 2. SCP拉取持仓文件
        r2 = subprocess.run(
            ["scp", "-P", str(SSH_PORT), "-i", SSH_KEY,
             f"{SSH_USER}@{SSH_HOST}:{WIN_QUANT}/logs/qmt_positions_latest.json",
             str(LOCAL_CACHE)],
            capture_output=True, timeout=15
        )
        if r2.returncode != 0:
            logger.warning(f"QMT同步失败(SCP拉取): {r2.stderr.strip()[:200]}")
            return {}

        if LOCAL_CACHE.exists():
            data = json.loads(LOCAL_CACHE.read_text(encoding="utf-8"))
            logger.info(f"QMT持仓同步: {len(data.get('positions',{}))}只")
            return data
    except Exception as e:
        logger.warning(f"QMT同步失败: {e}")
    return {}

def _enrich_positions(positions: dict) -> dict:
    """补全名称和实时盈亏"""
    import sys, os, requests, re
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from data.storage import load_meta

    # Load name map
    try:
        info = load_meta('stock_info_full')
        name_map = {}
        for _, r in info.iterrows():
            name_map[str(r['code'])] = r.get('name','')
    except:
        name_map = {}

    enriched = {}
    for code, pos in positions.items():
        name = pos.get('name', '?')
        if name == '?' or not name:
            name = name_map.get(code, '?')
        cost = pos.get('cost_price', 0) or 0
        shares = pos.get('shares', 0) or 0

        # Fetch real-time price from Sina
        cur = 0
        exch = 'sh' if code.startswith('6') else 'sz'
        try:
            r = requests.get(f'http://hq.sinajs.cn/list={exch}{code}',
                headers={'Referer':'https://finance.sina.com.cn'}, timeout=3)
            cur = float(r.text.split('"')[1].split(',')[3])
        except: pass

        pnl_pct = (cur / cost - 1) if cost > 0 and cur > 0 else (pos.get('pnl_pct', 0) or 0)
        mv = cur * shares if cur > 0 and shares > 0 else (pos.get('market_value', 0) or 0)

        enriched[code] = {
            'name': name, 'cost_price': cost, 'shares': shares,
            'market_value': mv, 'pnl_pct': pnl_pct,
            'cur': cur,
        }
    return enriched


def get_qmt_summary() -> dict:
    """返回QMT账户摘要,供监测邮件使用"""
    data = pull_qmt_positions()
    if not data:
        return {"available": False, "reason": "同步失败"}

    positions = _enrich_positions(data.get("positions", {}))
    account = data.get("account", {})

    total_mv = sum(p.get("market_value", 0) or 0 for p in positions.values())
    total_assets = account.get("total_assets", 0) or 0
    cash = account.get("cash", 0) or 0

    # 分类: 盈利/亏损, 需要关注
    profit_pos = []
    loss_pos = []
    alerts = []

    for code, pos in positions.items():
        pnl = pos.get("pnl_pct", 0) or 0
        mv = pos.get("market_value", 0) or 0
        name = pos.get("name", "?")
        cost = pos.get("cost_price", 0) or 0

        info = {"code": code, "name": name, "mv": mv, "pnl": pnl, "cost": cost,
                "pnl_pct": pnl}

        if pnl > 0:
            profit_pos.append(info)
        else:
            loss_pos.append(info)

        # 止损检查
        if pnl <= -0.12:
            alerts.append(f"    🔴 {code} {name}: 绝对止损{pnl:+.1%}(<-12%), 需立即卖出")
        elif pnl <= -0.08:
            alerts.append(f"    🟡 {code} {name}: 亏损{pnl:+.1%}, 接近止损线")

    profit_pos.sort(key=lambda x: x["pnl"], reverse=True)
    loss_pos.sort(key=lambda x: x["pnl"])

    return {
        "available": True,
        "total_assets": total_assets,
        "total_mv": total_mv,
        "cash": cash,
        "position_count": len(positions),
        "profit_count": len(profit_pos),
        "loss_count": len(loss_pos),
        "top_profit": profit_pos[:3],
        "top_loss": loss_pos[:3],
        "alerts": alerts,
    }

def format_qmt_report(data: dict) -> str:
    """格式化QMT摘要为邮件文本"""
    if not data.get("available"):
        return ""

    lines = [
        "🤖 QMT实盘持仓",
        "─" * 36,
        f"  总资产: ¥{data['total_assets']:,.0f} | 持仓市值: ¥{data['total_mv']:,.0f} | 现金: ¥{data['cash']:,.0f}",
        f"  持仓: {data['position_count']}只 | 盈利: {data['profit_count']}只 | 亏损: {data['loss_count']}只",
    ]

    # 最大盈亏
    if data["top_profit"]:
        lines.append("  📈 最大盈利:")
        for p in data["top_profit"]:
            lines.append(f"    {p['code']} {p['name']}: +{p['pnl']:.1%} (市值¥{p['mv']:,.0f})")

    if data["top_loss"]:
        lines.append("  📉 最大亏损:")
        for p in data["top_loss"]:
            lines.append(f"    {p['code']} {p['name']}: {p['pnl']:.1%} (市值¥{p['mv']:,.0f})")

    # 告警
    if data["alerts"]:
        lines.append("  ⚠️ 止损告警:")
        lines.extend(data["alerts"])

    lines.append("─" * 36)
    return "\n".join(lines)


if __name__ == "__main__":
    data = get_qmt_summary()
    print(format_qmt_report(data))
