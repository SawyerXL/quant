"""
持仓日报邮件构建器 — 独立模块, 避免重复bug
"""
import pandas as pd
from datetime import date, timedelta
from data.storage import load_daily, load_meta

def build_daily_report(results, analyzer, today):
    """构建完整邮件正文，返回字符串。一次构建，不拼接局部变量。"""
    from scripts.overnight_market import get_overnight_analysis, format_overnight_report

    body = ""

    # 1. 隔夜美盘
    try:
        ov = get_overnight_analysis()
        body += format_overnight_report(ov) + "\n\n"
    except Exception:
        pass

    # 2. 万泰生物补仓
    body += _build_stock_watch("603392", "万泰生物", 63.157,
        [("缩量", "日成交<5000万且<均量50%", lambda cur,cl,am: _vol_shrink(am)),
         ("阳线", lambda cur,cl,am: "收盘>" + f"{cl.iloc[-2]:.2f}" if len(cl)>=2 else "?", lambda cur,cl,am: cur > cl.iloc[-2] if len(cl)>=2 else False),
         ("站上MA10", lambda cur,cl,am: f"现价{cur:.2f} vs MA10 {cl.iloc[-10:].mean():.2f}", lambda cur,cl,am: cur > cl.iloc[-10:].mean())],
        "补仓", today)

    # 3. 华安证券建仓
    body += _build_stock_watch("600909", "华安证券", None,
        [("回调至MA10", lambda cur,cl,am: f"现价{cur:.2f} vs MA10{cl.iloc[-10:].mean():.2f}, 距高点{_pullback_pct(cl):+.1f}%", lambda cur,cl,am: abs(cur - cl.iloc[-10:].mean())/cl.iloc[-10:].mean() < 0.03),
         ("缩量", "", lambda cur,cl,am: _vol_shrink_big(am)),
         ("阳线", "", lambda cur,cl,am: cur > cl.iloc[-2] if len(cl)>=2 else False)],
        "建仓", today)

    # 4. QMT持仓
    try:
        from scripts.qmt_sync import get_qmt_summary, format_qmt_report
        qd = get_qmt_summary()
        if qd.get("available"):
            body += format_qmt_report(qd) + "\n\n"
    except Exception:
        pass

    # 5. 异动检测
    try:
        from scripts.ml_signals import AnomalyDetector
        ad = AnomalyDetector()
        meta = load_meta("csi800")
        ad.fit(sorted(meta["code"].tolist())[:200], today)
        anoms = []
        for r in results:
            if r.get("action") in ("skip", "nodata", "sell"): continue
            is_a, score, desc = ad.detect(r["code"], today)
            if is_a:
                anoms.append(f"    {r['code']} {r['name']}: {desc}")
        if anoms:
            body += "🔍 持仓异动检测(ML)\n" + "\n".join(anoms) + "\n\n"
        else:
            body += "🔍 持仓异动检测(ML)\n  今日无异常信号\n\n"
    except Exception:
        pass

    # 6. 板块轮动
    try:
        from scripts.ml_signals import sector_rotation_heatmap, format_rotation_report
        c8 = load_meta("csi800"); codes8 = sorted(c8["code"].tolist())
        from scripts.run_backtest_a import load_panels
        pr, _ = load_panels(codes8, (pd.Timestamp(today)-pd.Timedelta(days=350)).strftime("%Y-%m-%d"), today)
        if not pr.empty:
            info_r = load_meta("stock_info_full")
            dfr = sector_rotation_heatmap(pr, info_r, today)
            body += format_rotation_report(dfr) + "\n\n"
    except Exception:
        pass

    # 7. 多维综合表(基本面+资金流+信号+ML)
    try:
        body += _build_multi_dim_table(results, today)
    except Exception:
        pass

    # 8. 个人持仓日报(分类表格)
    body += _build_holdings_table(results, analyzer, today)

    return body


