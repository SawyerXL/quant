"""Market analysis service — overnight + technical + outlook."""
import sys, os
from pathlib import Path
from datetime import date, datetime

_project_root = Path(__file__).parent.parent.parent
os.chdir(_project_root)
sys.path.insert(0, str(_project_root))
sys.path.insert(0, str(_project_root / "scripts"))


# Cache: {timestamp, data}
_cache: dict = {"ts": 0, "data": None}
_CACHE_TTL = 300  # 5 minutes


def get_market_analysis(force: bool = False) -> dict:
    """Full market analysis: overnight, technical, news, and outlook.
    Time-aware: morning = overnight + prediction; afternoon = today's recap + tomorrow outlook.
    Cached for 5 minutes to avoid slow akshare calls on every page load.
    """
    import time
    now = time.time()
    if not force and _cache["data"] and (now - _cache["ts"]) < _CACHE_TTL:
        return _cache["data"]

    h = datetime.now().hour
    is_weekend = datetime.now().weekday() >= 5
    is_afternoon = h >= 15 and not is_weekend

    result = {
        "date": str(date.today()),
        "is_afternoon": is_afternoon,
        "is_weekend": is_weekend,
        "news": _get_news(),
        "technical": _get_technical_outlook(),
        "key_levels": _get_key_levels(),
        "outlook": {},
        "summary": "",
    }

    if is_afternoon:
        # Post-market: show today's recap, skip overseas
        result["overseas"] = {}
        result["today_recap"] = _get_today_recap(result)
        result["outlook"] = _generate_afternoon_outlook(result)
    else:
        # Morning or weekend: overseas lead + prediction
        result["overseas"] = _get_overseas()
        result["outlook"] = _generate_outlook(result)
        if is_weekend:
            # Weekend recap: label for Monday reference
            result["outlook"]["weekend_note"] = "周末复盘：外盘数据为周五收盘，供下周一参考"

    result["summary"] = _generate_summary(result)
    _cache["ts"] = now
    _cache["data"] = result
    return result


def _get_today_recap(analysis: dict) -> dict:
    """Today's market recap using actual daily close data."""
    tech = analysis.get("technical", {})
    lv = analysis.get("key_levels", {})

    # Get actual close from Sina and update tech dict so predictions use it too
    sh_close = tech.get("sh_close", 0)
    try:
        import requests
        resp = requests.get("http://hq.sinajs.cn/list=s_sh000001",
            headers={"Referer": "https://finance.sina.com.cn"}, timeout=3)
        resp.encoding = 'gb2312'
        d = resp.text.split('"')[1].split(',')
        live_price = float(d[1]) if d[1] else None
        if live_price and live_price > 0:
            sh_close = live_price
            tech["sh_close"] = sh_close  # update for prediction
            # Re-check MA20 position with new close
            if tech.get("sh_ma20"):
                tech["sh_above_ma20"] = sh_close > tech["sh_ma20"]
    except Exception:
        pass
    days_below = tech.get("days_below_ma200", 0)
    margin_chg = tech.get("margin_chg_5d", 0)
    margin_days = tech.get("margin_cons_days", 0)
    dd_52w = tech.get("dd_52w", 0)

    # Build summary from actual daily data
    parts = [f"上证收{sh_close:.0f}"]
    if dd_52w:
        parts.append(f"52周回撤{dd_52w:.1f}%")
    if margin_chg > 0:
        parts.append(f"融资回升{margin_chg:+.1f}%")
    elif margin_days > 0:
        parts.append(f"融资连降{margin_days}天")

    # Trend assessment
    if days_below >= 20:
        parts.append(f"MA200下方{days_below}天（熊市结构）")
    elif days_below >= 5:
        parts.append(f"MA200下方{days_below}天")

    # Distance to MA200
    lv_cur = lv.get("current")
    resistances = lv.get("resistance", [])
    for r in resistances:
        if r.get("label") == "MA200":
            dist = r["level"] - sh_close
            if dist > 0:
                parts.append(f"距MA200还有{dist:.0f}点")
            break

    date_label = analysis.get("date", "")[:10]
    return {
        "today_line": (date_label + "收盘：" if date_label else "最新收盘：") + " | ".join(parts),
        "sh_close": sh_close,
        "days_below_ma200": days_below,
        "margin_chg_5d": margin_chg,
    }


