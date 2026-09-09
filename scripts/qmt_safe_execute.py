"""
QMT safe execute - BUY ONLY, no auto-sell
用法: python scripts/qmt_safe_execute.py  (only on Windows)
"""
import sys, os, json, time, requests
from pathlib import Path
from datetime import date

ROOT = Path(r"H:\quant")
sys.path.insert(0, str(ROOT))
os.chdir(str(ROOT))
from dotenv import load_dotenv; load_dotenv(ROOT / ".env")
from loguru import logger

logger.add(str(ROOT / "logs/qmt_safe_execute.log"), rotation="7 days")

SIGNAL_PATH = ROOT / "data_store/meta/signal_a_latest.json"

def rt_price(code):
    exch = 'sh' if code.startswith(('6','68')) else 'sz'
    try:
        r = requests.get(f'http://hq.sinajs.cn/list={exch}{code}',
            headers={'Referer':'https://finance.sina.com.cn'}, timeout=5)
        return float(r.text.split('"')[1].split(',')[3])
    except:
        return None

def sync_signal_from_linux():
    """从Linux同步最新信号文件 (端口2222)"""
    import subprocess
    linux_host = os.getenv("LINUX_SERVER")
if not linux_host:
    # 2026-09-09: 删除旧IP硬编码回退(6/29全靠手动根因模式)——
    # 环境缺失即报错, 禁止静默指向退役服务器
    raise RuntimeError("LINUX_SERVER 未设置, 拒绝执行")
    ssh_key = os.getenv("SSH_KEY", "")

    cmd = ["scp", "-P", "2222", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10"]
    if ssh_key:
        cmd += ["-i", ssh_key]
    cmd += [
        f"root@{linux_host}:/root/quant/data_store/meta/signal_a_latest.json",
        str(SIGNAL_PATH)
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        logger.error(f"信号同步失败: {result.stderr}")
        return False
    logger.info("信号同步成功")
    return True

def main():
    logger.info("=" * 50)
    logger.info("QMT 安全执行开始")
    logger.info("=" * 50)

    # ═══ Step 1: 加载信号 (由Linux端推送, 此处不拉取) ═══
    sig = json.loads(SIGNAL_PATH.read_text(encoding="utf-8"))
    sig_date = sig.get("signal_date", "")
    today_str = date.today().strftime("%Y-%m-%d")

    if sig_date != today_str:
        print(f"ERROR: 信号日期{sig_date} != 今天{today_str}, 终止执行")
        return

    target_holdings = set(sig["holdings"])
    target_shares = sig.get("shares", {})
    explicit_sells = set(sig.get("sell", []))  # 只卖信号明确要求卖的

    print(f"信号: {sig_date} | 仓位{sig['position_ratio']:.0%} | {len(target_holdings)}只")
    print(f"Explicit sell: {sorted(explicit_sells)}")

    # ═══ Step 3: 连接QMT + 清旧单 ═══
    from execution.qmt_client import get_client
    c = get_client()

    # Cancel ALL pending orders first
    orders = c.get_today_orders()
    pending = [o for o in orders if o.get('status') not in (56, 57)]
    if pending:
        print(f"清理{len(pending)}笔旧委托...")
        for o in pending:
            c.cancel_order(o['order_id'])
        time.sleep(2)

    # ═══ Step 4: 获取QMT实际持仓 ═══
    raw = c.get_positions()
    held = {}
    for x, v in raw.items():
        code = x.split(".")[0]
        vol = v.get("volume", 0)
        if vol > 0:
            held[code] = vol

    acc = c.get_account_info()
    print(f"\nQMT: {len(held)} positions | MktVal CN${acc.get('market_value',0):,.0f}")

    # ═══ Step 5: 只卖信号明确要求卖的 ═══
    to_sell = {c for c in explicit_sells if c in held}
    if to_sell:
        print(f"\nSELL (signal): {sorted(to_sell)}")
        for code in sorted(to_sell):
            vol = held[code]
            p = rt_price(code)
            if p and vol > 0:
                oid = c.place_order(code, "sell", vol, p)
                print(f"  SELL {code} {vol} shares @{p:.2f} oid={oid}")
        time.sleep(2)
    else:
        print(f"\nSELL: none")

    # ═══ Step 6: BUY only missing (NEVER auto-sell) ═══
    to_buy = {c for c in target_holdings if c not in held}
    # Also check share amounts
    to_adjust = {}
    for code in target_holdings & set(held.keys()):
        target = target_shares.get(code, 0)
        current = held.get(code, 0)
        if target > current:
            to_adjust[code] = target - current

    if to_buy or to_adjust:
        print(f"\nBUY: {len(to_buy)} new + {len(to_adjust)} adjust")
        for code in sorted(to_buy):
            shares = target_shares.get(code, 100)
            p = rt_price(code)
            if p and shares > 0:
                oid = c.place_order(code, "buy", shares, p)
                print(f"  BUY {code} {shares} @{p:.2f} oid={oid}")
        for code, need in sorted(to_adjust.items()):
            p = rt_price(code)
            if p and need > 0:
                oid = c.place_order(code, "buy", need, p)
                print(f"  ADD  {code} +{need} @{p:.2f} oid={oid}")
        time.sleep(5)
    else:
        print(f"\nBUY: none (matched)")

    # ═══ Step 7: 验证 ═══
    time.sleep(3)
    raw2 = c.get_positions()
    held2 = {x.split(".")[0]: v.get("volume", 0) for x, v in raw2.items() if v.get("volume", 0) > 0}

    still_missing = target_holdings - set(held2.keys())

    print(f"\n{'='*50}")
    print(f"Result: {len(held2)} positions")
    if still_missing:
        print(f"WARN: still missing {len(still_missing)}: {sorted(still_missing)}")
        print(f"   (waiting for fill)")
    else:
        print(f"OK: all positions filled")

    # ═══ NEVER auto-sell ═══
    extra = set(held2.keys()) - target_holdings
    if extra:
        print(f"INFO: {len(extra)} extra: {sorted(extra)}")
        print(f"   (will NOT auto-sell)")

    logger.info("QMT安全执行完成")

if __name__ == "__main__":
    main()
