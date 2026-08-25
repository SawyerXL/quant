"""Intraday A-share market analysis — real-time indices, breadth, sectors."""
import sys, os
from pathlib import Path
from datetime import datetime, date, time

_project_root = Path(__file__).parent.parent.parent
os.chdir(_project_root)
sys.path.insert(0, str(_project_root))

# Key indices to track
WATCH_INDICES = {
    "sh000001": "上证指数",
    "sz399001": "深证成指",
    "sz399006": "创业板指",
    "sh000688": "科创50",
    "sh000300": "沪深300",
    "sh000905": "中证500",
}


def _is_trading_time() -> bool:
    """Check if currently within A-share trading hours."""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.time()
    return time(9, 0) <= t <= time(15, 0)


def get_intraday_analysis() -> dict:
    """Get intraday market analysis."""
    if not _is_trading_time():
        return {
            "is_trading": False,
            "message": "非交易时间，下一交易日9:00开始更新",
            "indices": [],
            "breadth": None,
        }

    try:
        import requests

        # 1. Fetch index data via Sina (more reliable than akshare)
        indices = []
        sina_map = {
            "sh000001": "s_sh000001", "sz399001": "s_sz399001",
            "sz399006": "s_sz399006", "sh000688": "s_sh000688",
            "sh000300": "s_sh000300", "sh000905": "s_sh000905",
        }
        ids = ",".join(sina_map.values())
        try:
            resp = requests.get(f"http://hq.sinajs.cn/list={ids}",
                headers={"Referer": "https://finance.sina.com.cn"}, timeout=8)
            resp.encoding = 'gb2312'
            for line in resp.text.strip().split('\n'):
                if '=' not in line: continue
                sid = line.split('=')[0].split('_')[-1]
                code = None
                for k, v in sina_map.items():
                    if v.endswith(sid): code = k; break
                if code is None: continue
                d = line.split('"')[1].split(',')
                if len(d) < 4: continue
                # Index format: name, current, change_amt, change_pct, volume, amount
                cur = float(d[1]) if d[1] else 0
                chg = float(d[3]) if d[3] else 0
                vol = float(d[4]) if len(d) > 4 and d[4] else 0
                indices.append({
                    "code": code, "name": WATCH_INDICES.get(code, sid),
                    "price": round(cur, 2), "chg_pct": round(chg, 2),
                    "volume": int(vol),
                })
        except Exception as e:
            pass  # Will show "暂无数据" if empty

        # 2. Market breadth — try akshare, fallback gracefully
        breadth = None
        try:
            import akshare as ak
            df_spot = ak.stock_zh_a_spot_em()
            if not df_spot.empty:
                up = int((df_spot["涨跌幅"] > 0).sum())
                down = int((df_spot["涨跌幅"] < 0).sum())
                flat = int((df_spot["涨跌幅"] == 0).sum())
                limit_up = int((df_spot["涨跌幅"] >= 9.8).sum())
                limit_down = int((df_spot["涨跌幅"] <= -9.8).sum())
                breadth = {
                    "up": up, "down": down, "flat": flat,
                    "total": up + down + flat,
                    "limit_up": limit_up, "limit_down": limit_down,
                    "up_ratio": round(up / max(up + down, 1) * 100, 1),
                }
        except Exception:
            pass  # breadth is optional

        # 3. Market direction summary
        sh = next((i for i in indices if i["code"] == "sh000001"), None)
        cy = next((i for i in indices if i["code"] == "sz399006"), None)
        kc = next((i for i in indices if i["code"] == "sh000688"), None)

        direction = "震荡"
        strength = ""
        if sh:
            if sh["chg_pct"] > 1.5: direction = "强势上涨"; strength = "强"
            elif sh["chg_pct"] > 0.3: direction = "小幅上涨"; strength = "偏强"
            elif sh["chg_pct"] < -1.5: direction = "明显下跌"; strength = "弱"
            elif sh["chg_pct"] < -0.3: direction = "小幅下跌"; strength = "偏弱"

        # Key observations
        observations = []
        if sh and abs(sh["chg_pct"]) > 1:
            observations.append(f"上证{sh['chg_pct']:+.1f}%，波动较大")
        if cy and kc:
            if cy["chg_pct"] * kc["chg_pct"] < 0:
                observations.append("创业板与科创50分化，结构性行情")
        if breadth:
            if breadth["up_ratio"] > 70:
                observations.append(f"普涨格局（{breadth['up_ratio']}%个股上涨）")
            elif breadth["up_ratio"] < 30:
                observations.append(f"普跌格局（{breadth['up_ratio']}%个股上涨）")
            if breadth["limit_up"] > 80:
                observations.append(f"涨停{breadth['limit_up']}家，情绪高涨")
            if breadth["limit_down"] > 30:
                observations.append(f"跌停{breadth['limit_down']}家，恐慌蔓延")

        return {
            "is_trading": True,
            "direction": direction if indices else "数据加载中",
            "strength": strength,
            "observations": observations,
            "indices": indices,
            "breadth": breadth,
            "updated_at": datetime.now().strftime("%H:%M:%S"),
        }

    except Exception as e:
        return {
            "is_trading": _is_trading_time(),
            "error": str(e),
            "indices": [],
            "breadth": None,
        }