def _generate_afternoon_outlook(analysis: dict) -> dict:
    """Post-market outlook: today recap + tomorrow prediction."""
    recap = analysis.get("today_recap", {})
    tech = analysis.get("technical", {})
    lv = analysis.get("key_levels", {})

    # Today's performance summary
    today_line = recap.get("today_line", "数据收集中")

    # Tomorrow outlook (reuse prediction logic)
    direction, confidence, factors = _predict_direction(analysis)

    # Key levels for tomorrow
    cur = lv.get("current")
    supports = lv.get("support", [])
    resistances = lv.get("resistance", [])
    levels = ""
    if cur and supports and resistances:
        s1, r1 = supports[0], resistances[0]
        levels = f"明日关键区间：{s1['label']} {s1['level']:.0f} — {r1['label']} {r1['level']:.0f}"

    # Suggestion
    days_below = tech.get("days_below_ma200", 0)
    if days_below >= 20:
        suggestion = "熊市结构未改，建议防御为主，仓位不超过50%"
    elif days_below >= 5:
        suggestion = "等待MA200收复确认，控制仓位在60%以内"
    else:
        suggestion = "正常操作，关注个股策略信号"

    return {
        "today": today_line,
        "direction": direction,
        "confidence": confidence,
        "factors": factors,
        "levels": levels,
        "suggestion": suggestion,
        "overnight": today_line,
        "technical": "上证" + str(tech.get("sh_close","?")) + "，MA200下方" + str(tech.get("days_below_ma200",0)) + "天，回撤" + format(tech.get("dd_52w",0),".1f") + "%",
    }


def _get_overseas() -> dict:
    """Overnight US market status."""
    try:
        from scripts.overnight_market import get_overnight_analysis
        ov = get_overnight_analysis()
        return {
            "sp500": ov.get("sp500"), "sp500_chg": ov.get("sp500_chg"),
            "nasdaq": ov.get("nasdaq"), "nasdaq_chg": ov.get("nasdaq_chg"),
            "dji": ov.get("dji"), "dji_chg": ov.get("dji_chg"),
            "vix": ov.get("vix"), "us10y": ov.get("us10y"),
            "sentiment": ov.get("sentiment", ""),
        }
    except Exception:
        return {"error": "Overseas data unavailable"}


def _get_news() -> list[dict]:
    """Latest financial news headlines relevant to A-shares."""
    try:
        import akshare as ak
        df = ak.stock_info_global_em()
        if df is None or df.empty:
            return []
        headlines = []
        # Take top 8, filter for relevance
        for _, row in df.head(20).iterrows():
            title = str(row.get("标题", ""))
            summary = str(row.get("摘要", ""))
            # Skip purely individual stock announcements
            skip_keywords = ["公告", "回购", "减持", "董事会", "股东大会", "业绩预告"]
            if any(kw in title for kw in skip_keywords):
                continue
            headlines.append({
                "title": title,
                "summary": summary[:120] if summary else "",
                "time": str(row.get("发布时间", ""))[:16],
            })
            if len(headlines) >= 8:
                break
        return headlines
    except Exception:
        return []
    """Technical summary for today."""
    try:
        import sys as _sys, io as _io
        from scripts.market_regime_monitor import run_all
        old_stdout = _sys.stdout
        _sys.stdout = _io.StringIO()
        try:
            _, result = run_all(quiet=True, json_out=False)
        finally:
            _sys.stdout = old_stdout

        sh_close = result.get("sh_close", 0)
        sh_dd = result.get("sh_dd_52w", 0)
        days_below = result.get("sh_days_below_ma200", 0)
        margin_days = result.get("margin_cons_days", 0)
        margin_chg = result.get("margin_chg_5d", 0)

        # Trend signals
        signals = []
        if days_below >= 20: signals.append("MA200下方超20天，熊市结构")
        elif days_below >= 5: signals.append(f"MA200下方{days_below}天，中期转弱")
        elif days_below > 0: signals.append(f"MA200下方{days_below}天，关注")

        if margin_days >= 5: signals.append(f"融资连降{margin_days}天，杠杆资金撤离")
        elif margin_chg < -3: signals.append("融资加速流出")

        if sh_dd < -15: signals.append(f"52周回撤{sh_dd:.0f}%，深度调整")
        elif sh_dd < -10: signals.append(f"52周回撤{sh_dd:.0f}%，技术性修正")

        return {
            "sh_close": sh_close,
            "days_below_ma200": days_below,
            "dd_52w": sh_dd,
            "margin_cons_days": margin_days,
            "margin_chg_5d": margin_chg,
            "signals": signals,
        }
    except Exception:
        return {"error": "Technical data unavailable"}


