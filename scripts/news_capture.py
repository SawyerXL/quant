"""消息层采集(2026-10-18 立项): 可验证新闻抓取+词典分类, 不直接进信号链。

源: 新浪财经全球快讯(stock_info_global_sina, 每次约20条) + 新闻联播(news_cctv)。
产出: logs/news_feed.csv (captured_at,source,pub_time,title,category,polarity,hash)
纪律: 词典分类仅作索引/计数; 预测中的消息层只允许"可验证政策事件"作定性
overlay(来源+时间+标题), 数值调整须另立假设并验证。
运行: python scripts/news_capture.py [--show 20]
"""
import argparse
import hashlib
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

OUT = Path("/root/quant/logs/news_feed.csv")
CATS = {
    "货币政策": ["央行", "降准", "降息", "逆回购", "MLF", "LPR", "公开市场", "流动性"],
    "财政": ["财政部", "专项债", "减税", "降费", "赤字", "国债"],
    "监管": ["证监会", "交易所", "IPO", "退市", "减持", "处罚", "立案", "回购"],
    "地产": ["房地产", "楼市", "房贷", "限购"],
    "外部": ["美国", "美联储", "关税", "贸易", "制裁", "地缘", "中东", "俄乌", "欧洲", "美股", "纳指"],
    "产业": ["芯片", "半导体", "新能源", "人工智能", "医药", "军工", "机器人"],
    "市场": ["A股", "上证", "创业板", "港股", "美股", "指数", "成交额"],
    "公司": ["公告", "公司", "股东", "业绩", "重组", "并购"],
}
POS = ["利好", "上涨", "增长", "放宽", "支持", "刺激", "回暖", "突破", "增持"]
NEG = ["利空", "下跌", "收紧", "处罚", "风险", "衰退", "下滑", "减持", "违约"]


def classify(text: str) -> tuple:
    cats = [c for c, kws in CATS.items() if any(k in text for k in kws)]
    pos = sum(1 for k in POS if k in text)
    neg = sum(1 for k in NEG if k in text)
    polarity = "positive" if pos > neg else ("negative" if neg > pos else "neutral")
    return ("/".join(cats) if cats else "其他"), polarity


def fetch_sina() -> list[dict]:
    import akshare as ak
    d = ak.stock_info_global_sina()
    rows = []
    for _, r in d.iterrows():
        text = str(r.get("内容", "")).strip()
        if not text:
            continue
        cat, pol = classify(text)
        rows.append({"source": "sina_global", "pub_time": str(r.get("时间", "")),
                     "title": text[:200], "category": cat, "polarity": pol})
    return rows


def fetch_cctv() -> list[dict]:
    import akshare as ak
    today = date.today().strftime("%Y%m%d")
    d = ak.news_cctv(date=today)
    rows = []
    for _, r in d.iterrows():
        text = f"{r.get('title','')} {str(r.get('content',''))[:200]}".strip()
        if not text:
            continue
        cat, pol = classify(text)
        rows.append({"source": "cctv", "pub_time": str(r.get("date", today)),
                     "title": text[:200], "category": cat, "polarity": pol})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", type=int, default=0)
    ap.add_argument("--no-cctv", action="store_true", help="只在晚间抓新闻联播")
    args = ap.parse_args()
    now = datetime.now().isoformat(timespec="seconds")
    rows = []
    fns = (fetch_sina,) if args.no_cctv else (fetch_sina, fetch_cctv)
    for fn in fns:
        try:
            rows.extend(fn())
        except Exception as e:
            print(f"[WARN] {fn.__name__} 失败: {type(e).__name__} {str(e)[:80]}")
    for r in rows:
        r["captured_at"] = now
        r["hash"] = hashlib.sha256((r["source"] + r["pub_time"] + r["title"]).encode()).hexdigest()[:16]
    if not rows:
        print("本次未取到新闻")
        return
    new = pd.DataFrame(rows)
    if OUT.exists():
        old = pd.read_csv(OUT)
        new = new[~new["hash"].isin(set(old["hash"]))]
        df = pd.concat([old, new], ignore_index=True)
    else:
        df = new
    OUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT, index=False)
    print(f"本次抓取 {len(rows)} 条, 新增 {len(new)} 条, 累计 {len(df)} 条")
    today = df[df["captured_at"].str.startswith(datetime.now().date().isoformat())]
    print("今日分类计数:")
    print(today["category"].value_counts().to_string())
    if args.show:
        print("\n最新 headlines:")
        for _, r in df.tail(args.show).iterrows():
            print(f"  [{r['pub_time']}] ({r['category']}/{r['polarity']}) {r['title'][:110]}")


if __name__ == "__main__":
    main()