# ═══ 开盘半小时量能分析（修正版框架，2026-08回测验证）═══

OPENING_WATCH_STOCKS = {
    "600276": "恒瑞医药", "603259": "药明康德", "600030": "中信证券",
    "603019": "中科曙光", "002409": "雅克科技", "601899": "紫金矿业",
    "603993": "洛阳钼业", "588000": "科创50ETF",
}

# 开盘量能分类阈值（基于44万样本回测）
GAP_UP_H = 1.0     # 高开阈值%
GAP_DN_H = -1.0    # 低开阈值%
VOL_HEAVY = 1.5    # 放量: 30分钟量 > 5日均量×1.5
VOL_LIGHT = 0.7    # 缩量: 30分钟量 < 5日均量×0.7


def _estimate_opening_volume(code: str) -> dict | None:
    """估算开盘半小时量能。新浪不提供分钟数据，用开盘缺口+实时量比近似。"""
    import requests
    try:
        sid = f"{'sh' if code.startswith(('6','5','9','11')) else 'sz'}{code}"
        resp = requests.get(f"http://hq.sinajs.cn/list={sid}",
            headers={"Referer": "https://finance.sina.com.cn"}, timeout=5)
        resp.encoding = 'gb2312'
        d = resp.text.split('"')[1].split(',')
        if len(d) < 10 or not d[3]:
            return None
        return {
            "open": float(d[1]), "prev_close": float(d[2]),
            "cur": float(d[3]), "high": float(d[4]), "low": float(d[5]),
            "volume": float(d[8]) if d[8] else 0,  # 成交量(股)
            "name": d[0],
        }
    except Exception:
        return None