def _build_multi_dim_table(results, today):
    """
    多维综合表: 每只持仓交叉 基本面 + 资金流 + 信号 + ML + 过热
    返回邮件正文段落
    """
    import json, requests, re
    from pathlib import Path

    # 信号
    sig = {}
    try:
        sf = Path("data_store/meta/signal_a_latest.json")
        if sf.exists():
            sig = json.loads(sf.read_text(encoding="utf-8"))
    except: pass
    sig_pool = set(sig.get("holdings", []))
    sig_buy = set(sig.get("buy", []))
    sig_sell = set(sig.get("sell", []))

    # 基本面
    fq = None
    try:
        fq = load_meta("financial_quarterly")
        fq = fq.sort_values("report_date").groupby("code").last()
    except: pass

    # ML
    try:
        from scripts.ml_signals import AnomalyDetector
        c8 = load_meta("csi800")
        ad = AnomalyDetector()
        ad.fit(sorted(c8["code"].tolist())[:200], today)
    except:
        ad = None

    # 新浪盘口(批量)
    orderflow = {}
    actives = [r for r in results if r.get("action") not in ("skip","nodata")]
    if actives:
        codes_str = ",".join(
            f'{"sh" if r["code"].startswith("6") else "sz"}{r["code"]}'
            for r in actives)
        try:
            resp = requests.get(f'http://hq.sinajs.cn/list={codes_str}',
                headers={"Referer":"https://finance.sina.com.cn"}, timeout=10)
            for line in resp.text.split("\n"):
                m = re.search(r'var hq_str_\w+=([^\n]+)', line)
                if not m: continue
                parts = m.group(1).strip('"').split(",")
                if len(parts) < 30: continue
                # Extract code from first field (name)
                the_code = ""
                for r2 in actives:
                    sid = f'sh{r2["code"]}' if r2["code"].startswith("6") else f'sz{r2["code"]}'
                    if sid in line:
                        the_code = r2["code"]; break
                if not the_code: continue
                b1v = int(parts[12]) if len(parts)>12 and parts[12] and parts[12].isdigit() else 0
                s1v = int(parts[22]) if len(parts)>22 and parts[22] and parts[22].isdigit() else 0
                orderflow[the_code] = {"buy1": b1v, "sell1": s1v}
        except: pass

    lines = [
        "📊 多维综合表 — 基本面+资金+信号+ML",
        "─" * 85,
        f'{"代码":<8} {"名称":<6} {"盈亏":>7} {"EPS":>6} {"盘口":>12} {"量":<6} {"ML":<8} {"信号":<10} {"过热":>12}',
        "─" * 85,
    ]

    for r in actives:
        code = r["code"]; name = r.get("name","")
        pnl = r.get("pnl_pct",0) or 0

        # EPS
        eps_str = "?"
        if fq is not None and code in fq.index:
            e = fq.loc[code].get("eps", None)
            if e is not None and not pd.isna(e):
                eps_str = f"{e:.2f}"
                if e < 0: eps_str = f"⚠{e:.2f}"

        # Order flow
        flow = orderflow.get(code, {})
        b1 = flow.get("buy1",0); s1 = flow.get("sell1",0)
        if b1 and s1:
            ratio = b1 / max(s1,1)
            if ratio > 2: flow_s = "买强↑"
            elif ratio < 0.5: flow_s = "卖强↓"
            else: flow_s = "均衡→"
        else:
            flow_s = "—"

        # Volume
        vol_s = "—"
        try:
            df2 = load_daily(code,
                (date.today()-timedelta(days=60)).strftime("%Y-%m-%d"),
                today)
            if not df2.empty and "amount" in df2.columns:
                df2 = df2.sort_values("date")
                amt = pd.to_numeric(df2["amount"], errors="coerce").dropna()
                if len(amt) >= 21:
                    vr = amt.iloc[-1] / amt.iloc[-21:-1].mean()
                    if vr > 3.0: vol_s = f"高{vr:.1f}x"
                    elif vr > 1.5: vol_s = f"放{vr:.1f}x"
                    elif vr < 0.5: vol_s = "缩量"
                    else: vol_s = "正常"
        except: pass

        # ML
        ml_s = "—"
        if ad is not None:
            try:
                is_a, sc, desc = ad.detect(code, today)
                ml_s = "⚠异常" if is_a else "✓"
            except: pass

        # Signal
        sig_s = ""
        if code in sig_buy: sig_s = "★买入"
        elif code in sig_sell: sig_s = "✗卖出"
        elif code in sig_pool: sig_s = "池内"
        else: sig_s = "—"

        # Overbought
        hot = "—"
        try:
            df = load_daily(code,
                (date.today()-timedelta(days=60)).strftime("%Y-%m-%d"),
                today)
            if not df.empty:
                df = df.sort_values("date")
                cl = pd.to_numeric(df["close"], errors="coerce").dropna()
                if len(cl) >= 20:
                    cur_p = cl.iloc[-1]
                    ret20 = (cur_p / cl.iloc[-21] - 1)*100 if len(cl)>=21 else 0
                    cons = sum(1 for i in range(len(cl)-1,max(0,len(cl)-15),-1) if cl.iloc[i]>cl.iloc[i-1])
                    hi20 = cl.iloc[-20:].max()
                    dh = (cur_p/hi20-1)*100
                    if ret20 > 50: hot = f"🔥20日+{ret20:.0f}%"
                    elif ret20 > 30: hot = f"20日+{ret20:.0f}%"
                    elif cons >= 8: hot = f"连涨{cons}"
                    elif cons >= 5: hot = f"连涨{cons}"
                    elif dh > -1 and cons >= 2: hot = "近高"
        except: pass

        lines.append(
            f'{code:<8} {name:<6} {pnl:+6.1f}% {eps_str:>6} {flow_s:>12} {vol_s:<6} {ml_s:<8} {sig_s:<10} {hot:>12}')

    lines.append("─" * 85)
    lines.append("EPS:<0=亏损⚠ | 盘口:买强=主力接盘,卖强=主力出货 | ML:⚠=异动 | 信号:主策略对齐")
    lines.append("")
    return "\n".join(lines)