def _get_technical_outlook() -> dict:
    """Technical summary for today."""
    try:
        import sys as _sys, io as _io
        from scripts.market_regime_monitor import run_all
        old_stdout = _sys.stdout
        _sys.stdout = _io.StringIO()
        try:
            _, result = run_all(quiet=True, json_out=False)
        finally:
            _sys.stdout = old_stdout

        sh_close = result.get("sh_close", 0)
        sh_dd = result.get("sh_dd_52w", 0)
        days_below = result.get("sh_days_below_ma200", 0)
        margin_days = result.get("margin_cons_days", 0)
        margin_chg = result.get("margin_chg_5d", 0)

        signals = []
        if days_below >= 20: signals.append("MA200下方超20天，熊市结构")
        elif days_below >= 5: signals.append(f"MA200下方{days_below}天，中期转弱")
        elif days_below > 0: signals.append(f"MA200下方{days_below}天")

        if margin_days >= 5: signals.append(f"融资连降{margin_days}天")
        elif margin_chg < -3: signals.append("融资加速流出")

        if sh_dd < -15: signals.append(f"52周回撤{sh_dd:.0f}%深度调整")
        elif sh_dd < -10: signals.append(f"52周回撤{sh_dd:.0f}%技术修正")

        sh_ma20 = None; sh_above_ma20 = None
        try:
            from scripts.market_regime_monitor import analyze_index_technical
            idx_data = analyze_index_technical("sh000001", "上证指数", "major")
            sh_ma20 = float(idx_data.get("ma20")) if idx_data.get("ma20") else None
            if sh_ma20 and sh_close:
                sh_above_ma20 = bool(sh_close > sh_ma20)
        except Exception: pass

        return {
            "sh_close": float(sh_close) if sh_close else 0,
            "days_below_ma200": int(days_below) if days_below else 0,
            "dd_52w": float(sh_dd) if sh_dd else 0,
            "margin_cons_days": int(margin_days) if margin_days else 0,
            "margin_chg_5d": float(margin_chg) if margin_chg else 0,
            "signals": [str(s) for s in signals],
            "sh_ma20": float(sh_ma20) if sh_ma20 else None,
            "sh_above_ma20": bool(sh_above_ma20) if sh_above_ma20 is not None else None,
        }
    except Exception:
        return {"error": "Technical data unavailable"}


def _get_key_levels() -> dict:
    """Key support and resistance levels using 上证指数."""
    try:
        import pandas as pd
        from data.storage import load_daily

        # Load 上证指数 (code 000001) across recent years
        dfs = []
        for y in [2024, 2025, 2026]:
            df = load_daily('000001', f'{y}-01-01', f'{y}-12-31')
            if not df.empty: dfs.append(df)
        if not dfs:
            return {"error": "上证数据不可用"}

        sh = pd.concat(dfs)
        sh['date'] = pd.to_datetime(sh['date'])
        sh = sh.set_index('date').sort_index()
        close = sh['close'].dropna()

        if len(close) < 60:
            return {"error": "上证数据不足"}

        cur = float(close.iloc[-1])
        ma20 = float(close.rolling(20).mean().iloc[-1])
        ma60 = float(close.rolling(60).mean().iloc[-1])
        ma200 = float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else None
        low60 = float(close.iloc[-60:].min())
        high60 = float(close.iloc[-60:].max())

        # Build levels from all candidates, then classify by position relative to current price
        candidates = [
            (round(low60, 1), "60日低点"),
            (round(high60, 1), "60日高点"),
            (round(ma20, 1), "MA20"),
            (round(ma60, 1), "MA60"),
        ]
        if ma200 and ma200 > 0:
            candidates.append((round(ma200, 1), "MA200"))

        supports = [{"level": v, "label": l} for v, l in candidates if v < cur]
        resistances = [{"level": v, "label": l} for v, l in candidates if v > cur]
        supports.sort(key=lambda x: x["level"], reverse=True)
        resistances.sort(key=lambda x: x["level"])

        return {
            "current": round(cur, 1),
            "support": supports,
            "resistance": resistances,
        }
    except Exception:
        return {"error": "Levels unavailable"}