def get_opening30_analysis() -> dict:
    """开盘半小时选股扫描：分析TOP60池，找出值得入手的票。"""
    now = datetime.now()
    t = now.time()
    if now.weekday() >= 5 or not (time(9, 30) <= t <= time(11, 30)):
        return {"visible": False, "message": "开盘半小时分析仅在9:30-11:30显示"}

    from data.storage import load_daily, load_meta
    import pandas as pd

    # 1. Build TOP60 pool (20d avg turnover over CSI800)
    pool_codes = []
    try:
        c800 = load_meta('csi800')
        codes = sorted(c800['code'].astype(str).tolist())
        amounts = {}
        for code in codes:
            df = load_daily(code, '2026-06-15', str(date.today()))
            if len(df) < 20: continue
            amt = pd.to_numeric(df['amount'], errors='coerce').dropna()
            if len(amt) >= 20:
                amounts[code] = amt.iloc[-20:].mean()
        pool_codes = sorted(amounts, key=amounts.get, reverse=True)[:60]
    except Exception:
        pool_codes = []

    results = []
    for code in pool_codes:
        rt = _estimate_opening_volume(code)
        if not rt: continue
        cur = rt["cur"]; prev_c = rt["prev_close"]; open_p = rt["open"]
        if prev_c <= 0 or open_p <= 0: continue
        gap = (open_p / prev_c - 1) * 100
        chg = (cur / prev_c - 1) * 100

        vol_ratio = 1.0
        try:
            df = load_daily(code, '2026-07-01', str(date.today()))
            if not df.empty:
                df['date'] = pd.to_datetime(df['date'])
                df = df.set_index('date').sort_index()
                vo = pd.to_numeric(df.get('volume', pd.Series(dtype=float)), errors='coerce').dropna()
                if len(vo) >= 5 and vo.iloc[-5:].mean() > 0:
                    vol_ratio = rt["volume"] / vo.iloc[-5:].mean()
        except Exception:
            pass

        # MA10 distance for entry check
        dist_ma10 = None
        try:
            df = load_daily(code, '2026-06-15', str(date.today()))
            if not df.empty:
                df['date'] = pd.to_datetime(df['date'])
                df = df.set_index('date').sort_index()
                cl = pd.to_numeric(df['close'], errors='coerce').dropna()
                if len(cl) >= 11:
                    ma10 = cl.iloc[-10:].mean()
                    dist_ma10 = round((cur / ma10 - 1) * 100, 2)
        except Exception:
            pass

        # 买入信号分类（结合开盘+MA10位置）
        if gap < GAP_DN_H and dist_ma10 is not None and -3 <= dist_ma10 <= 2:
            sig = "🟢 回调到位"
            advice = f"低开+MA10附近({dist_ma10:+.1f}%)，可关注介入"
            tone = "buy"
        elif gap > GAP_UP_H and vol_ratio > VOL_HEAVY and dist_ma10 is not None and dist_ma10 <= 3:
            sig = "🟡 强势启动"
            advice = "高开放量+MA10上方，可轻仓跟"
            tone = "up"
        elif dist_ma10 is not None and -3 <= dist_ma10 <= 2 and vol_ratio > 1.5:
            sig = "🟢 放量MA10"
            advice = "MA10附近放量，值得跟踪"
            tone = "watch"
        elif gap > 5:
            sig = "🔴 高开过多"
            advice = "高开超5%，不追"
            tone = "dn"
        else:
            continue  # 无信号的不展示

        results.append({
            "code": code, "name": rt.get("name", ""),
            "price": round(cur, 2), "chg_pct": round(chg, 2),
            "gap_pct": round(gap, 2), "vol_ratio": round(vol_ratio, 2),
            "dist_ma10": dist_ma10,
            "signal": sig, "advice": advice, "tone": tone,
        })

    # Sort: buy signals first
    tone_order = {"buy": 0, "up": 1, "watch": 2}
    results.sort(key=lambda x: tone_order.get(x["tone"], 9))

    # 大盘
    sh = _estimate_opening_volume("000001")
    market = None
    if sh:
        gap_sh = (sh["open"] / sh["prev_close"] - 1) * 100 if sh["prev_close"] else 0
        chg_sh = (sh["cur"] / sh["prev_close"] - 1) * 100 if sh["prev_close"] else 0
        if gap_sh > 1: sh_sig = "高开"
        elif gap_sh < -1: sh_sig = "低开"
        else: sh_sig = "平开"
        market = {"signal": sh_sig, "chg_pct": round(chg_sh, 2), "gap_pct": round(gap_sh, 2)}

    return {
        "visible": True,
        "market": market,
        "stocks": results[:15],
        "updated_at": now.strftime("%H:%M:%S"),
        "framework_note": "TOP60池开盘扫描：低开+MA10附近=关注介入 · 高开放量=轻仓跟 · 高开超5%不追",
    }
