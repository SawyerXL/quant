"""
执行MA10止损出清单。
在Windows上运行：
    python scripts/execute_ma10_exits.py           # 预览
    python scripts/execute_ma10_exits.py --execute # 真正下单
"""
import argparse, json, os, sys
from pathlib import Path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")
from loguru import logger

ORDERS_FILE = ROOT / "logs/ma10_exit_orders_20260605.json"

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    data = json.loads(ORDERS_FILE.read_text(encoding="utf-8"))
    orders = data["orders"]

    print(f"\n{'='*60}")
    print(f"  MA10止损出清  {data['generated_at']}")
    print(f"  {data['note']}")
    print(f"  共 {len(orders)} 只  预计释放 {data['estimated_release']:,.0f} 元")
    print(f"{'='*60}\n")

    for o in orders:
        print(f"  {'[待执行]' if not args.execute else '[将下单]'} "
              f"SELL {o['code']} {o['name']} {o['shares']:,}股 "
              f"@{o['ref_price']:.2f}  {o['reason']}")

    if not args.execute:
        print(f"\n  [WARN]  预览模式，未下单。确认后加 --execute 执行。")
        return

    confirm = input(f"\n  即将提交 {len(orders)} 笔止损卖出，输入 YES 确认：").strip()
    if confirm != "YES":
        print("  已取消。"); return

    os.environ.setdefault("ENV", "simulation")
    from execution.qmt_client import get_client
    from scripts.fix_overweight import _split_batches

    client = get_client()
    raw    = client.get_positions()
    pos    = {c.split(".")[0]: v for c, v in raw.items()}
    MAX_SINGLE = 90_000

    ok_cnt = 0
    for o in orders:
        code, shares, price = o['code'], o['shares'], o['ref_price']
        cur_vol = pos.get(code, {}).get("volume", 0)
        if cur_vol == 0:
            logger.warning(f"  {code} QMT无持仓，跳过")
            continue
        real_sell = min(shares, cur_vol)
        batches = _split_batches(real_sell, price)
        for i, qty in enumerate(batches, 1):
            oid = client.place_order(code, "sell", qty, price)
            logger.info(f"  SELL {code} 批{i} {qty}股 @{price:.2f} → {oid}")
            ok_cnt += 1

    print(f"\n  已提交 {ok_cnt} 笔委托")

if __name__ == "__main__":
    main()
