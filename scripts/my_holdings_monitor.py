"""
个人持仓监控 — 只管卖出/持有，绝不推荐买入。

约束：
  本工具不生成买入信号，不推荐新标的。
  输出为决策建议，执行由人工在券商完成。
  monitor=False 的持仓只显示不告警。

用法: python scripts/my_holdings_monitor.py [--enhanced]
Cron: 0 16 * * 1-5
"""
import sys; from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse, pandas as pd, numpy as np
from datetime import date, datetime
from loguru import logger
from data.storage import load_daily, load_meta

HOLDINGS_FILE = Path("config/my_holdings.csv")
LOG_FILE = Path("logs/holdings_monitor.log")
MA_WINDOW = 10; EXIT_DAYS = 3
ABSOLUTE_STOP = -0.12; TRAILING_STOP = -0.18
TAKE_FULL = 0.25; TAKE_HALF = 0.15
WARN_PCT = 0.03   # 接近线3%预警

# ── 核心分析 ───────────────────────────────────────────
def analyze(code: str, name: str, cost_price: float, shares: int,
            buy_date: str, is_etf: bool = False) -> dict:
    """
    返回持仓诊断字典。
    本工具不生成买入信号——returns dict with 'action' in [sell, reduce, warn, hold, locked]
    """
    today = date.today().strftime("%Y-%m-%d")
    mcp_vol_ratio = None  # MCP实时量比
    mcp_outer = None; mcp_inner = None
    # 优先从MCP拉取今日最新数据
    try:
        from data.source.mcp_source import MCPSource
        src = MCPSource()
        fresh = src.get_daily(code, today, today)
        if not fresh.empty:
            if '量比' in fresh.columns:
                mcp_vol_ratio = float(fresh['量比'].iloc[0]) if pd.notna(fresh['量比'].iloc[0]) else None
            if '外盘(万手)' in fresh.columns:
                mcp_outer = float(fresh['外盘(万手)'].iloc[0]) if pd.notna(fresh['外盘(万手)'].iloc[0]) else None
            if '内盘(万手)' in fresh.columns:
                mcp_inner = float(fresh['内盘(万手)'].iloc[0]) if pd.notna(fresh['内盘(万手)'].iloc[0]) else None
            clean = fresh.drop(columns=[c for c in ['量比','外盘(万手)','内盘(万手)','委比(%)','委差(手)','委买档位','委卖档位'] if c in fresh.columns], errors='ignore')
            from data.storage import save_daily
            try: save_daily(code, clean)
            except Exception: pass
    except Exception: pass
    df = load_daily(code, (date.today() - pd.Timedelta(days=90)).strftime("%Y-%m-%d"), today)
    if df.empty or len(df) < 10:
        return {"code": code, "name": name, "action": "nodata", "cur": None,
                "pnl_pct": None, "reason": "数据不足"}

    df = df.sort_values("date"); closes = df["close"].values
    cur = closes[-1]; ma10 = closes[-MA_WINDOW:].mean()
    pnl = (cur / cost_price - 1) if cost_price > 0 else 0
    high_since_buy = max(closes)  # 持有期内最高价

    # MA10 状态
    below = 0
    for c in reversed(closes):
        if c < ma10: below += 1
        else: break

    # 追踪止损：从持有期最高点回撤
    trail_dd = (cur / high_since_buy - 1) if high_since_buy > 0 else 0

    # T+1 锁定
    locked = False
    if buy_date:
        try:
            bd = datetime.strptime(buy_date, "%Y-%m-%d").date()
            if (date.today() - bd).days < 1:
                locked = True
        except Exception: pass

    result = {"code": code, "name": name, "cur": round(cur, 2),
              "ma10": round(ma10, 2), "pnl_pct": round(pnl * 100, 1),
              "below_ma": below, "trail_dd": round(trail_dd * 100, 1),
              "high": round(high_since_buy, 2), "cost": cost_price,
              "shares": shares, "mktval": round(cur * shares, 0), "etf": is_etf,
              "mcp_vol_ratio": mcp_vol_ratio, "mcp_outer": mcp_outer, "mcp_inner": mcp_inner}

    if locked:
        result["action"] = "locked"; result["reason"] = "T+1锁定，今日不可卖"
        return result

    # ETF也适用绝对止损和追踪止损（之前豁免不合理）
    if below >= EXIT_DAYS:
        result["action"] = "sell"; result["reason"] = f"MA10连续{below}日跌破({cur:.2f}<{ma10:.2f})"
    elif pnl <= ABSOLUTE_STOP:
        result["action"] = "sell"; result["reason"] = f"绝对止损{pnl:.1%}(<-12%)"
    elif trail_dd <= TRAILING_STOP:
        result["action"] = "sell"; result["reason"] = f"追踪止损{trail_dd:.1%}(<-18%，从高{high_since_buy:.2f})"
    elif pnl >= TAKE_FULL and not is_etf:
        result["action"] = "sell"; result["reason"] = f"止盈{pnl:.1%}(>+25%)"
    elif pnl >= TAKE_HALF and not is_etf:
        # 检查MA10拐头
        if len(closes) >= 12:
            ma_prev = closes[-12:-2].mean()
            if ma10 < ma_prev:
                result["action"] = "reduce"; result["reason"] = f"止盈减半{pnl:.1%}(>+15%)+MA10拐头"
            else:
                result["action"] = "hold"; result["reason"] = "浮盈保持"
        else:
            result["action"] = "hold"; result["reason"] = "浮盈保持"
    elif pnl <= ABSOLUTE_STOP + WARN_PCT:
        result["action"] = "warn"; result["reason"] = f"接近绝对止损(距{abs(ABSOLUTE_STOP):.0%}仅{abs(pnl-ABSOLUTE_STOP):.1%})"
    elif trail_dd <= TRAILING_STOP + WARN_PCT:
        result["action"] = "warn"; result["reason"] = f"接近追踪止损(距{abs(TRAILING_STOP):.0%}仅{abs(trail_dd-TRAILING_STOP):.1%})"
    elif below == 2:
        result["action"] = "warn"; result["reason"] = f"接近MA10止损(已连续{below}日)"
    else:
        result["action"] = "hold"; result["reason"] = "正常持有"

    return result


