"""QDII 溢价日检（2026-10-21 扩到全美股宽基）。

规则（美股腿入场前置）：IOPV/估算溢价 ≤5% 才买；>5% 不买、等待。
数据：
  - 单券 NAV 口径：最新单位净值 ak.fund_etf_fund_daily_em + 收盘价
    ak.fund_etf_hist_sina（T+1 滞后，标注日期）。
  - 全市场实时 IOPV 扫描：ak.fund_etf_spot_em 的"基金折价率"（负=溢价），
    覆盖标普500/美国50/纳斯达克100/纳指宽基，剔除行业/主题（生物/科技/
    油气/消费/红利/A股）。找**最低溢价载体**——2026-10-21 实测最低是
    513650 标普500ETF南方 6.84%、159655 6.97%、159612 7.92%，
    而 513100 是 18.88%；同样买美股，载体差 12pp。
用法：python scripts/qdii_premium_monitor.py
"""
import json
import re
import time
from datetime import date
from pathlib import Path

import pandas as pd
import requests
import akshare as ak
from loguru import logger

HEADERS = {"User-Agent": "Mozilla/5.0"}

CODES = {"513100": "sh513100", "159941": "sz159941", "513500": "sh513500",
         "513650": "sh513650", "159655": "sz159655",
         "159513": "sz159513", "513110": "sh513110",
         # 2026-10-22: DJIA 腿载体，溢价 2.62%、ADV 3.26亿（N=72 PASS）
         "513400": "sh513400"}
THRESH = 0.05
# watch-only: 记录溢价、不触发买入告警（US track/5% 红线未解决）
WATCH_ONLY = {"513400"}
OUT = Path("logs/qdii_premium_monitor.json")


def scan_us_broad():
    """全市场实时 IOPV 扫描：美股宽基最低溢价。负折价率=溢价。

    2026-10-22: 东财 fund_etf_spot_em 偶发 RemoteDisconnected(18:30 实测),
    akshare 3 次退避失败后回落到 Tencent 单券 IOPV(CODES 8 只), 避免整段
    us_scan 缺失; 仍失败才返回 err(健康行 us_scan_err)。
    """
    err = None
    for attempt in range(3):
        try:
            d = ak.fund_etf_spot_em()
            d["代码"] = d["代码"].astype(str).str.zfill(6)
            m = d[d["名称"].astype(str).str.contains(
                "标普500|美国50|纳斯达克100|纳指|道琼斯", na=False)].copy()
            m = m[~m["名称"].astype(str).str.contains(
                "生物|科技|油气|消费|红利|A股", na=False)]
            m["premium_pct"] = -pd.to_numeric(m["基金折价率"], errors="coerce")
            m = m.dropna(subset=["premium_pct"]).sort_values("premium_pct")
            cols = [c for c in ["代码", "名称", "最新价", "IOPV实时估值",
                                "premium_pct", "成交额"] if c in m.columns]
            return m[cols].head(20).to_dict("records")
        except Exception as e:  # noqa: BLE001
            err = e
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    fallback = []
    for code in CODES:
        try:
            q = tencent_quote(code)
            if q.get("premium_pct") is not None:
                fallback.append({"代码": code, "名称": code,
                                 "最新价": q["price"],
                                 "premium_pct": q["premium_pct"],
                                 "source": "tencent_fallback"})
        except Exception:
            continue
    if fallback:
        logger.warning(f"us_scan: 东财失败({err}), 回落 Tencent {len(fallback)} 只")
        fallback.sort(key=lambda x: x["premium_pct"])
        return fallback
    return [{"err": str(err)[:150]}]


def tencent_quote(code):
    """Tencent 实时行情：3=现价, 77=溢价率%, 78=IOPV, 81=净值。

    2026-10-22 新增：执行闸门要的是**当日实时 IOPV 溢价**，不是 T-1 NAV。
    东财 push2 实时扫描（fund_etf_spot_em）在本机常 RemoteDisconnected，
    Tencent 单券报价可用。
    """
    pre = "sh" if code.startswith("5") else "sz"
    r = requests.get(f"https://qt.gtimg.cn/q={pre}{code}",
                     headers=HEADERS, timeout=8)
    txt = r.text.split('="', 1)[1].rstrip('";\n')
    f = txt.split("~")
    if len(f) < 82:
        raise RuntimeError("Tencent 字段不足")
    px = float(f[3])
    iopv = float(f[78]) if f[78] not in ("", "0", "0.0") else None
    if iopv and iopv > 0:
        prem = (px / iopv - 1) * 100
    elif f[77] not in ("", "0", "0.0"):
        prem = float(f[77])
    else:
        prem = None
    ts = next((x for x in f if len(x) == 14 and x.isdigit()), "")
    return {"price": px,
            "prev_close": float(f[4]) if f[4] not in ("", "0", "0.0") else None,
            "iopv": iopv,
            "premium_pct": round(prem, 2) if prem is not None else None,
            "nav": float(f[81]) if f[81] not in ("", "0", "0.0") else None,
            "time": ts}


def nav_series(code):
    """单位净值序列（天天基金 pingzhongdata）。ETF 日净值表缺 QDII 行时兜底。"""
    txt = requests.get(f"https://fund.eastmoney.com/pingzhongdata/{code}.js",
                       headers=HEADERS, timeout=20).text
    m = re.search(r"Data_netWorthTrend\s*=\s*(\[.*?\]);", txt)
    if not m:
        raise RuntimeError("Data_netWorthTrend 未找到")
    rows = []
    for x in json.loads(m.group(1)):
        try:
            ts = (pd.Timestamp(x["x"], unit="ms", tz="UTC")
                  .tz_convert("Asia/Shanghai").tz_localize(None).normalize())
            v = float(x["y"])
        except Exception:  # noqa: BLE001
            continue
        if v > 0:
            rows.append((ts, v))
    if not rows:
        raise RuntimeError("净值序列为空")
    return pd.Series(dict(rows)).sort_index()


