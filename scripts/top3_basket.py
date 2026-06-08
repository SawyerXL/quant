"""
手动跟单篮子：5万本金，始终持有量化模型打分最高的3只股票。
信号直接来自 daily_signal_a.py 的策略打分，量化说什么就是什么。

用法：
    python scripts/top3_basket.py           # 查看当前篮子和信号
    python scripts/top3_basket.py --reset   # 重置篮子
"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
from datetime import date, timedelta
from loguru import logger
from data.storage import load_meta, load_daily
from run_backtest_a2 import compute_score_a2
from run_backtest_a import load_panels

BASKET_SIZE   = 3
CAPITAL       = 50_000          # 5万本金
BASKET_FILE   = Path("logs/top3_basket.json")

# 加载/保存篮子（含成本价和股数）
def _load_basket() -> dict:
    if BASKET_FILE.exists():
        return json.loads(BASKET_FILE.read_text())
    return {"holdings": [], "cost_prices": {}, "shares": {}, "capital": CAPITAL}

def _save_basket(data: dict):
    BASKET_FILE.parent.mkdir(exist_ok=True)
    data["updated"] = date.today().strftime("%Y-%m-%d")
    BASKET_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def main():
    today = date.today().strftime("%Y-%m-%d")
    info = load_meta("stock_info_full")
    info["code"] = info["code"].astype(str).str.zfill(6)
    nmap = dict(zip(info["code"], info["name"]))

    # ── 1. 加载当前持仓和打分 ──────────────────────────
    sig_path = Path("data_store/meta/signal_a_latest.json")
    sig = json.loads(sig_path.read_text("utf-8")) if sig_path.exists() else {}
    strategy_holdings = set(sig.get("holdings", []))
    strategy_sell     = set(sig.get("sell", []))
    ma10_exits        = set(sig.get("ma10_exits", []))

    # 用策略同款打分算全市场排名
    csi = load_meta("csi800")
    codes = sorted(csi["code"].tolist())
    panel, ap = load_panels(codes, "2025-06-01", today)
    score = compute_score_a2(panel, pd.Timestamp(today), ap, info)

    # ── 2. 当前篮子 ──────────────────────────────────
    data   = _load_basket()
    basket = data["holdings"]
    cost_p = data.get("cost_prices", {})
    shares = data.get("shares", {})
    cash   = data.get("capital", CAPITAL)

    if not basket:
        # 首次：取策略持仓中得分最高的3只，资金三等分
        top3 = score[score.index.isin(strategy_holdings)].nlargest(BASKET_SIZE)
        basket = top3.index.tolist()
        per_stock = CAPITAL / BASKET_SIZE
        for code in basket:
            df = load_daily(code, (date.today()-timedelta(days=5)).strftime("%Y-%m-%d"), today)
            cur = float(df["close"].iloc[-1]) if not df.empty else score.get(code, 0)
            lot = 100
            qty = max(int(per_stock / cur / lot) * lot, lot)
            cost_p[code] = cur
            shares[code] = qty
        data = {"holdings": basket, "cost_prices": cost_p, "shares": shares, "capital": CAPITAL}
        _save_basket(data)

    # ── 3. 检查是否需要换仓 ──────────────────────────
    to_remove = []
    for code in basket:
        if code in ma10_exits:
            to_remove.append((code, "策略MA10止损"))
        elif code in strategy_sell:
            to_remove.append((code, "策略调仓卖出"))
        elif code not in strategy_holdings:
            to_remove.append((code, "已不在策略持仓"))

    # ── 4. 选替补 ────────────────────────────────────
    candidates = score.nlargest(30).index.tolist()
    for old_code, reason in to_remove:
        for c in candidates:
            if c not in basket and c not in [r[0] for r in to_remove if r != (old_code, reason)]:
                df = load_daily(c, (date.today()-timedelta(days=5)).strftime("%Y-%m-%d"), today)
                cur = float(df["close"].iloc[-1]) if not df.empty else 0
                lot = 100
                per_stock = CAPITAL / BASKET_SIZE
                qty = max(int(per_stock / cur / lot) * lot, lot)
                cost_p[c] = cur
                shares[c] = qty
                basket.remove(old_code)
                basket.append(c)
                cost_p.pop(old_code, None)
                shares.pop(old_code, None)
                print(f"\n  ⚠️  替换: 卖出 {old_code} {nmap.get(old_code,'?')}（{reason}）"
                      f" → 买入 {c} {nmap.get(c,'?')} {qty}股 @{cur:.2f}")
                break
    data = {"holdings": basket, "cost_prices": cost_p, "shares": shares, "capital": CAPITAL}
    _save_basket(data)

    # ── 5. 输出 ──────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  Top 3 篮子  本金: ¥{CAPITAL:,}  {today}")
    print(f"{'='*60}")

    total_mv = 0; total_cost = 0
    for i, code in enumerate(basket, 1):
        name = nmap.get(code, "?")
        s    = score.get(code, 0)
        cp   = cost_p.get(code, 0)
        sh   = shares.get(code, 0)
        df = load_daily(code, (date.today()-timedelta(days=10)).strftime("%Y-%m-%d"), today)
        cur = float(df["close"].iloc[-1]) if not df.empty else cp
        mv = cur * sh
        pnl = (cur/cp - 1)*100 if cp > 0 else 0
        total_mv += mv; total_cost += cp * sh

        # MA10
        closes = df['close'].values
        ma10_val = closes[-10:].mean() if len(closes)>=10 else cur

        print(f"\n  {i}. {code} {name}")
        print(f"     得分:{s:.2f}  |  {sh}股 @{cp:.2f} → 现价{cur:.2f}")
        print(f"     市值: ¥{mv:,.0f}  盈亏:{pnl:+.1f}%  "
              f"MA10:{ma10_val:.2f} {'❌跌破' if cur<ma10_val else '✅'}")

    pnl_total = total_mv - total_cost
    print(f"\n  {'─'*60}")
    print(f"  总市值: ¥{total_mv:,.0f}  总成本: ¥{total_cost:,.0f}  "
          f"浮动盈亏: {pnl_total:+,.0f}")
    print(f"  今日操作: {'无，持有' if not to_remove else f'已替换{len(to_remove)}只'}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
