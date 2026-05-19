"""
持仓对账脚本：比较 QMT 实际持仓 vs 信号文件目标持仓。
在每次执行后运行，确认下单是否符合预期。

用法：
    python scripts/reconcile.py              # 对账 Track A
    python scripts/reconcile.py --track b   # 对账 Track B
    python scripts/reconcile.py --dry       # 只读，不写任何文件
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from loguru import logger
from monitoring.alerts import send_alert

SIGNAL_A = Path("data_store/meta/signal_a_latest.json")
SIGNAL_B = Path("data_store/meta/signal_b_latest.json")


def load_signal(track: str) -> dict:
    f = SIGNAL_A if track == "a" else SIGNAL_B
    if not f.exists():
        logger.error(f"信号文件不存在: {f}")
        return {}
    return json.loads(f.read_text(encoding="utf-8"))


def reconcile(track: str = "a") -> dict:
    """
    对比 QMT 实际持仓 vs 信号目标持仓，输出差异报告。
    返回 {"ok": bool, "missing": [...], "extra": [...], "diff_shares": {...}}
    """
    from execution.qmt_client import get_client
    client = get_client()

    # QMT 实际持仓
    try:
        actual_pos = client.get_positions()   # {code: {volume, cost_price, market_value}}
        account    = client.get_account_info()
    except Exception as e:
        logger.error(f"QMT 连接失败: {e}")
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

    logger.info("=" * 55)
    logger.info(f"Track {track.upper()} 持仓对账 | {sig.get('signal_date', '?')}")
    logger.info("=" * 55)
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

    result = {
        "ok":          ok,
        "signal_date": sig.get("signal_date"),
        "track":       track,
        "missing":     missing,
        "extra":       extra,
        "diff_shares": diff_shares,
        "account":     {"total": total, "cash": cash, "market_value": mktval},
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