def latest_navs():
    d = ak.fund_etf_fund_daily_em()
    d["基金代码"] = d["基金代码"].astype(str).str.zfill(6)
    navcols = [c for c in d.columns if "单位净值" in c]
    out = {}
    for code in CODES:
        row = d[d["基金代码"] == code]
        if row.empty:
            continue
        nav = None
        navdt = None
        for c in navcols:
            v = row.iloc[0][c]
            try:
                v = float(v)
            except Exception:
                continue
            if v > 0:
                nav = v
                navdt = c.split("-单位净值")[0]
                break
        out[code] = {"nav": nav, "nav_date": navdt}
    return out


def subscription_usable():
    """QDII 联接/指数基金：开放申购或限大额但日额度>=1000元，才算有可用通道。"""
    try:
        d = ak.fund_purchase_em()
        m = d[d["基金简称"].astype(str).str.contains(
            "纳斯达克|纳指|标普500", na=False)].copy()
        m["日累计限定金额"] = pd.to_numeric(m["日累计限定金额"], errors="coerce")
        ok = m[(m["申购状态"] == "开放申购") |
               ((m["申购状态"] == "限大额") & (m["日累计限定金额"] >= 1000))]
        return ok[["基金代码", "基金简称", "申购状态",
                   "日累计限定金额", "手续费"]].to_dict("records")
    except Exception as e:
        return [{"err": str(e)[:150]}]


def main():
    try:
        navs = latest_navs()
    except Exception as e:  # noqa: BLE001
        navs = {}
        logger.warning(f"ETF净值日表拉取失败: {e}")
    res = {"checked_at": date.today().isoformat(), "threshold": THRESH,
           "etfs": {}, "alert": False, "data_gaps": [],
           "watch_only": sorted(WATCH_ONLY), "watch_hits": [], "watch_scan": [],
           "subscription_usable": subscription_usable()}
    for code, sym in CODES.items():
        try:
            d = ak.fund_etf_hist_sina(symbol=sym)
            d["date"] = pd.to_datetime(d["date"])
            d = d.sort_values("date")
            px_s = pd.Series(d["close"].astype(float).values, index=d["date"])
            try:
                nv_s = nav_series(code)
                nav_src = "eastmoney_pingzhongdata.Data_netWorthTrend"
            except Exception:  # noqa: BLE001
                n = navs.get(code, {})
                if not n.get("nav"):
                    raise
                nvdt = pd.to_datetime(str(n["nav_date"])[:10], errors="coerce")
                nv_s = pd.Series({nvdt: float(n["nav"])})
                nav_src = "fund_etf_fund_daily_em"
            # 同日对齐: 净值 T+1, 价格 T 时仅用最近共同日, 禁止跨日比价
            common = px_s.index.intersection(nv_s.index)
            if not len(common):
                raise RuntimeError("净值与价格无共同交易日")
            last = common.max()
            px, nav = float(px_s.loc[last]), float(nv_s.loc[last])
            prem = px / nav - 1
            # 实时 IOPV 优先(P0 执行闸门); T-1 NAV 作兜底/留痕
            rt = None
            try:
                rt = tencent_quote(code)
            except Exception:  # noqa: BLE001
                pass
            rt_prem = (rt or {}).get("premium_pct")
            gate_prem = (rt_prem / 100.0) if rt_prem is not None else prem
            hit = bool(gate_prem <= THRESH)
            watch = code in WATCH_ONLY
            res["etfs"][code] = {
                "price": px, "price_date": str(last.date()),
                "nav": nav, "nav_date": str(last.date()), "nav_source": nav_src,
                "premium_pct": round(prem * 100, 2),
                "rt_price": (rt or {}).get("price"),
                "rt_iopv": (rt or {}).get("iopv"),
                "rt_premium_pct": rt_prem,
                "rt_time": (rt or {}).get("time"),
                "gate_premium_pct": round(gate_prem * 100, 2),
                "buy_ok": bool(hit and not watch),
                "watch_hit": bool(hit and watch)}
            if hit and watch:
                res["watch_hits"].append(code)
            elif hit:
                res["alert"] = True
        except Exception as e:  # noqa: BLE001
            res["etfs"][code] = {"err": str(e)[:150]}
            res["data_gaps"].append(code)
    res["us_scan"] = scan_us_broad()
    res["us_scan_err"] = bool(res["us_scan"] and res["us_scan"][0].get("err"))
    scan_hits = [r for r in res["us_scan"]
                 if isinstance(r.get("premium_pct"), (int, float))
                 and r["premium_pct"] <= THRESH * 100]
    scan_ok = [r for r in scan_hits if r.get("代码") not in WATCH_ONLY]
    res["watch_scan"] = [r for r in scan_hits if r.get("代码") in WATCH_ONLY]
    res["watch_hits"] += [r["代码"] for r in res["watch_scan"]]
    if scan_ok:
        res["alert"] = True
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1))
    print(json.dumps(res, ensure_ascii=False, indent=1))
    if res["alert"]:
        try:
            from monitoring.alerts import send_alert
            parts = [f"{c} {v.get('rt_premium_pct', v.get('premium_pct'))}%"
                     for c, v in res["etfs"].items() if v.get("buy_ok")]
            parts += [f"{r['代码']} {r['名称']} {r['premium_pct']:.2f}%"
                      for r in scan_ok]
            send_alert(f"[QDII] 溢价进入买入阈值 ≤{THRESH:.0%}: " +
                       ", ".join(parts), level="warning")
        except Exception:
            logger.warning("send_alert 未配置")


if __name__ == "__main__":
    main()
