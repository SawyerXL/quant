"""消息面/国家战略候选事件台账（2026-10-26 用户三层预测纪律）。

只读 logs/news_feed.csv，按固定关键词表抽当日候选事件，写：
  logs/news_events_YYYYMMDD.csv  结构化候选（可回测字段齐全）
  logs/news_events_YYYYMMDD.md   人工可读摘要（只列 material=1）
候选不是信号：direction 默认 unknown，必须人工复核+预注册+过闸门十九/
MDE/OOS 才可进入预测；没有重大事件也必须写 no_material_event 行。

全量候选写 CSV（material=0/1 可审计）；MD 只列 material=1，避免把公司
新闻/研报/海外个股当成宏观事件。

用法: python scripts/news_event_ledger.py [--date 2026-10-26]
"""
import argparse
import csv
import hashlib
import json
import re
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FEED = ROOT / "logs" / "news_feed.csv"
OUT_DIR = ROOT / "logs"

TAXONOMY = [
    ("国家战略", ["全会", "政治局", "国常会", "国务院", "五年规划",
                  "新质生产力", "科技自立", "统一大市场", "一带一路",
                  "中央经济工作会议", "两会", "国家安全", "战略"]),
    ("金融战略/货币", ["央行", "降准", "降息", "MLF", "逆回购", "LPR",
                       "社融", "信贷", "流动性", "货币政策"]),
    ("财政", ["财政部", "专项债", "特别国债", "赤字", "减税", "补贴",
              "财政政策", "化债"]),
    ("资本市场", ["证监会", "IPO", "再融资", "减持", "分红", "退市",
                  "汇金", "保险资金", "养老金", "中长期资金", "印花税",
                  "交易所", "北交所", "科创板", "注册制"]),
    ("产业政策", ["产业政策", "规划", "出口管制", "国产替代", "大基金",
                  "产业基金", "试点", "标准", "补贴", "以旧换新",
                  "房地产", "地产", "住房", "现房销售"]),
    ("外部", ["美联储", "Fed", "关税", "中美", "制裁", "中东", "伊朗",
              "油价", "石油", "俄乌", "欧洲央行", "日本央行", "双反",
              "地缘", "美股", "标普", "纳斯达克", "道指", "美债", "美元",
              "人民币", "汇率", "日经", "恒生", "亚太", "黄金", "铜",
              "原油", "大宗", "白宫", "美国财政部", "贸易代表", "芯片出口",
              "台海", "朝鲜", "哈马斯", "北约", "OPEC", "减产", "增产",
              "非农", "CPI", "PPI", "失业", "零售", "FOMC", "鲍威尔",
              "点阵图", "美国大选", "中期选举"]),
    ("监管", ["金融监管", "金监总局", "银保监", "市场监管", "处罚",
              "立案", "调查"]),
]
# 只有“国内政策主体/政策动作”才算 material；海外/公司/研报只留候选
STRICT_BODIES = ("国务院", "国常会", "政治局", "全会", "央行",
                 "中国人民银行", "证监会", "财政部", "发改委", "工信部",
                 "商务部", "金融监管总局", "交易所", "上交所", "深交所",
                 "北交所", "住建局", "住房和城乡建设局", "统计局")
STRICT_POLICY = ("降准", "降息", "MLF", "LPR", "专项债", "特别国债",
                 "中长期资金", "汇金", "印花税", "注册制", "减持新规",
                 "分红", "退市", "化债", "以旧换新", "出口管制",
                 "房地产政策", "楼市政策", "现房销售")
EXTERNAL_MATERIAL = ("美联储", "FOMC", "鲍威尔", "点阵图", "关税",
                     "中美", "制裁", "伊朗", "中东", "油价", "石油",
                     "俄乌", "欧洲央行", "日本央行", "双反", "美股",
                     "标普", "纳斯达克", "道指", "美债", "美元指数",
                     "人民币汇率", "离岸人民币", "OPEC", "非农", "CPI",
                     "PPI", "美国大选", "中期选举", "台海", "朝鲜",
                     "北约", "哈马斯", "芯片出口", "贸易代表", "白宫",
                     "美国财政部")
SECONDARY_SKIP = EXTERNAL_SKIP = ("公司", "股份", "公告", "业绩", "营收", "中标", "集团",
                 "首席执行官", "银行", "贷款", "估值", "票据", "发行",
                 "客户", "盘前", "日内", "现报", "美元/桶", "美元/盎司",
                 "涨近", "涨超", "跌超", "涨停", "跌停", "创新高",
                 "突破", "合约", "合作", "即期", "雷亚尔", "大跌", "品牌", "创始人", "商标")
FOREIGN_CENTRAL = ("美联储", "欧洲央行", "日本央行", "英国央行", "巴西央行",
                   "匈牙利央行", "意大利央行", "尼日利亚央行",
                   "保加利亚央行", "南非央行", "加拿大央行",
                   "澳大利亚央行", "印度央行", "韩国央行", "土耳其央行")