def _predict_direction(analysis: dict) -> tuple[str, str, list[str]]:
    """Predict today's likely market direction with transparent factor analysis."""
    ov = analysis.get("overseas", {})
    tech = analysis.get("technical", {})
    lv = analysis.get("key_levels", {})

    score = 0
    factors = []

    # Factor 1: Overnight US (weight: 2)
    sp_chg = ov.get("sp500_chg")
    nq_chg = ov.get("nasdaq_chg")
    if sp_chg is not None:
        if sp_chg > 1.5:
            score += 3; factors.append(f"🌍 隔夜S&P500大涨{sp_chg:+.1f}%，利多开盘 (+3)")
        elif sp_chg > 0.3:
            score += 1; factors.append(f"🌍 隔夜S&P500上涨{sp_chg:+.1f}%，偏多 (+1)")
        elif sp_chg < -1.5:
            score -= 3; factors.append(f"🌍 隔夜S&P500大跌{sp_chg:+.1f}%，利空开盘 (-3)")
        elif sp_chg < -0.3:
            score -= 1; factors.append(f"🌍 隔夜S&P500下跌{sp_chg:+.1f}%，偏空 (-1)")
        else:
            factors.append(f"🌍 隔夜美股平稳{sp_chg:+.1f}%，中性 (0)")

    # Factor 2: Technical position (weight: 2)
    days_below = tech.get("days_below_ma200", 0)
    dd_52w = tech.get("dd_52w", 0)
    sh_close = tech.get("sh_close", 0)

    # Check MA20 — short-term trend matters more than MA200 for daily direction
    above_ma20 = tech.get("sh_above_ma20")
    sh_ma20 = tech.get("sh_ma20")

    if above_ma20 and sh_ma20:
        score += 2; factors.append(f"📊 上证{sh_close:.0f}站上MA20({sh_ma20:.0f})，短期偏强 (+2)")
    elif days_below >= 20:
        score -= 3; factors.append(f"📊 MA200下方{days_below}天，中期偏弱 (-3)")
    elif days_below >= 5:
        score -= 1; factors.append(f"📊 MA200下方{days_below}天，短期承压 (-1)")
    else:
        factors.append(f"📊 均在MA200上方，技术面正常 (0)")

    if dd_52w < -15:
        score -= 1; factors.append(f"📊 52周回撤{dd_52w:.0f}%，深度调整 (-1)")

    # Factor 3: Margin flow (weight: 2)
    margin_chg = tech.get("margin_chg_5d", 0)
    margin_days = tech.get("margin_cons_days", 0)
    if margin_days >= 10:
        score -= 2; factors.append(f"💰 融资连降{margin_days}天，杠杆撤离 (-2)")
    elif margin_days >= 5:
        score -= 1; factors.append(f"💰 融资连降{margin_days}天，资金偏紧 (-1)")
    elif margin_chg > 3:
        score += 2; factors.append(f"💰 融资大幅回流{margin_chg:+.1f}% (+2)")
    elif margin_chg > 0.5:
        score += 1; factors.append(f"💰 融资回升{margin_chg:+.1f}%，资金回补 (+1)")
    elif margin_chg < -3:
        score -= 1; factors.append(f"💰 融资流出{margin_chg:+.1f}%，资金偏紧 (-1)")
    else:
        factors.append(f"💰 融资变化{margin_chg:+.1f}%，资金面平稳 (0)")

    # Factor 4: Support/resistance proximity (weight: 1)
    cur = lv.get("current")
    supports = lv.get("support", [])
    resistances = lv.get("resistance", [])
    if cur and supports:
        s1 = supports[0]["level"]
        dist_s = (cur - s1) / cur * 100
        if dist_s < 1.5:
            score += 1; factors.append(f"🎯 距支撑{s1:.0f}仅{dist_s:.1f}%，反弹概率大 (+1)")
        elif dist_s < 3:
            factors.append(f"🎯 距支撑{s1:.0f}有{dist_s:.1f}%，有一定支撑 (0)")
    if cur and resistances:
        r1 = resistances[0]["level"]
        dist_r = (r1 - cur) / cur * 100
        if dist_r < 1.5:
            score -= 1; factors.append(f"🎯 距阻力{r1:.0f}仅{dist_r:.1f}%，上涨空间受限 (-1)")

    # Determine direction from score (uses "明日" for afternoon, "今日" for morning)
    pre = "明日" if analysis.get("is_afternoon") else "今日"
    if score >= 5:
        direction = f"{pre}大概率强势上涨"
        confidence = "高"
    elif score >= 2:
        direction = f"{pre}偏多，收涨概率较大"
        confidence = "中"
    elif score >= -1:
        direction = f"{pre}窄幅震荡，方向不明"
        confidence = "低"
    elif score >= -4:
        direction = f"{pre}偏空，收跌概率较大"
        confidence = "中"
    else:
        direction = f"{pre}大概率弱势下跌"
        confidence = "高"

    return direction, confidence, factors
    """Generate a rich multi-part outlook for the morning briefing."""
    ov = analysis.get("overseas", {})
    tech = analysis.get("technical", {})
    lv = analysis.get("key_levels", {})

    # 1. Overnight impact
    overnight = ""
    sp_chg = ov.get("sp500_chg")
    nq_chg = ov.get("nasdaq_chg")
    if sp_chg is not None:
        impact = "偏多" if sp_chg > 0.5 else "偏空" if sp_chg < -0.5 else "中性"
        overnight = f"隔夜美股S&P 500 {sp_chg:+.2f}%，对A股开盘影响{impact}"
        if abs(sp_chg) > 2:
            overnight += "，波动较大需关注"
        if nq_chg is not None and abs(nq_chg) > 2:
            overnight += f"，纳斯达克{nq_chg:+.2f}%科技股波动明显"

    # 2. Technical position
    technical = ""
    days_below = tech.get("days_below_ma200", 0)
    dd_52w = tech.get("dd_52w", 0)
    if days_below >= 20:
        technical = f"上证已在MA200下方{days_below}天，中期趋势偏弱"
        if dd_52w < -15:
            technical += f"，52周回撤{dd_52w:.0f}%处深度调整区间"
    elif days_below >= 5:
        technical = f"上证MA200下方{days_below}天，短期承压但未确认熊市"
    else:
        technical = f"上证在MA200附近震荡，52周回撤{dd_52w:.1f}%"

    # 3. Key levels for today
    levels_text = ""
    cur = lv.get("current")
    supports = lv.get("support", [])
    resistances = lv.get("resistance", [])
    if cur:
        s1 = supports[0] if supports else None
        r1 = resistances[0] if resistances else None
        if s1 and r1:
            levels_text = f"今日关键区间：支撑{s1['level']:.0f}({s1['label']}) — 阻力{r1['level']:.0f}({r1['label']})"
            dist_to_support = (cur - s1['level']) / cur * 100
            dist_to_resist = (r1['level'] - cur) / cur * 100
            if dist_to_support < 2:
                levels_text += "，距支撑较近"
            elif dist_to_resist < 2:
                levels_text += "，距阻力较近"

    # 4. Sentiment & risks
    sentiment = ""
    margin_days = tech.get("margin_cons_days", 0)
    margin_chg = tech.get("margin_chg_5d", 0)
    signals = tech.get("signals", [])
    if margin_days >= 5:
        sentiment += f"融资连降{margin_days}天，杠杆资金持续撤离"
    elif margin_chg < -3:
        sentiment += f"融资5日变化{margin_chg:.1f}%，资金面偏紧"
    if signals:
        if sentiment: sentiment += "；"
        sentiment += "；".join(signals[:2])

    # 5. Directional prediction
    direction, confidence = _predict_direction(analysis)

    # 6. Actionable suggestion
    suggestion = ""
    if days_below >= 20:
        suggestion = "建议防御为主，仓位不超过50%，关注恐慌底+缩量的抄底信号"
    elif days_below >= 5:
        suggestion = "建议控制仓位在60%以内，等待MA200收复确认再加仓"
    else:
        suggestion = "正常操作，关注个股策略信号"

    return {
        "overnight": overnight,
        "technical": technical,
        "levels": levels_text,
        "sentiment": sentiment,
        "direction": direction,
        "confidence": confidence,
        "suggestion": suggestion,
    }


