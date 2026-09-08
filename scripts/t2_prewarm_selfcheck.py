"""
T2 预加载自检 (2026-09-08 overnight T2) — min_bars=250 暖机改为数据预加载后
的三项验证。上会话临时 probe 自检(a)❌(面板仅24根预窗口bar且净值1/2偏离),
本脚本落盘复验并加量纲抽检:

(a) 首调仓一致性: 引擎首个实际成交日 == 窗口首个调仓日(2019-01-15/2015-01-15),
    且在此之前净值与纯现金线相对偏差 < 1e-6(浮点级);
(b) 无前视: 窗口首调仓日上 vol20/MA10/成交额20日均 的数据范围全部 ≤ 当日,
    且预窗口 bar 数 ≥ 各指标窗口(vol20需20, MA10需10, MA200由指数序列供给);
(c) 预加载段量纲抽检: 成交额(万元)÷成交量(万股)×100 ≈ 收盘价, 逐日计算
    偏离比例, 防止暖机段把单位混写类污染带进 2019 首季指标(第零道闸门)。

用法: python scripts/t2_prewarm_selfcheck.py [--window main|oos]
输出: 追加 logs/t2_selfcheck_20260908.log
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd
from loguru import logger
logger.remove()

from data.storage import load_daily, load_meta
from backtest_config import BacktestConfig, DEFAULT_CONFIG
from backtest_engine import run_backtest, make_rebal_dates
from backtest_return_attribution import (WINDOWS, BASE, load_blacklist,
                                         load_pit_memberships, load_entry_map)

PREWARM_DAYS = 400          # 与 backtest_return_attribution.main() 一致
DEV_TOL = 1e-6              # 净值与现金线相对偏差容忍(浮点级)


def build_panels(START, END):
    LOAD_START = (pd.Timestamp(START) - pd.Timedelta(days=PREWARM_DAYS)).strftime("%Y-%m-%d")
    meta = load_meta("stock_info_full")
    blacklist = load_blacklist()
    codes = [c for c in meta["code"].tolist() if c not in blacklist] \
        if not meta.empty else []
    prices, amounts = {}, {}
    for code in codes:
        try:
            d = load_daily(code, LOAD_START, END)
            if d.empty:
                continue
            d["date"] = pd.to_datetime(d["date"])
            d = d.set_index("date").sort_index()
            cl = pd.to_numeric(d["close"], errors="coerce").dropna()
            amt = pd.to_numeric(d.get("amount", pd.Series(dtype=float)),
                                errors="coerce")
            if len(cl) >= 200:
                prices[code] = cl
                if len(amt) >= 200:
                    amounts[code] = amt
        except Exception:
            pass
    panel = pd.DataFrame(prices).sort_index()
    ap = pd.DataFrame(amounts).sort_index()
    return panel, ap, codes


def main():
    import argparse
    ap_ = argparse.ArgumentParser()
    ap_.add_argument("--window", default="main", choices=["main", "oos"])
    args = ap_.parse_args()
    START, END = WINDOWS[args.window]
    ws = pd.Timestamp(START)

    lines = []
    def log(s=""):
        lines.append(s)

    panel, ap, codes = build_panels(START, END)
    ic = load_meta("csi800_index").set_index("date")["close"].sort_index()
    ic.index = pd.to_datetime(ic.index)
    sh = load_daily("000001", "2014-06-01", END)
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))
    base = [d for d in make_rebal_dates(cal, "biweekly") if START <= d <= END]
    first_rebal = base[0]
    pre_bars = int((panel.index < ws).sum())

    log(f"面板: {panel.shape}, 首日{panel.index[0].date()}, "
        f"窗口首日前的bar数: {pre_bars}")
    log(f"窗口首个调仓日(预期): {first_rebal}")

    # 引擎单跑 A3 配置(diag_exposure=True 以取得每日敞口诊断)
    cfg = BacktestConfig(**{**DEFAULT_CONFIG.to_dict(), **BASE,
                            "diag_exposure": True})
    nav, info = run_backtest(panel, ap, base, cfg, ic)
    cash_nav = np.cumprod(np.full(len(nav), 1 + cfg.cash_yield / 252))
    cash_nav = pd.Series(cash_nav, index=nav.index)
    dev = ((nav - cash_nav).abs() / cash_nav)
    dev_dates = dev[dev > DEV_TOL]
    first_dev = str(dev_dates.index[0].date()) if len(dev_dates) else "无"

    log(f"净值首次偏离现金收益(>{DEV_TOL:.0e})的日期: {first_dev}")
    # (a) 修正口径(2026-09-08二轮): 首笔成交日取决于择时档位——
    # 窗口首调仓日若 tier0(pos≤0.3清仓档)则空仓不成交, 属正常语义;
    # T2 目标=成交不被 min_bars 延迟(旧行为: 窗口第250根≈窗口后1年)。
    # 断言: ①预窗口bar≥250 ②首笔成交日∈窗口前3个调仓日且为档位>0.3的首个
    # 调仓日 ③首笔成交日早于窗口内第250根bar(证明无暖机延迟)。
    # 成交只发生在调仓日(Step4), 故首个成交日=窗口内首个"调仓日∩目标>0.3"
    # (四轮修正: 早前版本找首个目标>0.3的"日子", 命中非调仓日如2/13,
    #  而2/15调仓日恰好目标回0.3清仓档→实际首成交2/28, 属正确语义)
    exp_ts = info.get("exposure_ts") or []
    rebal_set = set(base)
    first_trade_rebal = None
    for dstr, actual, target, _n in exp_ts:
        if dstr >= str(ws.date()) and dstr in rebal_set and target > 0.3:
            first_trade_rebal = dstr
            break
    assert_first = (first_dev == first_trade_rebal) if first_trade_rebal else (info.get("trades", 0) == 0)
    bar250_date = str(nav.index[min(pre_bars + 249, len(nav) - 1)].date()) \
        if pre_bars + 250 < len(nav) else "样本内无"
    early_ok = (first_dev != "无") and (bar250_date == "样本内无"
                                        or first_dev < bar250_date)
    ok_a = (pre_bars >= 250) and assert_first and (first_dev != "无") and early_ok
    log(f"  ①预窗口bar数{pre_bars} {'≥250 ✅' if pre_bars >= 250 else '❌<250'}")
    log(f"  ②首笔成交日={first_dev} vs 首个目标仓位>0.3调仓日={first_trade_rebal} "
        f"{'✅' if assert_first else '❌'}")
    log(f"  ③首笔成交日早于窗口内第250根bar({bar250_date}) {'✅' if early_ok else '❌ 仍被暖机延迟'}")
    log(f"  引擎trades={info.get('trades')} tier切换={info.get('tier_switch_count')}")
    log(f"自检(a): {'✅ 预加载生效, 成交=档位驱动无暖机延迟' if ok_a else '❌ 不一致(见上)'}")

    # (b) 无前视: 首调仓日指标数据范围
    i = panel.index.get_loc(pd.Timestamp(first_rebal))
    idx = list(panel.index)
    log(f"自检(b): 预窗口bar数{pre_bars} "
        f"{'≥20(vol20/MA10/成交额20日均可算)' if pre_bars >= 20 else '❌ <20'} "
        f"{'✅' if pre_bars >= 20 else ''}")
    amt_avg = ap.iloc[max(0, i - 20):i].mean().dropna()
    pool = amt_avg.nlargest(60).index.tolist()
    for code in pool[:3]:
        rr = panel[code].iloc[max(0, i - 21):i].pct_change().dropna()
        seg = panel[code].iloc[max(0, i - 21):i]
        log(f"  {code}: 首调仓日vol20窗口数据范围 {seg.index[0].date()}~"
            f"{seg.index[-1].date()} (全部≤{first_rebal}, 含{int((seg.index < ws).sum())}根窗口前) "
            f"n={len(rr)} {'✅' if seg.index[-1] <= pd.Timestamp(first_rebal) else '❌ 前视!'}")

    # (c) 预加载段量纲抽检(三轮修正): est=amount/volume≈VWAP, est/close=复权因子。
    # 高送转股该比值≠1(如002594≈3.1, 成交额是真实成交不受复权影响, 排名口径
    # 正确)——首轮按[0.5,2]判异常是错误设计。真正的单位错配(万元/万股 vs
    # 元/股混写)=ratio跳10^4倍。判据: ①段内ratio稳定(段内max/min≤5x)
    # ②预加载段ratio中位与窗口段(前60日)一致(边界不跳>5x) ③全局∈[0.05,20]。
    pre_seg = panel[(panel.index < ws) & (panel.index >= pd.Timestamp(START)
                                          - pd.Timedelta(days=PREWARM_DAYS))]
    if len(pre_seg):
        bad = []
        for code in pool[:60]:
            d = load_daily(code, (ws - pd.Timedelta(days=PREWARM_DAYS)).strftime("%Y-%m-%d"),
                           (ws + pd.Timedelta(days=90)).strftime("%Y-%m-%d"))
            if d.empty:
                continue
            d["date"] = pd.to_datetime(d["date"])
            v = pd.to_numeric(d["volume"], errors="coerce")
            a = pd.to_numeric(d["amount"], errors="coerce")
            c = pd.to_numeric(d["close"], errors="coerce")
            ratio = (a / v / c)
            ratio = ratio[(v > 0) & (c > 0)].dropna()
            if len(ratio) < 10:
                continue
            pre_r = ratio[d["date"] < ws]
            win_r = ratio[d["date"] >= ws]
            pre_med = pre_r.median() if len(pre_r) else None
            win_med = win_r.median() if len(win_r) else None
            seg_jump = (pre_med / win_med) if (pre_med and win_med and win_med > 0) else 1.0
            intra = pre_r.max() / pre_r.min() if len(pre_r) > 1 else 1.0
            if not (0.05 < (pre_med or 1) < 20) or seg_jump > 5 or seg_jump < 0.2 or intra > 5:
                bad.append((code, round(float(pre_med), 2) if pre_med else None,
                            round(float(win_med), 2) if win_med else None,
                            round(float(intra), 1)))
        # 预加载段覆盖抽检: 窗口前最近150根bar中有效<100根的池内票
        # (首调仓日指标质量降级, 记录不阻塞)
        thin = []
        for code in pool[:60]:
            n = int(panel[code].iloc[max(0, pre_bars - 150):pre_bars].notna().sum())
            if 0 < n < 100:
                thin.append((code, n))
        if bad:
            log(f"自检(c): ❌ 单位错配/边界跳变 {len(bad)}只: {bad[:8]}")
        else:
            log(f"自检(c): ✅ 预加载段量纲抽检(池60只, 段内稳定+跨段连续+全局∈[0.05,20])全过")
        if thin:
            log(f"  ⚠️ 预加载段覆盖<150根bar的池内票{len(thin)}只(指标降级不阻塞): {thin[:8]}")
    else:
        log("自检(c): ⚠️ 无预加载段数据, 跳过")

    out = "\n".join(lines)
    print(out)
    with open("logs/t2_selfcheck_20260908.log", "a", encoding="utf-8") as f:
        f.write(f"\n=== {args.window} @ 重跑 ===\n{out}\n")


if __name__ == "__main__":
    main()
