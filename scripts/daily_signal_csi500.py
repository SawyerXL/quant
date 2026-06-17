"""
CSI500 中盘增强每日信号 — 选前6只，双周调仓，18万等权。
跟主策略8:55同步运行，独立输出信号文件。
"""
import sys, json, os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd, numpy as np
from datetime import date, timedelta
from loguru import logger
from run_backtest_a2 import compute_score_a2
from run_backtest_a import load_panels
from data.storage import load_meta
from monitoring.alerts import send_alert

CAPITAL = 180_000; N_HOLDINGS = 6
SIGNAL_FILE = Path("data_store/meta/signal_csi500_latest.json")
CSI500_CACHE = Path("data_store/meta/csi500_components.parquet")


def get_csi500_codes() -> list[str]:
    import akshare as ak
    if not CSI500_CACHE.exists():
        df = ak.index_stock_cons(symbol="000905")
        df["code"] = df["品种代码"].astype(str).str.zfill(6)
        df[["code"]].to_parquet(CSI500_CACHE)
    return sorted(pd.read_parquet(CSI500_CACHE)["code"].tolist())


def run():
    today = date.today().strftime("%Y-%m-%d")
    logger.info(f"[CSI500] 生成信号: {today}")

    codes = get_csi500_codes()[:200]
    panel, ap = load_panels(codes, (date.today() - timedelta(days=400)).strftime("%Y-%m-%d"), today)
    info = load_meta("stock_info_full")

    score = compute_score_a2(panel, pd.Timestamp(today), ap, info)
    if len(score) < N_HOLDINGS:
        logger.warning(f"候选不足{len(score)}只，跳过")
        return

    top6 = score.nlargest(N_HOLDINGS)
    holdings = top6.index.tolist()

    info["code"] = info["code"].astype(str).str.zfill(6)
    nmap = dict(zip(info["code"], info["name"]))
    indmap = dict(zip(info["code"], info.get("industry_l1", "其他")))

    hist = panel[panel.index <= today]
    cur_p = hist.iloc[-1]
    per = CAPITAL / N_HOLDINGS
    shares = {}; prices = {}; weights = {}
    pnl = []; total_w = 0

    for code in holdings:
        price = float(cur_p.get(code, 0)) if code in cur_p.index else 0
        if price <= 0: continue
        qty = max(int(per / price / 100) * 100, 100)
        cost = qty * price
        shares[code] = qty; prices[code] = round(price, 2)
        w = cost / CAPITAL
        weights[code] = w; total_w += w
        pnl.append(f"   {code} {nmap.get(code,'?'):<8} {indmap.get(code,'?'):<8} {qty}股 @{price:.2f}  ¥{cost:,.0f}  {w:.1%}")

    signal = {
        "signal_date": today, "generated_at": pd.Timestamp.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "strategy": "CSI500_enhanced_N6",
        "capital": CAPITAL, "holdings": holdings,
        "shares": shares, "prices": prices, "weights": weights,
        "note": f"CSI500中盘增强 6只等权 投入{total_w*CAPITAL:,.0f}元",
    }
    SIGNAL_FILE.parent.mkdir(exist_ok=True)
    SIGNAL_FILE.write_text(json.dumps(signal, ensure_ascii=False, indent=2), encoding="utf-8")

    # 输出
    industry_cnt = {}
    for c in holdings: industry_cnt[indmap.get(c, "其他")] = industry_cnt.get(indmap.get(c, "其他"), 0) + 1

    logger.info(f"[CSI500] 持仓 {len(holdings)} 只 | 行业: {dict(sorted(industry_cnt.items(), key=lambda x: x[1], reverse=True)[:5])}")
    for line in pnl: logger.info(line)
    logger.info(f"💰 投入: {total_w*CAPITAL:,.0f} / {CAPITAL:,}")

    try:
        send_alert(
            f"【CSI500中盘增强】{today}\n"
            f"持仓 {N_HOLDINGS} 只 | 投入 ¥{total_w*CAPITAL:,.0f}\n" +
            "\n".join(pnl[:6])
        )
    except Exception: pass


if __name__ == "__main__":
    run()
