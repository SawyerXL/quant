"""
修正超买仓位：将超量持仓减仓至信号目标股数，并清出多余旧仓位。

默认 dry-run（只预览，不下单）。确认后加 --execute 真正执行。

用法（Windows QMT 服务器上运行）：
    python scripts/fix_overweight.py            # 预览（dry-run）
    python scripts/fix_overweight.py --execute  # 真正下单
"""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from loguru import logger

# ── 修正清单（来自对账报告 2026-06-02）──────────────────────
# 格式：{code: (名称, 实际持仓, 目标持仓, 操作)}
OVERWEIGHT_FIXES = {
    "600816": ("建元信托", 72000, 4800,  "sell_excess"),
    "601939": ("建设银行", 16500, 1100,  "sell_excess"),
    "601198": ("东兴证券",  9000,  600,  "sell_excess"),
    "002559": ("(旧仓位)",  400,    0,   "clear"),
    "600893": ("(旧仓位)",  400,    0,   "clear"),
}

# ── 打印预览 ─────────────────────────────────────────────────
def print_plan(positions: dict):
    print("\n" + "=" * 60)
    print("  修正计划预览")
    print("=" * 60)
    total_sell_value = 0
    orders = []
    for code, (name, expected_actual, target, action) in OVERWEIGHT_FIXES.items():
        actual_vol = positions.get(code, {}).get("volume", 0)
        cost_price = positions.get(code, {}).get("cost_price", 0)

        if actual_vol == 0:
            print(f"  [WARN] {code} {name}: QMT 无持仓，跳过")
            continue

        if action == "sell_excess":
            to_sell = actual_vol - target
            if to_sell <= 0:
                print(f"  [OK] {code} {name}: 实际{actual_vol}股 <= 目标{target}股，无需操作")
                continue
        else:  # clear
            to_sell = actual_vol
            target  = 0

        est_price = cost_price  # 以成本价估算，实际按市价执行
        est_value = to_sell * est_price
        total_sell_value += est_value
        orders.append((code, name, actual_vol, target, to_sell, est_price, est_value))
        flag = "[!!]" if to_sell > 1000 else "[!]"
        print(f"  {flag} {code} {name}:")
        print(f"      当前 {actual_vol:,}股 → 目标 {target:,}股  卖出 {to_sell:,}股")
        print(f"      参考价 {est_price:.2f}  预计回收 ≈ {est_value:,.0f} 元")

    print(f"\n  合计预计回收资金：≈ {total_sell_value:,.0f} 元")
    print("=" * 60)
    return orders


MAX_SINGLE_VALUE = 90_000   # 每批上限（略低于风控100k，留安全边际）


def _split_batches(to_sell: int, price: float) -> list[int]:
    """超过单笔上限时，按100股整数倍拆批。"""
    if price <= 0:
        return [to_sell]
    max_per_batch = max(int(MAX_SINGLE_VALUE / price / 100) * 100, 100)
    batches, remaining = [], to_sell
    while remaining > 0:
        batches.append(min(remaining, max_per_batch))
        remaining -= batches[-1]
    return batches


def execute_orders(orders: list, dry_run: bool = True):
    os.environ.setdefault("ENV", "simulation")
    from execution.qmt_client import get_client

    client    = get_client()
    account   = client.get_account_info()
    raw_pos   = client.get_positions()
    positions = {code.split(".")[0]: v for code, v in raw_pos.items()}

    print(f"\n{'[DRY-RUN] ' if dry_run else '[执行] '}开始处理 {len(orders)} 只"
          f"（大单自动拆批，每批≤{MAX_SINGLE_VALUE//10000}万）...\n")
    results = {"ok": [], "failed": [], "skipped": []}

    for code, name, actual_vol, target, to_sell, _, _ in orders:
        cur_pos  = positions.get(code, {})
        cur_vol  = cur_pos.get("volume", 0)

        if cur_vol == 0:
            logger.warning(f"  跳过 {code} {name}：QMT 无持仓")
            results["skipped"].append(code)
            continue

        real_sell = min(to_sell, cur_vol - target)
        if real_sell <= 0:
            logger.info(f"  {code} {name}: 已达目标股数，跳过")
            results["skipped"].append(code)
            continue

        cur_price  = cur_pos.get("cost_price", 1.0)
        sell_price = round(cur_price * 0.998, 2)
        batches    = _split_batches(real_sell, sell_price)

        logger.info(f"  {code} {name}: 卖出{real_sell:,}股 → 拆{len(batches)}批 {batches}")

        for i, qty in enumerate(batches, 1):
            val = qty * sell_price
            logger.info(f"    {'[模拟]' if dry_run else '[委托]'} 批{i} SELL {code} "
                        f"{qty:,}股 @{sell_price:.2f} ≈{val:,.0f}元")
            if not dry_run:
                try:
                    oid = client.place_order(code, "sell", qty, sell_price)
                    logger.info(f"      → order_id={oid}")
                    results["ok"].append({"code": code, "batch": i,
                                          "shares": qty, "order_id": oid})
                except Exception as e:
                    logger.error(f"      → 失败：{e}")
                    results["failed"].append({"code": code, "reason": str(e)})
            else:
                results["ok"].append({"code": code, "batch": i, "shares": qty})

    print(f"\n{'预览' if dry_run else '执行'}完成:")
    print(f"  [OK] {'可执行批次' if dry_run else '已提交批次'}: {len(results['ok'])}")
    print(f"  [FAIL] 失败: {len(results['failed'])}")
    print(f"  [SKIP]  跳过: {len(results['skipped'])} 只")
    if dry_run:
        print("\n  确认无误后：python scripts/fix_overweight.py --execute")
    return results


def main():
    parser = argparse.ArgumentParser(description="修正QMT超买仓位")
    parser.add_argument("--execute", action="store_true",
                        help="真正下单（不加此参数则为 dry-run 预览）")
    args = parser.parse_args()

    os.environ.setdefault("ENV", "simulation")
    from execution.qmt_client import get_client
    client    = get_client()
    account   = client.get_account_info()
    # QMT返回代码含交易所后缀（600816.SH），统一去掉后缀
    raw_pos   = client.get_positions()
    positions = {code.split(".")[0]: v for code, v in raw_pos.items()}

    print(f"\nQMT 账户: 总资产 {account['total_assets']:,.0f}  "
          f"现金 {account['cash']:,.0f}  持仓 {account['market_value']:,.0f}")
    print(f"QMT 持仓 {len(positions)} 只: {sorted(positions.keys())}")

    orders = print_plan(positions)
    if not orders:
        print("\n无需修正。")
        return

    if not args.execute:
        print("\n[WARN]  当前为 dry-run 预览模式，未真正下单。")
        print("   确认上述计划无误后，运行：")
        print("   python scripts/fix_overweight.py --execute")
        return

    # ── 执行前最后确认 ────────────────────────────────────────
    print("\n" + "=" * 60)
    print("  [WARN]  即将真正提交减仓委托，请最终确认！")
    print("=" * 60)
    confirm = input("  输入 YES 确认执行，其他任意键取消：").strip()
    if confirm != "YES":
        print("  已取消。")
        return

    execute_orders(orders, dry_run=False)


if __name__ == "__main__":
    main()
