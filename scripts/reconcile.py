"""
持仓对账脚本：比较 QMT 实际持仓 vs 信号文件目标持仓。

用法：
    python scripts/reconcile.py              # 对账 Track A（优先读离线持仓文件）
    python scripts/reconcile.py --track b   # 对账 Track B
    python scripts/reconcile.py --dry       # 只读，不写任何文件

离线对账（Linux上运行）：
    1. 在 Windows 运行: python scripts/export_qmt_positions.py
       → 自动推送 logs/qmt_positions_latest.json 到 Linux
    2. 在 Linux 运行: python scripts/reconcile.py
       → 自动读取 logs/qmt_positions_latest.json 进行对账
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from loguru import logger
from monitoring.alerts import send_alert

SIGNAL_A    = Path("data_store/meta/signal_a_latest.json")
SIGNAL_B    = Path("data_store/meta/signal_b_latest.json")
QMT_POS_FILE = Path("logs/qmt_positions_latest.json")


def load_signal(track: str) -> dict:
    f = SIGNAL_A if track == "a" else SIGNAL_B
    if not f.exists():
        logger.error(f"信号文件不存在: {f}")
        return {}
    return json.loads(f.read_text(encoding="utf-8"))


def load_actual_positions() -> tuple[dict, dict, str]:
    """
    获取 QMT 实际持仓。优先顺序：
    1. 若 xtquant 可用（Windows），直接查询 QMT
    2. 否则读取 logs/qmt_positions_latest.json（由 export_qmt_positions.py 推送）
    返回 (actual_pos, account_info, source_desc)
    """
    from execution.qmt_client import QMT_AVAILABLE

    if QMT_AVAILABLE:
        import os as _os
        _os.environ.setdefault("ENV", "simulation")
        from execution.qmt_client import get_client
        client = get_client()
        raw_pos = client.get_positions()
        # QMT返回代码含交易所后缀（如600816.SH），统一去掉
        clean_pos = {code.split(".")[0]: v for code, v in raw_pos.items()}
        return (clean_pos, client.get_account_info(), "QMT直连")

    # 离线模式：读取 Windows 端推送的持仓快照
    if QMT_POS_FILE.exists():
        data    = json.loads(QMT_POS_FILE.read_text(encoding="utf-8"))
        exported_at = data.get("exported_at", "?")
        # 检查文件是否过期（超过2天则警告）
        try:
            exp_dt   = datetime.fromisoformat(exported_at)
            age_hrs  = (datetime.now() - exp_dt).total_seconds() / 3600
            age_warn = f" ⚠️ 数据已 {age_hrs:.0f}小时前，建议重新导出" if age_hrs > 48 else ""
        except Exception:
            age_warn = ""

        pos_raw = data.get("positions", {})
        account = data.get("account", {})
        # 统一去掉交易所后缀
        actual_pos = {
            code.split(".")[0]: v for code, v in pos_raw.items()
            if v.get("volume", 0) > 0
        }
        return (actual_pos, account, f"离线快照({exported_at}{age_warn})")

    return ({}, {"total_assets": 0, "cash": 0, "market_value": 0},
            "❌ 无持仓数据（请在Windows运行 export_qmt_positions.py）")


def reconcile(track: str = "a") -> dict:
    """
    对比 QMT 实际持仓 vs 信号目标持仓，输出差异报告。
    返回 {"ok": bool, "missing": [...], "extra": [...], "diff_shares": {...}}
    """
    try:
        actual_pos, account, source = load_actual_positions()
    except Exception as e:
        logger.error(f"获取持仓失败: {e}")
        return {"ok": False, "error": str(e)}

    # 信号目标持仓
    sig = load_signal(track)
    if not sig:
        return {"ok": False, "error": "信号文件读取失败"}

    target_holdings = set(sig.get("holdings", []))
    target_shares   = sig.get("shares", {})
    actual_holdings = set(actual_pos.keys())

    missing = sorted(target_holdings - actual_holdings)   # 信号要持仓，但QMT没有
    extra   = sorted(actual_holdings - target_holdings)   # QMT有持仓，但信号没要求
    diff_shares = {}
    for code in target_holdings & actual_holdings:
        t = target_shares.get(code, 0)
        a = actual_pos[code].get("volume", 0)
        if abs(t - a) > 0:
            diff_shares[code] = {"target": t, "actual": a, "diff": a - t}

    ok = (len(missing) == 0 and len(extra) == 0 and len(diff_shares) == 0)

    # 账户摘要
    total  = account.get("total_assets", 0)
    cash   = account.get("cash", 0)
    mktval = account.get("market_value", 0)

    logger.info("=" * 58)
    logger.info(f"Track {track.upper()} 持仓对账 | 信号:{sig.get('signal_date','?')} | 数据源:{source}")
    logger.info("=" * 58)
    logger.info(f"账户总资产: {total:,.0f} 元  现金: {cash:,.0f} 元  持仓市值: {mktval:,.0f} 元")
    logger.info(f"目标持仓: {len(target_holdings)} 只  实际持仓: {len(actual_holdings)} 只")

    if ok:
        logger.info("✅ 持仓完全一致，对账通过")
    else:
        if missing:
            logger.warning(f"⚠️  应持有但QMT没有 ({len(missing)} 只): {missing}")
        if extra:
            logger.warning(f"⚠️  QMT多余持仓 ({len(extra)} 只): {extra}")
        if diff_shares:
            logger.warning(f"⚠️  手数不一致 ({len(diff_shares)} 只):")
            for code, d in diff_shares.items():
                logger.warning(f"    {code}: 目标{d['target']}股 / 实际{d['actual']}股 "
                               f"(差{d['diff']:+d}股)")

    # ── 成交价偏差分析（信号假设价 vs QMT实际成本价）──────────
    assumed_prices = sig.get("prices", {})
    buy_codes      = set(sig.get("buy", []))
    slippage_rows  = []
    for code in buy_codes & actual_holdings:
        assumed = assumed_prices.get(code)
        actual_cost = actual_pos[code].get("cost_price")
        if assumed and actual_cost and assumed > 0:
            slip = (actual_cost - assumed) / assumed * 100
            slippage_rows.append((code, assumed, actual_cost, slip))

    if slippage_rows:
        logger.info(f"\n── 本次买入成交价偏差（信号假设=T-1收盘） ──")
        for code, assumed, actual_cost, slip in sorted(slippage_rows, key=lambda x: abs(x[3]), reverse=True):
            flag = " ⚠️" if abs(slip) > 0.3 else ""
            logger.info(f"  {code}: 假设{assumed:.2f} → 实际{actual_cost:.2f}  偏差{slip:+.3f}%{flag}")
        avg_slip = sum(r[3] for r in slippage_rows) / len(slippage_rows)
        logger.info(f"  平均偏差: {avg_slip:+.3f}%  （回测成本假设: 0.175% 单边）")

    result = {
        "ok":          ok,
        "signal_date": sig.get("signal_date"),
        "track":       track,
        "source":      source,
        "missing":     missing,
        "extra":       extra,
        "diff_shares": diff_shares,
        "account":     {"total": total, "cash": cash, "market_value": mktval},
        "slippage":    [{"code": c, "assumed": a, "actual": ac, "pct": s}
                        for c, a, ac, s in slippage_rows],
    }

    if not ok:
        send_alert(
            f"【对账差异】Track {track.upper()} {sig.get('signal_date')}\n"
            f"应持未持: {missing[:5]}\n"
            f"多余持仓: {extra[:5]}\n"
            f"手数差异: {list(diff_shares.keys())[:5]}"
        )

    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--track", default="a", choices=["a", "b"])
    parser.add_argument("--dry",   action="store_true", help="只读模式（不写文件）")
    args = parser.parse_args()

    result = reconcile(args.track)
    if result.get("ok"):
        print("\n✅ 对账通过，持仓与信号一致")
    else:
        print(f"\n⚠️  对账发现差异，请检查日志")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