# ── 主流程 ───────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="个人持仓监控")
    parser.add_argument("--enhanced", action="store_true",
                        help="启用多因子增强分析(CSI800全市场打分+行业+反弹)")
    args = parser.parse_args()

    if not HOLDINGS_FILE.exists():
        logger.error(f"持仓文件不存在: {HOLDINGS_FILE}")
        return

    df_h = pd.read_csv(HOLDINGS_FILE, dtype={"code": str})
    df_h["code"] = df_h["code"].str.zfill(6)
    results = []; alerts = []; today = date.today().strftime("%Y-%m-%d")

    # 预加载多因子分析引擎
    analyzer = None
    if args.enhanced:
        try:
            from scripts.holdings_analyzer import HoldingsAnalyzer
            analyzer = HoldingsAnalyzer()
        except Exception as e:
            logger.error(f"HoldingsAnalyzer 初始化失败: {e}, 降级为纯价格模式")
            args.enhanced = False

    for _, r in df_h.iterrows():
        code, name, monitor = r["code"], r["name"], r.get("monitor", True)
        if not monitor:
            results.append({"code": code, "name": name, "action": "skip", "reason": "monitor=False"})
            continue

        cp = float(r["cost_price"]) if pd.notna(r["cost_price"]) else 0
        sh = int(r["shares"]) if pd.notna(r["shares"]) else 0
        bd = str(r["buy_date"]) if pd.notna(r.get("buy_date")) else ""
        etf = code.startswith("51") or code.startswith("15")
        res = analyze(code, name, cp, sh, bd, etf)

        # 多因子增强
        if analyzer is not None and res.get("action") not in ("skip", "nodata"):
            try:
                res = analyzer.evaluate(code, name, cp, sh, bd, etf, base_result=res)
            except Exception as e:
                logger.warning(f"  因子增强失败 {code}: {e}")

        results.append(res)

    # ── 输出 ─────────────────────────────────────────
    enhanced = args.enhanced and analyzer is not None
    print(f"\n{'='*80}")
    print(f"  个人持仓监控  {today}{'  [多因子增强]' if enhanced else ''}")
    rule_line = f"  规则: 绝对-12% | 追踪-18% | MA10三日 | 止盈25%/减半15% | T+1"
    if enhanced:
        rule_line += f" | 市场: {analyzer.regime_label}"
    print(rule_line)
    print(f"{'='*80}")

    if enhanced:
        print(f"\n  {'代码':<8} {'名称':<8} {'现价':>7} {'盈亏':>7} {'分位':>6} {'趋势':<4} {'行业':<6} {'做T':<8} {'动作':<14} {'原因'}")
        print(f"  {'-'*78}")
    else:
        print(f"\n  {'代码':<8} {'名称':<8} {'现价':>8} {'盈亏':>7} {'MA10':>8} {'动作':<12} {'原因'}")
        print(f"  {'-'*66}")

    for r in results:
        act = r.get("action", "?"); cur = r.get("cur", 0) or 0; pnl = r.get("pnl_pct", 0) or 0
        tag = {"sell": "🔴 清仓", "reduce": "🟡 减半", "warn": "🔔 预警", "hold": "✅ 持有",
               "locked": "🔒 锁定", "skip": "⏭️ 跳过", "nodata": "❓",
               "cut": "💀 减仓", "weakening": "🟠 走弱",
               "strong_hold": "⭐ 强势持有"}.get(act, act)
        if enhanced:
            sp = r.get("score_pct")
            sp_str = f"{sp:.0%}" if sp is not None else "N/A"
            st = r.get("score_trend", "?")
            st_icon = {"rising": " ↑", "falling": " ↓", "stable": " →", "unknown": " ?"}.get(st, " ?")
            sector = (r.get("sector", "?"))[:6]
            # T+0 signal
            ts = r.get("t_signal", "none")
            ts_icon = {"dip_buy": "📉抄底", "trend_ride": "📈加仓", "bounce_t": "🔄做T",
                       "falling_knife": "⚠️回避", "none": "—"}.get(ts, "—")
            add = r.get("add_signal", "wait")
            add_icon = {"yes_add": "✅", "no_add": "❌", "wait": "⏸"}.get(add, "")
            print(f"  {r.get('code',''):<8} {r.get('name',''):<8} {cur:>7.2f} {pnl:>+6.1f}% "
                  f"{sp_str:>6} {st_icon:<4} {sector:<6} {ts_icon:<8} {tag:<14} {r.get('reason','')}")
            # 两融信号
            ms = r.get("margin_signal")
            if ms:
                print(f"  {'':16}  💰两融: {ms}")

            # 龙虎榜
            ds = r.get("dragon_signal")
            if ds:
                print(f"  {'':16}  🐉龙虎榜: {ds}")

            # 业绩预警
            ea = r.get("earnings_alert")
            if ea:
                print(f"  {'':16}  📋业绩: {ea}")

            # MCP实时量比(比历史vol_ratio更准)
            mcp_vr = r.get("mcp_vol_ratio")
            if mcp_vr is not None:
                vr_tag = "放量" if mcp_vr > 1.5 else ("正常" if mcp_vr > 0.7 else "缩量")
                mcp_out = r.get("mcp_outer", 0) or 0
                mcp_in = r.get("mcp_inner", 0) or 0
                net_tag = "买入>" if mcp_out > mcp_in else ("卖出>" if mcp_in > mcp_out else "均衡")
                print(f"  {'':16}  📊MCP实时: 量比{mcp_vr:.1f}x({vr_tag}) 外盘{mcp_out:.1f}万 内盘{mcp_in:.1f}万 {net_tag}")

            # Suggestion line (only for non-exit actions)
            if act not in ("sell", "reduce", "skip", "nodata", "locked", "cut"):
                sug = r.get("suggestion", "")
                if sug:
                    pred = r.get("pred_details", {})
                extra = []
                if pred.get("vol_pattern") == "accumulation": extra.append("吸筹")
                elif pred.get("vol_pattern") == "distribution": extra.append("出货")
                if pred.get("cons_down", 0) >= 3: extra.append(f"连跌{pred['cons_down']}天")
                elif pred.get("cons_up", 0) >= 3: extra.append(f"连涨{pred['cons_up']}天")
                mom5 = pred.get("mom_5d", 0)
                if abs(mom5) > 0.03: extra.append(f"5日{mom5:+.1%}")
                at_h = pred.get("at_high_20d", 0)
                at_l = pred.get("at_low_20d", 0)
                if at_h > -0.03: extra.append(f"近压力({at_h:+.0%})")
                if at_l < 0.03: extra.append(f"近支撑({at_l:+.0%})")
                ext_str = ' | '.join(extra) if extra else ''
                print(f"  {'':16}  {sug}  {ext_str}")
        else:
            print(f"  {r.get('code',''):<8} {r.get('name',''):<8} {cur:>8.2f} {pnl:>+6.1f}% "
                  f"{r.get('ma10',0):>8.2f} {tag:<12} {r.get('reason','')}")
        if act in ("sell", "reduce", "warn", "cut", "weakening"):
            alerts.append(f"{tag} {r['code']} {r['name']} ({r.get('reason','')})")

    # ── 汇总 ─────────────────────────────────────────
    print(f"\n  {'─'*78}")
    total_mv = sum(r.get("mktval", 0) or 0 for r in results)
    print(f"  持仓市值: ¥{total_mv:,.0f}")
    if alerts:
        print(f"  ⚠️  需关注: {len(alerts)} 项")
        for a in alerts: print(f"    {a}")
    else:
        print(f"  ✅ 全部正常持有")
    if enhanced:
        print(f"  市场环境: {analyzer.regime_label}")

    # ── 邮件报告 ─────────────────────────────────────
    if enhanced:
        try:
            from scripts.holdings_report import build_daily_report
            full_body = build_daily_report(results, analyzer, today)
            from monitoring.alerts import send_alert
            send_alert(full_body)
        except Exception as e:
            logger.error(f"增强报告失败: {e}, 降级简单告警")
            from monitoring.alerts import send_alert
            send_alert("【个人持仓监控】" + " | ".join(alerts))
    elif alerts:
        try:
            from monitoring.alerts import send_alert
            send_alert("【个人持仓监控】" + " | ".join(alerts))
        except Exception as e:
            logger.warning(f"告警失败: {e}")

    # 归档
    LOG_FILE.parent.mkdir(exist_ok=True)
    import json
    LOG_FILE.write_text(json.dumps({"date": today, "results": results}, ensure_ascii=False, indent=2))
    print(f"\n  日志: {LOG_FILE}")
    print(f"{'='*65}\n")