def _generate_outlook(analysis: dict) -> dict:
    """Generate a rich multi-part outlook for the morning briefing."""
    ov = analysis.get("overseas", {})
    tech = analysis.get("technical", {})
    lv = analysis.get("key_levels", {})

    # 1. Overnight impact
    sp_chg = ov.get("sp500_chg")
    nq_chg = ov.get("nasdaq_chg")
    overnight = ""
    if sp_chg is not None:
        impact = "偏多" if sp_chg > 0.5 else "偏空" if sp_chg < -0.5 else "中性"
        overnight = f"隔夜美股S&P 500 {sp_chg:+.2f}%，对A股开盘影响{impact}"
        if abs(sp_chg) > 2:
            overnight += "，波动较大需关注"
        if nq_chg is not None and abs(nq_chg) > 2:
            overnight += f"，纳斯达克{nq_chg:+.2f}%科技股波动明显"

    # 2. Technical position
    days_below = tech.get("days_below_ma200", 0)
    dd_52w = tech.get("dd_52w", 0)
    if days_below >= 20:
        technical = f"上证已在MA200下方{days_below}天，中期趋势偏弱"
        if dd_52w < -15: technical += f"，52周回撤{dd_52w:.0f}%处深度调整区间"
    elif days_below >= 5:
        technical = f"上证MA200下方{days_below}天，短期承压但未确认熊市"
    else:
        technical = f"上证在MA200附近震荡，52周回撤{dd_52w:.1f}%"

    # 3. Key levels
    cur = lv.get("current")
    supports = lv.get("support", [])
    resistances = lv.get("resistance", [])
    levels_text = ""
    if cur and supports and resistances:
        s1, r1 = supports[0], resistances[0]
        levels_text = f"今日关键区间：支撑{s1['level']:.0f}({s1['label']}) — 阻力{r1['level']:.0f}({r1['label']})"
        if (cur - s1['level']) / cur * 100 < 2: levels_text += "，距支撑较近"
        elif (r1['level'] - cur) / cur * 100 < 2: levels_text += "，距阻力较近"

    # 4. Sentiment
    margin_days = tech.get("margin_cons_days", 0)
    margin_chg = tech.get("margin_chg_5d", 0)
    signals = tech.get("signals", [])
    parts = []
    if margin_days >= 5: parts.append(f"融资连降{margin_days}天，杠杆资金持续撤离")
    elif margin_chg < -3: parts.append(f"融资5日变化{margin_chg:.1f}%，资金面偏紧")
    parts.extend(signals[:2])
    sentiment = "；".join(parts) if parts else ""

    # 5. Direction prediction
    direction, confidence, factors = _predict_direction(analysis)

    # 6. Suggestion
    if days_below >= 20:
        suggestion = "建议防御为主，仓位不超过50%，关注恐慌底+缩量的抄底信号"
    elif days_below >= 5:
        suggestion = "建议控制仓位在60%以内，等待MA200收复确认再加仓"
    else:
        suggestion = "正常操作，关注个股策略信号"

    return {
        "overnight": overnight, "technical": technical, "levels": levels_text,
        "sentiment": sentiment, "direction": direction, "confidence": confidence,
        "factors": factors, "suggestion": suggestion,
    }


def _generate_summary(analysis: dict) -> str:
    """Generate a concise one-line summary."""
    if analysis.get("is_afternoon"):
        recap = analysis.get("today_recap", {})
        return recap.get("today_line", "") + "。"

    outlook = _generate_outlook(analysis)
    parts = [outlook["overnight"], outlook["technical"]]
    if outlook.get("sentiment"):
        parts.append(outlook["sentiment"])
    return "。".join(p for p in parts if p) + "。"