FOREIGN = ("巴西", "土耳其", "英国", "法国", "日本", "韩国", "印尼",
           "以色列", "伊朗", "美国", "特朗普", "美联储", "欧洲央行",
           "英国央行", "日本央行", "巴西央行", "印度", "俄罗斯",
           "乌克兰", "沙特", "埃及", "德国", "欧洲", "越南", "新加坡",
           "澳大利亚", "加拿大", "匈牙利", "意大利", "尼日利亚",
           "保加利亚", "南非")
# 每日公开市场操作是流动性背景，不是“事件”；除非伴随降准/降息/MLF/LPR
# 或利率调整。否则 9 天能抽出上百条同质“事件”，把事件层变成噪声。
ROUTINE_OMO = ("逆回购操作量为零", "今日开展", "净投放", "净回笼",
               "公开市场业务一级交易商", "天期逆回购操作量",
               "隔夜逆回购操作")
RATE_ACTIONS = ("降准", "降息", "MLF", "LPR", "利率下调", "利率上调")
# 政策“公告/发布”不应被 EXCLUDE 的“公司公告”规则误杀；只在国内政策主体
# + 明确动作 + 无公司身份时豁免。
POLICY_EXEMPT = ("设立", "推出", "决定", "印发", "出台", "实施",
                 "启动", "开展", "调整", "修订", "发布")
COMPANY_TERMS = ("公司", "股份", "业绩", "研报", "评级", "中标", "签约")
# 日常 OMO 是背景；单日净投放/回笼超过 5000 亿元才算重大流动性事件。
LARGE_LIQUIDITY_YI = 5000.0
EXCLUDE = ("研报", "评级", "目标价", "盘前", "股价", "成交", "ETF成交",
           "公司", "股份", "公告", "业绩", "数据显示", "指数报",
           "期货", "上涨", "下跌", "涨幅", "跌幅", "涨停", "跌停",
           "收涨", "收跌", "涨", "跌", "品牌", "创始人", "发布",
           "推出", "产品", "销量", "门店", "商标", "网站", "无法访问",
           "捐赠", "驰援", "地震", "应急响应", "中信证券", "专家称",
           "会面", "会见", "银行董事长", "社融规模存量",
           "交易及清算系统", "运行正常", "对华投资")
MARKET_TERMS = ("经济", "金融", "资本市场", "股市", "证券", "货币", "财政",
                "产业", "科技", "半导体", "人工智能", "新能源", "军工",
                "国防", "消费", "地产", "房地产", "化债", "投资", "融资",
                "IPO", "减持", "分红", "汇率", "关税", "贸易", "制裁",
                "油价", "能源")
OFFICIAL = ("新华社", "央视", "人民日报", "中国政府网", "国务院", "央行",
            "证监会", "财政部", "发改委", "统计局", "商务部")
FIELDS = ["event_id", "pub_time", "source", "source_tier", "category",
          "subcategory", "polarity", "title", "keywords", "material",
          "scope", "direction", "surprise", "entry_rule", "horizon_d",
          "exit_rule", "control", "sample_n", "mde", "oos", "status"]


def _classify(title: str):
    # 海外央行周/外盘央行优先归外部，避免被“央行”吸进国内货币
    if (any(x in title for x in FOREIGN_CENTRAL)
            and not any(x in title for x in ("中国", "中方", "人民银行"))):
        return "外部", [x for x in FOREIGN_CENTRAL if x in title]
    for cat, kws in TAXONOMY:
        hit = [k for k in kws if k in title]
        if hit:
            return cat, hit
    return None, []


WEAK_EXTERNAL = ("外交部", "选举", "访问", "会谈", "联合会议")
HIGH_IMPACT_EXTERNAL = ("油价", "石油", "制裁", "军事", "袭击", "战争", "冲突",
                        "美联储", "关税", "中美", "双反")


def _large_liquidity(title: str) -> bool:
    vals = []
    for m in re.findall(r"([0-9][0-9,\.]*)\s*亿元", title):
        try:
            vals.append(float(m.replace(",", "")))
        except ValueError:
            pass
    return bool(vals) and max(vals) >= LARGE_LIQUIDITY_YI