def _vol_shrink(amounts):
    if len(amounts) < 20: return False
    return amounts.iloc[-1] < amounts.iloc[-21:-1].mean() * 0.5 and amounts.iloc[-1] < 50000000

def _vol_shrink_big(amounts):
    if len(amounts) < 20: return False
    recent = amounts.iloc[-1]; avg20 = amounts.iloc[-21:-1].mean()
    peak = amounts.iloc[-10:].max()
    return recent < avg20 * 0.5 and recent < peak * 0.3

def _pullback_pct(closes):
    if len(closes) < 20: return 0
    high20 = closes.iloc[-20:].max()
    return (closes.iloc[-1] / high20 - 1) * 100 if high20 > 0 else 0


def _build_stock_watch(code, name, cost, checks, action_label, today):
    """构建单只追踪股票的监测段落。"""
    start = (date.today() - timedelta(days=30)).strftime("%Y-%m-%d")
    df = load_daily(code, start, today)
    if df.empty or len(df) < 10: return ""

    df = df.sort_values("date")
    closes = pd.to_numeric(df["close"], errors="coerce").dropna()
    amounts = pd.to_numeric(df["amount"], errors="coerce").dropna() if "amount" in df.columns else None
    cur = closes.iloc[-1]
    ma10 = closes.iloc[-10:].mean()

    check_strs = []
    met = 0
    for label, detail_fn, check_fn in checks:
        try:
            detail = detail_fn(cur, closes, amounts) if callable(detail_fn) else detail_fn
            passed = check_fn(cur, closes, amounts)
            check_strs.append(f"{label}: {'✅' if passed else '❌'}{(' (' + detail + ')' if detail else '')}")
            if passed: met += 1
        except Exception:
            check_strs.append(f"{label}: ❌")

    if met >= 3: signal = f"🟢 {action_label}条件满足!"
    elif met >= 2: signal = f"🟡 接近{action_label}({met}/3)"
    else: signal = f"⏸ 继续等待({met}/3)"

    cost_line = f" | 成本: ¥{cost:.3f} | 亏损: {(cur/cost-1)*100:.1f}%" if cost else ""
    lines = [
        f"🎯 {name}({code}) {action_label}监测",
        "─" * 36,
        f"  现价: ¥{cur:.2f}{cost_line}",
        f"  MA10: {ma10:.2f}",
        f"  {' | '.join(check_strs)}",
        f"  📌 {signal}",
        "─" * 36,
        "", "",
    ]
    return "\n".join(lines)


