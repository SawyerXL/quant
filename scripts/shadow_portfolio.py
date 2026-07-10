"""
主策略影子组合：5万资金跟单主策略Top权重股。
主策略换仓时自动同步，不独立选股。

用法: python scripts/shadow_portfolio.py
"""
import sys, json; from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from datetime import date
from loguru import logger
from data.storage import load_meta

CAPITAL = 50_000; MAX_STOCKS = 2
SIGNAL_FILE = Path("data_store/meta/signal_a_latest.json")
STATE_FILE  = Path("logs/shadow_portfolio.json")


def main():
    today = date.today().strftime("%Y-%m-%d")

    # 读信号
    if not SIGNAL_FILE.exists():
        print("信号文件不存在"); return
    sig = json.loads(SIGNAL_FILE.read_text(encoding="utf-8"))
    weights = sig.get("weights", {})
    prices  = sig.get("prices", {})
    shares_d = sig.get("shares", {})

    info = load_meta("stock_info_full")
    info["code"] = info["code"].astype(str).str.zfill(6)
    nmap = dict(zip(info["code"], info["name"]))

    # 按权重排序，取可买的前2只
    sorted_w = sorted(weights.items(), key=lambda x: x[1], reverse=True)
    bought = []; cash = CAPITAL
    for code, w in sorted_w:
        if len(bought) >= MAX_STOCKS: break
        p = prices.get(code, 0)
        if p <= 0: continue
        per = CAPITAL / MAX_STOCKS
        qty = max(int(per / p / 100) * 100, 100)
        cost = qty * p
        if cost <= cash:
            bought.append({"code": code, "name": nmap.get(code, "?"),
                          "shares": qty, "price": p, "cost": cost,
                          "weight_in_strategy": f"{w:.1%}"})
            cash -= cost

    # 保存状态
    state = {"updated": today, "signal_date": sig.get("signal_date"), "holdings": bought,
             "capital": CAPITAL, "cash": cash}
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))

    # 输出
    print(f"\n  {'='*45}")
    print(f"  主策略影子组合  本金: ¥{CAPITAL:,}  {today}")
    print(f"  信号日期: {sig.get('signal_date')}")
    print(f"  {'='*45}")
    total = 0
    for i, h in enumerate(bought, 1):
        print(f"\n  {i}. {h['code']} {h['name']}")
        print(f"     {h['shares']}股 @{h['price']:.2f}  ¥{h['cost']:,.0f}")
        print(f"     主策略权重: {h['weight_in_strategy']}")
        total += h['cost']
    print(f"\n  投入: ¥{total:,}  现金: ¥{cash:,}")
    print(f"  {'='*45}\n")

    # 对比主策略
    overlap = len(set(h['code'] for h in bought) & set(weights.keys()))
    print(f"  与主策略重叠: {overlap}/{MAX_STOCKS} 只")
    print(f"  下次换仓: 跟随主策略调仓日自动更新\n")

    # 换仓时发邮件
    prev = {}
    old_state = Path("logs/shadow_portfolio_prev.json")
    if old_state.exists():
        try: prev = json.loads(old_state.read_text())
        except: pass
    prev_codes = {h['code'] for h in prev.get('holdings', [])} if prev else set()
    new_codes = {h['code'] for h in bought}
    if prev_codes and prev_codes != new_codes:
        try:
            from monitoring.alerts import send_alert
            sold = prev_codes - new_codes
            added = new_codes - prev_codes
            msg = f"【影子组合换仓】{today}\n"
            if sold: msg += f"卖出: {', '.join(sold)}\n"
            if added: msg += f"买入: {', '.join(added)}\n"
            cur = ', '.join(f"{h['code']} {h['name']}" for h in bought)
            msg += f"当前: {cur}"
            send_alert(msg)
        except Exception: pass
    old_state.write_text(json.dumps(state, ensure_ascii=False))


if __name__ == "__main__":
    main()