def _material(title: str, cat: str) -> bool:
    if cat != "外部":
        policy_exempt = (
            any(x in title for x in POLICY_EXEMPT)
            and any(x in title for x in STRICT_BODIES)
            and not any(x in title for x in COMPANY_TERMS))
        if any(x in title for x in EXCLUDE) and not policy_exempt:
            return False
    if (cat != "外部" and "央行" in title
            and any(x in title for x in ROUTINE_OMO)
            and not any(x in title for x in RATE_ACTIONS)
            and not _large_liquidity(title)):
        return False
    if cat == "外部":
        # 外围只留宏观/市场级，不留海外个股/研报/纯价格播报
        if any(x in title for x in SECONDARY_SKIP):
            return False
        if (any(x in title for x in WEAK_EXTERNAL)
                and not any(x in title for x in HIGH_IMPACT_EXTERNAL)):
            return False
        return any(x in title for x in EXTERNAL_MATERIAL)
    if not (any(x in title for x in STRICT_BODIES)
            or any(x in title for x in STRICT_POLICY)):
        return False
    if (any(x in title for x in FOREIGN)
            and not any(x in title for x in ("中国", "中方", "人民银行",
                                             "国务院"))):
        return False
    if cat == "国家战略" and not any(x in title for x in MARKET_TERMS):
        return False
    return True


def _tier(title: str, source: str) -> str:
    if any(x in title for x in OFFICIAL):
        return "official"
    return "media" if source else "unknown"


def _scope(cat: str) -> str:
    if cat in ("国家战略", "金融战略/货币", "财政"):
        return "全市场"
    if cat in ("资本市场", "产业政策"):
        return "风格/行业"
    if cat == "外部":
        return "外部"
    return "全市场"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=date.today().isoformat())
    args = ap.parse_args()
    day = args.date
    rows = []
    if FEED.exists():
        with FEED.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if not str(r.get("pub_time", "")).startswith(day):
                    continue
                title = (r.get("title") or "").strip()
                if not title:
                    continue
                cat, hits = _classify(title)
                if not cat:
                    continue
                eid = hashlib.sha256(title.encode("utf-8")).hexdigest()[:12]
                rows.append({
                    "event_id": eid,
                    "pub_time": r.get("pub_time", ""),
                    "source": r.get("source", ""),
                    "source_tier": _tier(title, r.get("source", "")),
                    "category": cat,
                    "subcategory": ",".join(hits[:3]),
                    "polarity": r.get("polarity", ""),
                    "title": title,
                    "keywords": ",".join(hits),
                    "material": int(_material(title, cat)),
                    "scope": _scope(cat),
                    "direction": "unknown",
                    "surprise": "unknown",
                    "entry_rule": "T+1_open",
                    "horizon_d": "1;5;20",
                    "exit_rule": "fixed_horizon",
                    "control": "same_regime_non_event",
                    "sample_n": "",
                    "mde": "",
                    "oos": "",
                    "status": "candidate",
                })
    uniq = {}
    for r in rows:
        uniq.setdefault(r["event_id"], r)
    rows = sorted(uniq.values(), key=lambda x: x["pub_time"])
    materials = [r for r in rows if r["material"] == 1]
    if not materials:
        rows.append({
            "event_id": "no_material_event",
            "pub_time": f"{day} 23:59:59",
            "source": "news_feed",
            "source_tier": "n/a",
            "category": "none",
            "subcategory": "",
            "polarity": "neutral",
            "title": "当日无重大国内政策/战略/监管事件（候选表无 material=1）",
            "keywords": "",
            "material": 0,
            "scope": "none",
            "direction": "none",
            "surprise": "none",
            "entry_rule": "n/a",
            "horizon_d": "",
            "exit_rule": "",
            "control": "",
            "sample_n": "",
            "mde": "",
            "oos": "",
            "status": "no_event",
        })
    out = OUT_DIR / f"news_events_{day.replace('-', '')}.csv"
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    md = OUT_DIR / f"news_events_{day.replace('-', '')}.md"
    lines = [f"# {day} 消息面候选事件（人工复核前）", ""]
    lines.append(f"- 全部候选：{len(rows)}；**material=1：{len(materials)}**")
    lines.append("- direction 默认 unknown；候选不是信号，须人工复核+预注册。")
    lines.append("")
    if materials:
        lines.append("## material=1（进入预测卡候选）")
        for r in materials:
            lines.append(f"- `{r['pub_time']}` [{r['category']}/{r['source_tier']}] "
                         f"{r['title'][:140]}")
    else:
        lines.append("## 无重大国内政策/战略/监管事件")
        lines.append("- 全量候选里只有公司/研报/海外个股/弱相关，未达 material。")
    lines.append("")
    lines.append("## 其他候选（material=0，仅供人工扫读）")
    for r in rows:
        if r["material"] == 0 and r["event_id"] != "no_material_event":
            lines.append(f"- `{r['pub_time']}` [{r['category']}] {r['title'][:100]}")
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"date": day, "rows": len(rows),
                      "material": len(materials),
                      "material_categories": {c: sum(1 for r in materials
                                                     if r["category"] == c)
                                             for c in sorted({r["category"]
                                                              for r in materials})},
                      "out": str(out)}, ensure_ascii=False))
    print("->", md)


if __name__ == "__main__":
    main()
