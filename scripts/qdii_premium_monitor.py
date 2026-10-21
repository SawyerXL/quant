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
from datetime import date
from pathlib import Path

import pandas as pd
import akshare as ak
from loguru import logger

CODES = {"513100": "sh513100", "159941": "sz159941", "513500": "sh513500",
         "513650": "sh513650", "159655": "sz159655",
         "159513": "sz159513", "513110": "sh513110"}
THRESH = 0.05
OUT = Path("logs/qdii_premium_monitor.json")


def scan_us_broad():
    """全市场实时 IOPV 扫描：美股宽基最低溢价。负折价率=溢价。"""
    try:
        d = ak.fund_etf_spot_em()
        d["代码"] = d["代码"].astype(str).str.zfill(6)
        m = d[d["名称"].astype(str).str.contains(
            "标普500|美国50|纳斯达克100|纳指", na=False)].copy()
        m = m[~m["名称"].astype(str).str.contains(
            "生物|科技|油气|消费|红利|A股", na=False)]
        m["premium_pct"] = -pd.to_numeric(m["基金折价率"], errors="coerce")
        m = m.dropna(subset=["premium_pct"]).sort_values("premium_pct")
        cols = [c for c in ["代码", "名称", "最新价", "IOPV实时估值",
                            "premium_pct", "成交额"] if c in m.columns]
        return m[cols].head(20).to_dict("records")
    except Exception as e:
        return [{"err": str(e)[:150]}]


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
    navs = latest_navs()
    res = {"checked_at": date.today().isoformat(), "threshold": THRESH,
           "etfs": {}, "alert": False,
           "subscription_usable": subscription_usable()}
    for code, sym in CODES.items():
        try:
            d = ak.fund_etf_hist_sina(symbol=sym)
            d["date"] = pd.to_datetime(d["date"])
            px = float(d.sort_values("date")["close"].iloc[-1])
            pxdate = str(d.sort_values("date")["date"].iloc[-1].date())
            n = navs.get(code, {})
            nav = n.get("nav"); navdt = n.get("nav_date")
            prem = (px / nav - 1) if nav else None
            res["etfs"][code] = {"price": px, "price_date": pxdate,
                                 "nav": nav, "nav_date": navdt,
                                 "premium_pct": round(prem * 100, 2) if prem is not None else None,
                                 "buy_ok": bool(prem is not None and prem <= THRESH)}
            if prem is not None and prem <= THRESH:
                res["alert"] = True
        except Exception as e:
            res["etfs"][code] = {"err": str(e)[:150]}
    res["us_scan"] = scan_us_broad()
    scan_ok = [r for r in res["us_scan"]
               if isinstance(r.get("premium_pct"), (int, float))
               and r["premium_pct"] <= THRESH * 100]
    if scan_ok:
        res["alert"] = True
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1))
    print(json.dumps(res, ensure_ascii=False, indent=1))
    if res["alert"]:
        try:
            from monitoring.alerts import send_alert
            parts = [f"{c} {v.get('premium_pct')}%"
                     for c, v in res["etfs"].items() if v.get("buy_ok")]
            parts += [f"{r['代码']} {r['名称']} {r['premium_pct']:.2f}%"
                      for r in scan_ok]
            send_alert(f"[QDII] 溢价进入买入阈值 ≤{THRESH:.0%}: " +
                       ", ".join(parts), level="warning")
        except Exception:
            logger.warning("send_alert 未配置")


if __name__ == "__main__":
    main()
