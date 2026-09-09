"""开盘半小时复盘：实时拉取行情+K线，计算开盘量能与全天结果（2026-08-21）。"""
import json, re, sys, time
import pandas as pd
import requests

HEADERS = {"Referer": "https://finance.sina.com.cn"}
UA = {"User-Agent": "Mozilla/5.0"}

RT_CODES = ["sh000001", "sh000688", "sz399001", "sz399006", "sh600276", "sh603019",
            "sh603259", "sh600030", "sh601899", "sh603993", "sz002409", "sh588000",
            "sh512480", "sh512010", "sh512880", "sh512400", "sh515880",
            "sh603392", "sz002559", "sz002714",
            "sh601138", "sz000977", "sz300308", "sz300760"]  # 算力/医药对照

INDEX_SYMS = {"sh000001", "sh000688", "sz399001", "sz399006"}

def fetch_realtime(codes):
    url = f"http://hq.sinajs.cn/list={','.join(codes)}"
    r = requests.get(url, headers={**HEADERS, **UA}, timeout=8)
    r.encoding = "gbk"
    out = {}
    for line in r.text.strip().split("\n"):
        m = re.match(r'var hq_str_(\w+)="(.*)";', line)
        if not m:
            continue
        sym, fields = m.group(1), m.group(2)
        if not fields:
            out[sym] = None
            continue
        f = fields.split(",")
        out[sym] = {"name": f[0], "open": float(f[1]), "prev_close": float(f[2]),
                    "price": float(f[3]), "high": float(f[4]), "low": float(f[5]),
                    "volume": float(f[8]), "amount": float(f[9]),
                    "dt": f[30], "tm": f[31]}
        if sym in INDEX_SYMS:
            out[sym]["volume"] *= 100  # 指数成交量单位为手，K线为股，对齐量纲
    return out

def fetch_kline(sym, scale, datalen=60):
    url = (f"https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
           f"?symbol={sym}&scale={scale}&ma=no&datalen={datalen}")
    r = requests.get(url, headers=UA, timeout=8)
    return json.loads(r.text)