def _send_daily_report(results, alerts, analyzer, today):
    """构建并发送每日持仓分析邮件，格式与终端输出一致。"""
    from monitoring.alerts import send_alert

    # ── 隔夜美盘分析 ───────────────────────────────
    overnight_lines = ""
    try:
        from scripts.overnight_market import get_overnight_analysis, format_overnight_report
        ov = get_overnight_analysis()
        overnight_lines = format_overnight_report(ov) + "\n\n"
    except Exception as e:
        logger.warning(f"隔夜分析获取失败: {e}")

    # ── 持仓异动检测 ───────────────────────────────
    anomaly_lines = ""
    try:
        from scripts.ml_signals import AnomalyDetector
        ad = AnomalyDetector()
        meta_c800 = load_meta("csi800")
        ad.fit(sorted(meta_c800["code"].tolist())[:200], today)
        anoms = []
        for r in results:
            if r.get("action") in ("skip", "nodata", "sell"): continue
            is_a, score, desc = ad.detect(r["code"], today)
            if is_a:
                anoms.append(f"    {r['code']} {r['name']}: {desc} (分数{score:.2f})")
        if anoms:
            anomaly_lines = "🔍 持仓异动检测(ML)\n" + "\n".join(anoms) + "\n\n"
        else:
            anomaly_lines = "🔍 持仓异动检测(ML)\n  今日无异常信号\n\n"
    except Exception as e:
        logger.warning(f"异动检测失败: {e}")

    actives_all = [r for r in results if r.get("action") != "skip"]
    sell_list   = [r for r in actives_all if r.get("action") == "sell"]
    cut_list    = [r for r in actives_all if r.get("action") == "cut"]
    warn_list   = [r for r in actives_all if r.get("action") in ("warn", "weakening")]
    add_list    = [r for r in actives_all if r.get("add_signal") == "yes_add" and r.get("action") == "hold"]
    alert_hold  = [r for r in actives_all if r.get("action") == "hold"
                   and r.get("add_signal") != "yes_add"
                   and (r.get("pred_details", {}).get("vol_pattern") == "distribution"
                        or r.get("pred_details", {}).get("at_high_20d", -1) > -0.03
                        or r.get("score_pct") is None)]
    normal_hold = [r for r in actives_all if r.get("action") == "hold"
                   and r not in add_list and r not in alert_hold]

    def _pnl(r): return r.get("pnl_pct", 0) or 0
    def _sp(r):
        v = r.get("score_pct")
        return f"{v:.0%}" if v is not None else "N/A"
    def _st(r):
        m = {"rising":"↑", "falling":"↓", "stable":"→", "unknown":"?"}
        return m.get(r.get("score_trend", "?"), "?")
    def _ts(r):
        m = {"dip_buy":"📉抄底", "trend_ride":"📈加仓", "bounce_t":"🔄做T",
             "falling_knife":"⚠️回避", "none":"—"}
        return m.get(r.get("t_signal", "none"), "—")

    total_mv = sum(r.get("mktval", 0) or 0 for r in actives_all if r.get("action") != "nodata")
    pnl_total = sum(
        (_pnl(r) / 100) * (r.get("mktval", 0) or 0)
        for r in actives_all if r.get("mktval") and r.get("pnl_pct") is not None
    )

    lines = [
        f"个人持仓日报 — {today}",
        f"市场: {analyzer.regime_label} | 市值: ¥{total_mv:,.0f} | 盈亏: {pnl_total:+,.0f}",
        f"=" * 70,
        "",
    ]

    def _section(title, items, cols):
        if not items: return
        lines.append(title)
        lines.append("")
        # Header
        header = "  " + "  ".join(f"{c:<{w}}" for c, w in cols)
        lines.append(header)
        lines.append("  " + "-" * (sum(w for _, w in cols) + 2*(len(cols)-1)))
        for r in items:
            vals = []
            for c, w in cols:
                if c == "代码": vals.append(f"{r.get('code',''):<{w}}")
                elif c == "名称": vals.append(f"{r.get('name',''):<{w}}")
                elif c == "盈亏": vals.append(f"{_pnl(r):+{w}.1f}%")
                elif c == "分位": vals.append(f"{_sp(r):<{w}}")
                elif c == "趋势": vals.append(f"{_st(r):<{w}}")
                elif c == "做T": vals.append(f"{_ts(r):<{w}}")
                elif c == "判断":
                    reason = r.get("reason","")
                    sug = r.get("suggestion","")
                    txt = f"{reason}。{sug}" if sug else reason
                    vals.append(f"{txt[:w]}")
            lines.append("  " + "  ".join(vals))
        lines.append("")

    # 🔴 卖出
    _section("🔴 触发卖出", sell_list,
             [("代码", 8), ("名称", 10), ("盈亏", 8), ("分位", 6), ("判断", 65)])

    # 💀 减仓
    _section("💀 减仓", cut_list,
             [("代码", 8), ("名称", 10), ("盈亏", 8), ("分位", 6), ("趋势", 4), ("判断", 65)])

    # 🔔 预警
    if warn_list:
        _section("🔔 预警关注", warn_list,
                 [("代码", 8), ("名称", 10), ("盈亏", 8), ("分位", 6), ("判断", 65)])

    # ✅ 可补仓
    _section("✅ 可补仓/加仓", add_list,
             [("代码", 8), ("名称", 10), ("盈亏", 8), ("分位", 6), ("趋势", 4), ("做T", 8), ("判断", 70)])

    # ⚠️ 持有需警惕
    _section("⚠️ 持有但需警惕", alert_hold,
             [("代码", 8), ("名称", 10), ("盈亏", 8), ("分位", 6), ("趋势", 4), ("判断", 65)])

    # ✅ 正常持有
    _section("✅ 正常持有", normal_hold,
             [("代码", 8), ("名称", 10), ("盈亏", 8), ("分位", 6), ("趋势", 4), ("判断", 65)])

    # 汇总
    parts = []
    if sell_list: parts.append(f"🔴卖出{len(sell_list)}只")
    if cut_list: parts.append(f"💀减仓{len(cut_list)}只")
    if warn_list: parts.append(f"🔔预警{len(warn_list)}只")
    if add_list: parts.append(f"✅可加仓{len(add_list)}只")
    if normal_hold: parts.append(f"✅持有{len(normal_hold)}只")
    lines.append("=" * 70)
    lines.append("  " + " | ".join(parts) if parts else "  ✅ 全部正常")
    lines.append(f"  数据: 实时取价 | 分析: 多因子增强(CSI800全市场打分)")
    lines.append("=" * 70)

    # ── 万泰生物补仓监测 ─────────────────────────
    bottomfish_lines = ""
    try:
        df_603392 = load_daily("603392", (date.today()-pd.Timedelta(days=30)).strftime("%Y-%m-%d"), today)
        if not df_603392.empty and len(df_603392) >= 10:
            df_603392 = df_603392.sort_values("date")
            closes = pd.to_numeric(df_603392["close"], errors="coerce").dropna()
            volumes = pd.to_numeric(df_603392["volume"], errors="coerce").dropna() if "volume" in df_603392.columns else None
            amounts = pd.to_numeric(df_603392["amount"], errors="coerce").dropna() if "amount" in df_603392.columns else None

            cur = closes.iloc[-1]; ma10 = closes.iloc[-10:].mean()
            prev_close = closes.iloc[-2] if len(closes) >= 2 else cur
            green_candle = cur > prev_close

            vol_shrink = False
            if amounts is not None and len(amounts) >= 20:
                recent_amt = amounts.iloc[-1]
                avg20 = amounts.iloc[-21:-1].mean()
                vol_shrink = recent_amt < avg20 * 0.5 and recent_amt < 50000000

            above_ma10 = cur > ma10

            checks = []
            checks.append(f"缩量: {'✅' if vol_shrink else '❌'} (日成交<5000万且<均量50%)")
            checks.append(f"阳线: {'✅' if green_candle else '❌'} (收盘>{prev_close:.2f})")
            checks.append(f"站上MA10: {'✅' if above_ma10 else '❌'} (现价{cur:.2f} vs MA10 {ma10:.2f})")

            met = sum([vol_shrink, green_candle, above_ma10])
            if met >= 2:
                signal = "🟢 触发补仓信号!" if met == 3 else "🟡 接近补仓(满足{}/3)".format(met)
            else:
                signal = f"⏸ 继续等待({met}/3)"

            bottomfish_lines = (
                f"🎯 万泰生物(603392) 补仓监测\n"
                f"─" * 36 + "\n"
                f"  现价: ¥{cur:.2f} | 成本: ¥63.157 | 亏损: {(cur/63.157-1)*100:.1f}%\n"
                f"  MA10: {ma10:.2f} | 距60日低: {(cur/closes.min()-1)*100:+.1f}%\n"
                f"  {' | '.join(checks)}\n"
                f"  📌 {signal}\n"
                f"─" * 36 + "\n\n"
            )
    except Exception as e:
        logger.warning(f"万泰生物监测失败: {e}")

    # ── 华安证券建仓监测 ─────────────────────────
    entry_lines = ""
    try:
        df_600909 = load_daily("600909", (date.today()-pd.Timedelta(days=30)).strftime("%Y-%m-%d"), today)
        if not df_600909.empty and len(df_600909) >= 10:
            df_600909 = df_600909.sort_values("date")
            closes = pd.to_numeric(df_600909["close"], errors="coerce").dropna()
            amounts = pd.to_numeric(df_600909["amount"], errors="coerce").dropna()

            cur = closes.iloc[-1]; ma10 = closes.iloc[-10:].mean()
            high_20 = closes.iloc[-20:].max() if len(closes) >= 20 else closes.max()
            prev_close = closes.iloc[-2] if len(closes) >= 2 else cur

            pullback_target = ma10
            pullback_pct = (cur / high_20 - 1) * 100
            near_ma10 = abs(cur - ma10) / ma10 < 0.03

            vol_shrink = False
            if len(amounts) >= 20:
                recent = amounts.iloc[-1]
                avg20 = amounts.iloc[-21:-1].mean()
                peak_vol = amounts.iloc[-10:].max()
                vol_shrink = recent < avg20 * 0.5 and recent < peak_vol * 0.3

            green = cur > prev_close

            checks = []
            checks.append(f"回调至MA10: {'✅' if near_ma10 else '❌'} (现价{cur:.2f} vs MA10{ma10:.2f}, 距高点{pullback_pct:+.1f}%)")
            checks.append(f"缩量: {'✅' if vol_shrink else '❌'}")
            checks.append(f"阳线: {'✅' if green else '❌'}")

            met = sum([near_ma10, vol_shrink, green])
            if met >= 3: signal = "🟢 建仓条件满足!"
            elif met >= 2: signal = f"🟡 接近建仓({met}/3)"
            else: signal = f"⏸ 继续等待({met}/3)"

            entry_lines = (
                f"🎯 华安证券(600909) 建仓监测\n"
                f"─" * 36 + "\n"
                f"  现价: ¥{cur:.2f} | MA10: {ma10:.2f} | 距20日高: {pullback_pct:+.1f}%\n"
                f"  目标建仓区: ¥{ma10*0.97:.2f}-{ma10*1.03:.2f} (MA10附近)\n"
                f"  {' | '.join(checks)}\n"
                f"  📌 {signal}\n"
                f"─" * 36 + "\n\n"
            )
    except Exception as e:
        logger.warning(f"华安证券监测失败: {e}")

    # ── QMT实盘持仓 ──────────────────────────────
    qmt_lines = ""
    try:
        from scripts.qmt_sync import get_qmt_summary, format_qmt_report
        qmt_data = get_qmt_summary()
        if qmt_data.get("available"):
            qmt_lines = format_qmt_report(qmt_data) + "\n\n"
    except Exception as e:
        logger.warning(f"QMT同步失败: {e}")

    # ── 板块轮动(每日) ───────────────────────────
    rotation_lines = ""
    try:
        from scripts.ml_signals import sector_rotation_heatmap, format_rotation_report
        c800_rot = load_meta("csi800")
        codes_800 = sorted(c800_rot["code"].tolist())
        from scripts.run_backtest_a import load_panels
        panel_r, _ = load_panels(codes_800,
                                 (pd.Timestamp(today)-pd.Timedelta(days=350)).strftime("%Y-%m-%d"),
                                 today)
        if not panel_r.empty:
            info_r = load_meta("stock_info_full")
            df_rot = sector_rotation_heatmap(panel_r, info_r, today)
            rotation_lines = format_rotation_report(df_rot) + "\n\n"
    except Exception as e:
        logger.warning(f"板块轮动失败: {e}")

    # 按顺序组装完整邮件body
    body_parts = [overnight_lines]
    if bottomfish_lines: body_parts.append(bottomfish_lines)
    if entry_lines: body_parts.append(entry_lines)
    if qmt_lines: body_parts.append(qmt_lines)
    if anomaly_lines: body_parts.append(anomaly_lines)
    if rotation_lines: body_parts.append(rotation_lines)
    body_parts.append("\n".join(lines))
    send_alert("".join(body_parts))

if __name__ == "__main__":
    main()
