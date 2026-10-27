"""把 trade_plan 渲染成 tier3 人工执行清单（只读，不下单）。

背景：`fetch_and_execute` 对 tier3 只出清单、拒绝自动执行；11-02 首次
CB 月度大概率 tier3。本脚本把 plan 变成“先卖后买”的 Markdown/CSV，
并做三件事：
- 买入张数必须 10 张整数倍；卖出可为零头；
- 单笔金额 >10 万标红（需拆单）；
- 生成成交回填列（order_id/成交张数/成交价/状态），供人工逐笔记录。
不修改 plan，不调用任何下单接口。
"""
import argparse
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "logs"
MAX_ORDER_VALUE = 100_000


def build_rows(plan: dict) -> list[dict]:
    rows = []
    for side in ("sell", "buy"):
        for e in plan.get(f"{side}s", []):
            shares = int(e.get("shares", 0) or 0)
            price = float(e.get("price", 0) or 0)
            amount = float(e.get("est_amount", shares * price) or 0)
            code = str(e.get("code", ""))
            flags = []
            if shares <= 0:
                flags.append("股数<=0")
            if side == "buy" and shares % 10 != 0:
                flags.append("买入非10张整手")
            if amount > MAX_ORDER_VALUE:
                flags.append("单笔>10万需拆")
            rows.append({
                "side": side, "code": code, "shares": shares,
                "price": round(price, 4), "est_amount": round(amount, 2),
                "held": e.get("held"), "target": e.get("target"),
                "flags": ";".join(flags),
                "order_id": "", "filled_shares": "", "filled_price": "",
                "status": "", "note": "",
            })
    return rows


def render(plan: dict, date_str: str):
    rows = build_rows(plan)
    sells = [r for r in rows if r["side"] == "sell"]
    buys = [r for r in rows if r["side"] == "buy"]
    total_sell = sum(r["est_amount"] for r in sells)
    total_buy = sum(r["est_amount"] for r in buys)
    lines = [
        f"# CB 人工执行清单 {date_str}", "",
        f"- tier: **{plan.get('tier')}**；reasons: {plan.get('tier_reasons')}",
        f"- 计划总额: {plan.get('total_est', 0):,.0f} 元；"
        f"卖出 {len(sells)} 笔/{total_sell:,.0f}；买入 {len(buys)} 笔/{total_buy:,.0f}；"
        f"净现金 {total_sell - total_buy:+,.0f}",
        "- **先卖后买**；价格是计划参考价，人工下单以 QMT 实时价为准，"
        "委托价必须是 tick 整数倍；单笔≤10 万；涨停禁买/跌停禁卖。",
        "- 本清单只读，不自动下单；成交后回填 CSV 的 order_id/成交张数/成交价。",
        "", "| # | 方向 | 代码 | 张数 | 参考价 | 预估金额 | 校验 |",
        "|---:|---|---|---:|---:|---:|---|",
    ]
    for i, r in enumerate(rows, 1):
        lines.append(
            f"| {i} | {r['side']} | {r['code']} | {r['shares']} | "
            f"{r['price']:.3f} | {r['est_amount']:,.0f} | {r['flags'] or 'OK'} |")
    lines += ["", "## 成交回填", "",
              "见同名 CSV；order_id/成交张数/成交价/状态/备注 五列由人工填写。"]
    md = LOGS / f"trade_plan_{date_str}_cb_checklist.md"
    csv_path = LOGS / f"trade_plan_{date_str}_cb_checklist.csv"
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else
                          ["side", "code", "shares", "price", "est_amount",
                           "held", "target", "flags", "order_id",
                           "filled_shares", "filled_price", "status", "note"])
        w.writeheader()
        w.writerows(rows)
    return md, csv_path, rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="20261102")
    ap.add_argument("--plan", default=None)
    args = ap.parse_args()
    p = Path(args.plan) if args.plan else LOGS / f"trade_plan_{args.date}_cb.json"
    plan = json.loads(p.read_text(encoding="utf-8"))
    md, csv_path, rows = render(plan, args.date)
    print(f"{len(rows)} rows → {md} / {csv_path}")
    for r in rows:
        print(f"{r['side']:4s} {r['code']:8s} {r['shares']:>5d} "
              f"@{r['price']:.3f} = {r['est_amount']:>10,.0f} {r['flags']}")


if __name__ == "__main__":
    main()
