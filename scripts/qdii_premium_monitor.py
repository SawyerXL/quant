"""QDII 溢价日检（2026-10-20）：513100 / 159941 / 513500。

规则（美股腿入场前置）：IOPV/估算溢价 ≤5% 才买；>5% 不买、等待。
数据：最新单位净值 ak.fund_etf_fund_daily_em；最新收盘价 ak.fund_etf_hist_sina。
NAV 有 T+1 滞后；盘前/盘后各看一次，阈值判定以最近可得 NAV 为准并标注日期。
用法：python scripts/qdii_premium_monitor.py
"""
import json
from datetime import date
from pathlib import Path

import pandas as pd
import akshare as ak
from loguru import logger

CODES = {"513100": "sh513100", "159941": "sz159941", "513500": "sh513500"}
THRESH = 0.05
OUT = Path("logs/qdii_premium_monitor.json")


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
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1))
    print(json.dumps(res, ensure_ascii=False, indent=1))
    if res["alert"]:
        try:
            from monitoring.alerts import send_alert
            send_alert(f"[QDII] 溢价进入买入阈值 ≤{THRESH:.0%}: " +
                       ", ".join(f"{c} {v.get('premium_pct')}%"
                                 for c, v in res["etfs"].items()
                                 if v.get("buy_ok")), level="warning")
        except Exception:
            logger.warning("send_alert 未配置")


if __name__ == "__main__":
    main()