def main():
    rt = fetch_realtime(RT_CODES)
    print("=== REALTIME (今日收盘快照) ===")
    for sym, q in rt.items():
        if q is None:
            print(f"{sym}: 无数据")
        elif "open" in q:
            print(f"{sym} {q['name']}: open={q['open']} prev={q['prev_close']} "
                  f"now={q['price']} hi={q['high']} lo={q['low']} "
                  f"vol={q['volume']/1e6:.1f}M股 amt={q['amount']/1e8:.2f}亿 "
                  f"pct={(q['price']/q['prev_close']-1)*100:+.2f}% {q['dt']} {q['tm']}")
        else:
            print(f"{sym} {q['name']}: price={q['price']} pct={q['pct']}% "
                  f"amt={q['amount']/1e8:.2f}亿 {q['dt']} {q['tm']}")

    today = rt.get("sh000001", {}).get("dt", "")
    print(f"\n行情日期: {today}")
    if today != "2026-08-21":
        print("!! 行情日期非今日，以下分析基于该日期数据")

    print("\n=== 开盘半小时量能 vs 5日均量 ===")
    for sym in ["sh000001", "sh000688", "sh600276", "sh603019", "sh603259",
                "sh600030", "sh601899", "sh603993", "sz002409", "sh588000"]:
        try:
            d5 = fetch_kline(sym, 240, 8)      # 日线: 含最近5-6个交易日
            m5 = fetch_kline(sym, 5, 130)      # 5分钟线: 今天+昨天(130根保证盘后仍含今日开盘段)
            time.sleep(0.3)
            d5df = pd.DataFrame(d5)
            d5df["volume"] = d5df["volume"].astype(float)
            m5df = pd.DataFrame(m5)
            m5df["day"] = m5df["day"].str[:10]
            m5df["vol"] = m5df["volume"].astype(float)
            td = str(d5df["day"].iloc[-1])
            # 开盘半小时: 09:35~10:00 六根5分钟K线
            bars = m5df[(m5df["day"] == td) & (m5df["day"].str.len() > 0)].copy()
            bars = bars[bars["day"].str[:10] == td]
            open30 = None
            if len(bars) >= 6:
                t0 = bars.iloc[0]["day"]
                first6 = bars[bars["day"].str[11:16] <= "10:00"].head(6)
                open30 = first6["vol"].sum()
            prev5 = d5df.iloc[-6:-1]["volume"]  # 前5个交易日
            avg5 = prev5.mean()
            ratio = open30 / (avg5 / 8) if (open30 and avg5) else None
            q = rt.get(sym, {})
            gap = None
            if "open" in q:
                gap = (q["open"] / q["prev_close"] - 1) * 100
            full_day_pct = (q["price"] / q["prev_close"] - 1) * 100 if "open" in q else q.get("pct")
            name = q.get("name", sym)
            print(f"{name}({sym}): 昨收={q.get('prev_close')} 开盘={q.get('open')} "
                  f"开幅={gap:+.2f}% | 开盘30分量={open30/1e6 if open30 else 'NA':.2f}M股 "
                  f"vs 5日均量/8={avg5/8/1e6:.2f}M股 倍率={ratio:.2f}x "
                  f"| 全天量={q.get('volume', 0)/1e6:.1f}M股 vs 5日={q.get('volume', 0)/avg5:.2f}x "
                  f"| 收盘pct={full_day_pct:+.2f}% 高={q.get('high')} 低={q.get('low')}")
        except Exception as e:
            print(f"{sym}: 拉取失败 {e}")

    print("\n=== 持仓检查: MA10-4d / 止盈信号 (cost来自my_holdings.csv) ===")
    holdings = {}
    for line in open("/root/quant/config/my_holdings.csv", encoding="utf-8").read().strip().split("\n")[1:]:
        p = line.split(",")
        code = p[0].strip()
        if code.startswith(("11", "12", "71", "40")):  # 转债/三板不做MA10检查
            continue
        try:
            holdings[code] = (p[1], float(p[2]))
        except ValueError:
            pass
    def rsi14(closes):
        d = closes.diff()
        gain = d.clip(lower=0).rolling(14).mean()
        loss = (-d.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, 1e-9)
        return float((100 - 100 / (1 + rs)).iloc[-1])
    for code, (name, cost) in holdings.items():
        try:
            pre = "sh" if code.startswith(("6", "5", "9")) else "sz"
            k = fetch_kline(f"{pre}{code}", 240, 35)
            time.sleep(0.3)
            df = pd.DataFrame(k)
            df["close"] = df["close"].astype(float)
            df["ma10"] = df["close"].rolling(10).mean()
            df["ma20"] = df["close"].rolling(20).mean()
            last = df.iloc[-1]
            px = rt.get(f"{pre}{code}", {}).get("price") or last["close"]
            # 收盘价连续位于MA10下方的天数
            below = 0
            for _, r in df[::-1].iterrows():
                if r["close"] < r["ma10"]:
                    below += 1
                else:
                    break
            gain = (px / cost - 1) * 100
            tp = ""
            if gain >= 60: tp = "TP2:再卖1/3"
            elif gain >= 30: tp = "TP1:卖1/3"
            ma_ref = "MA20" if gain > 50 else "MA10"
            rsi = rsi14(df["close"])
            print(f"{name}({code}): 现价={px:.2f} 盈亏={gain:+.1f}% MA10={last['ma10']:.2f} "
                  f"MA20={last['ma20']:.2f} 破MA10天数={below} RSI14={rsi:.0f} "
                  f"止盈={tp or '无'} 卖出参考线={ma_ref}")
        except Exception as e:
            print(f"{code} {name}: 拉取失败 {e}")

if __name__ == "__main__":
    main()