def _build_holdings_table(results, analyzer, today):
    """构建持仓分类表格。"""
    actives = [r for r in results if r.get("action") != "skip"]
    sell_list = [r for r in actives if r.get("action") == "sell"]
    cut_list = [r for r in actives if r.get("action") == "cut"]
    warn_list = [r for r in actives if r.get("action") in ("warn", "weakening")]
    add_list = [r for r in actives if r.get("add_signal") == "yes_add" and r.get("action") == "hold"]
    alert_hold = [r for r in actives if r.get("action") == "hold"
                  and r.get("add_signal") != "yes_add"
                  and (r.get("pred_details", {}).get("vol_pattern") == "distribution"
                       or r.get("pred_details", {}).get("at_high_20d", -1) > -0.03
                       or r.get("score_pct") is None)]
    normal_hold = [r for r in actives if r.get("action") == "hold"
                   and r not in add_list and r not in alert_hold]

    def pnl(r): return r.get("pnl_pct", 0) or 0
    def sp(r):
        v = r.get("score_pct")
        return f"{v:.0%}" if v is not None else "N/A"
    def st(r):
        return {"rising":"↑","falling":"↓","stable":"→","unknown":"?"}.get(r.get("score_trend","?"),"?")

    total_mv = sum(r.get("mktval", 0) or 0 for r in actives if r.get("action") != "nodata")
    pnl_total = sum((pnl(r)/100)*(r.get("mktval",0) or 0) for r in actives if r.get("mktval"))

    lines = [
        f"个人持仓日报 — {today}",
        f"市场: {analyzer.regime_label} | 市值: ¥{total_mv:,.0f} | 盈亏: {pnl_total:+,.0f}",
        "=" * 70, "",
    ]

    def _table(title, items, cols):
        if not items: return
        lines.append(title); lines.append("")
        lines.append("  " + "  ".join(f"{c:<{w}}" for c, w in cols))
        lines.append("  " + "-" * (sum(w for _,w in cols) + 2*(len(cols)-1)))
        for r in items:
            vals = []
            for c, w in cols:
                if c == "代码": vals.append(f"{r.get('code',''):<{w}}")
                elif c == "名称": vals.append(f"{r.get('name',''):<{w}}")
                elif c == "盈亏": vals.append(f"{pnl(r):+{w}.1f}%")
                elif c == "分位": vals.append(f"{sp(r):<{w}}")
                elif c == "趋势": vals.append(f"{st(r):<{w}}")
                elif c == "判断":
                    reason = r.get("reason",""); sug = r.get("suggestion","")
                    txt = f"{reason}。{sug}" if sug else reason
                    vals.append(f"{txt[:w]}")
            lines.append("  " + "  ".join(vals))
        lines.append("")

    _table("🔴 触发卖出", sell_list, [("代码",8),("名称",10),("盈亏",8),("分位",6),("判断",65)])
    _table("💀 减仓", cut_list, [("代码",8),("名称",10),("盈亏",8),("分位",6),("趋势",4),("判断",65)])
    if warn_list:
        _table("🔔 预警关注", warn_list, [("代码",8),("名称",10),("盈亏",8),("分位",6),("判断",65)])
    _table("✅ 可补仓/加仓", add_list, [("代码",8),("名称",10),("盈亏",8),("分位",6),("趋势",4),("判断",70)])
    _table("⚠️ 持有但需警惕", alert_hold, [("代码",8),("名称",10),("盈亏",8),("分位",6),("趋势",4),("判断",65)])
    _table("✅ 正常持有", normal_hold, [("代码",8),("名称",10),("盈亏",8),("分位",6),("趋势",4),("判断",65)])

    parts = []
    if sell_list: parts.append(f"🔴卖出{len(sell_list)}只")
    if cut_list: parts.append(f"💀减仓{len(cut_list)}只")
    if add_list: parts.append(f"✅可加仓{len(add_list)}只")
    if normal_hold: parts.append(f"✅持有{len(normal_hold)}只")
    lines.append("=" * 70)
    lines.append("  " + " | ".join(parts) if parts else "  ✅ 全部正常")
    lines.append("=" * 70)
    return "\n".join(lines)
