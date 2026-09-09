"""
生成个人持仓策略分析Word文档。
用法: python scripts/generate_strategy_doc.py [--send]
输出: 个人策略手册.docx
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from docx import Document
from docx.shared import Inches, Pt, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn
from datetime import date, datetime
import pandas as pd
import os
from loguru import logger

OUTPUT = Path("个人策略手册.docx")
HOLDINGS_FILE = Path("config/my_holdings.csv")

def set_cell_font(cell, text, bold=False, size=9, color=None):
    """设置单元格字体"""
    cell.text = ""
    p = cell.paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(str(text))
    run.font.size = Pt(size)
    run.font.name = '微软雅黑'
    run._element.rPr.rFonts.set(qn('w:eastAsia'), '微软雅黑')
    run.bold = bold
    if color:
        run.font.color.rgb = RGBColor(*color)

def add_styled_table(doc, headers, rows, col_widths=None):
    """添加格式化表格"""
    table = doc.add_table(rows=1 + len(rows), cols=len(headers))
    table.style = 'Table Grid'
    table.alignment = WD_TABLE_ALIGNMENT.CENTER

    # 表头
    for i, h in enumerate(headers):
        set_cell_font(table.rows[0].cells[i], h, bold=True, size=8,
                      color=(255, 255, 255))
        # 深色背景
        shading = table.rows[0].cells[i]._element
        tcPr = shading.get_or_add_tcPr()
        solidFill = tcPr.makeelement(qn('w:shd'), {
            qn('w:fill'): '2F5496', qn('w:val'): 'clear'
        })
        tcPr.append(solidFill)

    # 数据行
    for r_idx, row_data in enumerate(rows):
        for c_idx, val in enumerate(row_data):
            cell = table.rows[r_idx + 1].cells[c_idx]
            set_cell_font(cell, val, size=8)
            # 交替行背景
            if r_idx % 2 == 0:
                shading = cell._element
                tcPr = shading.get_or_add_tcPr()
                solidFill = tcPr.makeelement(qn('w:shd'), {
                    qn('w:fill'): 'D6E4F0', qn('w:val'): 'clear'
                })
                tcPr.append(solidFill)

    doc.add_paragraph("")  # spacer
    return table


def run():
    today_str = date.today().strftime("%Y-%m-%d")
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # ── 加载持仓数据 ──
    df = pd.read_csv(HOLDINGS_FILE, dtype={'code': str})
    df['code'] = df['code'].str.zfill(6)

    # 计算持仓市值 (用cost_price近似，实际需实时价)
    df['cost_value'] = df['cost_price'] * df['shares']
    total_cost = df['cost_value'].sum()
    total_shares = df['shares'].sum()

    # ── 加载最新价格数据(从Parquet) ──
    from data.storage import load_daily
    latest_prices = {}
    for _, r in df.iterrows():
        code = r['code']
        try:
            d = load_daily(code, None, today_str)
            if not d.empty and 'close' in d.columns:
                d = d.sort_values('date')
                latest_prices[code] = float(d['close'].iloc[-1])
        except:
            pass

    # ── 加载MA10数据 ──
    ma10_data = {}
    for _, r in df.iterrows():
        code = r['code']
        try:
            d = load_daily(code, (pd.Timestamp(today_str) - pd.Timedelta(days=30)).strftime("%Y-%m-%d"), today_str)
            if not d.empty and 'close' in d.columns:
                d = d.sort_values('date')
                closes = d['close'].dropna()
                if len(closes) >= 10:
                    ma10_data[code] = float(closes.tail(10).mean())
        except:
            pass

    # ── 创建文档 ──
    doc = Document()

    # 设置默认字体
    style = doc.styles['Normal']
    font = style.font
    font.name = '微软雅黑'
    font.size = Pt(10)
    style.element.rPr.rFonts.set(qn('w:eastAsia'), '微软雅黑')

    # 设置页边距
    for section in doc.sections:
        section.top_margin = Cm(2)
        section.bottom_margin = Cm(2)
        section.left_margin = Cm(2.5)
        section.right_margin = Cm(2.5)

    # ════════════════════════════════════════
    # 封面标题
    # ════════════════════════════════════════
    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = title.add_run("个人持仓策略分析报告")
    run.font.size = Pt(22)
    run.font.name = '微软雅黑'
    run._element.rPr.rFonts.set(qn('w:eastAsia'), '微软雅黑')
    run.bold = True
    run.font.color.rgb = RGBColor(0x2F, 0x54, 0x96)

    subtitle = doc.add_paragraph()
    subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = subtitle.add_run(f"生成日期: {now}")
    run.font.size = Pt(11)
    run.font.color.rgb = RGBColor(0x80, 0x80, 0x80)

    doc.add_paragraph("")

    # ════════════════════════════════════════
    # 一、持仓总览
    # ════════════════════════════════════════
    doc.add_heading("一、持仓总览", level=1)

    # 计算实际市值和盈亏
    total_mv = 0
    total_pl = 0
    in_pool_count = 0
    ma10_below_count = 0

    # TOP60池 (7/11快照: 基于持仓股票的池内外判断)
    # 实际池: 大族激光/立讯精密/药明康德/紫金矿业/工业富联/中科曙光/紫光国微/洛阳钼业/东方财富/牧原股份
    top60_pool = {"002008", "002475", "603259", "601899", "601138", "603019", "002049",
                  "603993", "300059", "002714", "600030", "600893"}

    for _, r in df.iterrows():
        code = r['code']
        price = latest_prices.get(code, r['cost_price'])
        mv = price * int(r['shares'])
        pl = (price - r['cost_price']) * int(r['shares'])
        total_mv += mv
        total_pl += pl
        if code in top60_pool:
            in_pool_count += 1
        if code in ma10_data and price < ma10_data[code]:
            ma10_below_count += 1

    holding_count = len(df)

    overview_items = [
        ("持仓数量", f"{holding_count}只"),
        ("持仓市值", f"¥{total_mv:,.0f}"),
        ("持仓成本", f"¥{total_cost:,.0f}"),
        ("总盈亏", f"¥{total_pl:,.0f} ({total_pl/total_cost*100:+.1f}%)"),
        ("TOP60池内", f"{in_pool_count}/{holding_count}只"),
        ("MA10下方", f"{ma10_below_count}只"),
        ("可用现金", "¥125,911 (周五清仓后)"),
        ("策略框架", "MA10-4d退出 + TP30/60%止盈 + TOP60选股池"),
    ]

    table = doc.add_table(rows=len(overview_items), cols=2)
    table.style = 'Table Grid'
    for i, (k, v) in enumerate(overview_items):
        set_cell_font(table.rows[i].cells[0], k, bold=True, size=10)
        set_cell_font(table.rows[i].cells[1], v, size=10)
        table.rows[i].cells[0].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.LEFT
        table.rows[i].cells[1].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.LEFT

    doc.add_paragraph("")

    # ════════════════════════════════════════
    # 二、策略框架说明
    # ════════════════════════════════════════
    doc.add_heading("二、策略框架", level=1)

    rules = [
        ("选股池", "中证800中成交额TOP60，调仓日统一评估，优先池内换池外"),
        ("调仓周期", "双周（每月15日 + 月末）"),
        ("退出规则", "MA10连续4个交易日收盘跌破 → 全部清仓（不留底仓、不逐步减）"),
        ("止盈规则", "盈利+30%卖1/3，再+60%卖1/3（自动驾驶，触发即执行）"),
        ("风控规则", "无追踪止损、无绝对止损、无过热过滤（7/9消融测试全部证伪）"),
        ("集中度", "单只上限15%，行业上限30%"),
        ("回测基准", "年化+9.28%，夏普0.53，最大回撤-35.1%"),
    ]

    for name, desc in rules:
        p = doc.add_paragraph()
        run = p.add_run(f"▸ {name}：")
        run.bold = True
        run.font.size = Pt(10)
        run = p.add_run(desc)
        run.font.size = Pt(10)

    doc.add_paragraph("")

    # ════════════════════════════════════════
    # 三、个股持仓明细
    # ════════════════════════════════════════
    doc.add_heading("三、个股持仓明细", level=1)

    p = doc.add_paragraph()
    run = p.add_run("说明：新框架=MA10-4d退出+TP30/60%，旧框架=十维评分。冲突时按新框架执行（回测年化+9.3% vs 旧+5.7%）。")
    run.font.size = Pt(9)
    run.font.color.rgb = RGBColor(0x80, 0x80, 0x80)

    # 持仓表格
    headers = ["代码", "名称", "成本", "现价", "盈亏%", "MA10", "破MA10天数", "新框架", "旧框架", "池", "操作建议"]
    rows = []

    for _, r in df.iterrows():
        code = r['code']
        name = r['name']
        cost = r['cost_price']
        shares = int(r['shares'])
        price = latest_prices.get(code, cost)
        pl_pct = (price / cost - 1) * 100
        ma10 = ma10_data.get(code, 0)

        # 破MA10天数(简化: 如有MA10且价格低于MA10)
        days_below = ""
        if ma10 > 0 and price < ma10:
            # 近似: 用(MA10-price)/MA10作为偏离度
            pct_below = (1 - price / ma10) * 100
            days_below = f"破({pct_below:.0f}%)"

        # 新框架判断
        if pl_pct >= 60:
            new_fw = "🔴 止盈TP2"
        elif pl_pct >= 30:
            new_fw = "🟠 止盈TP1"
        elif ma10 > 0 and price < ma10:
            new_fw = "🔴 清仓(MA10)"
        else:
            new_fw = "🟢 持有"

        # 旧框架判断 (简化)
        old_fw = "🟡 中性"
        if price < ma10 and pl_pct < -10:
            old_fw = "🟠 偏弱"

        in_pool = "✅" if code in top60_pool else "池外"

        # 操作建议
        if "清仓" in new_fw:
            action = f"🔴 卖出 {shares}股"
        elif "止盈TP2" in new_fw:
            sell_shares = int(shares * 0.5)
            action = f"🔴 止盈卖{sell_shares}股"
        elif "止盈TP1" in new_fw:
            sell_shares = int(shares * 0.33)
            action = f"🟠 止盈卖{sell_shares}股"
        elif code not in top60_pool:
            action = "🟡 持有(池外→逐步换)"
        else:
            action = "🟢 持有"

        rows.append([code, name, f"¥{cost:.2f}", f"¥{price:.2f}",
                     f"{pl_pct:+.1f}%", f"¥{ma10:.2f}" if ma10 > 0 else "—",
                     days_below, new_fw, old_fw, in_pool, action])

    add_styled_table(doc, headers, rows)

    # ════════════════════════════════════════
    # 四、周一操作计划
    # ════════════════════════════════════════
    doc.add_heading("四、周一(7/13)操作计划", level=1)

    doc.add_heading("4.1 必须卖出（框架触发）", level=2)
    sell_list = [
        ("600893 航发动力", "700股 ≈ ¥25,039", "MA10下4d触发，盈亏+14.8%，死叉+RSI=28", "按框架全清"),
        ("300059 东方财富", "800股 ≈ ¥16,152", "MA10下4d触发，盈亏-4.1%，资金踩踏-8.65亿", "按框架全清"),
        ("002559 亚威股份", "剩余约400股", "TP2触发，盈亏+182%，周五已卖400股", "清剩余"),
        ("603993 洛阳钼业", "500股 ≈ ¥8,750", "MA10下4d触发(有资金抢筹争议)，盈亏-8.2%", "按框架执行"),
    ]
    for stock, amount, reason, action in sell_list:
        p = doc.add_paragraph()
        run = p.add_run(f"🔴 {stock} | {amount}")
        run.bold = True
        p2 = doc.add_paragraph(f"   原因: {reason}")
        p3 = doc.add_paragraph(f"   操作: {action}")

    doc.add_heading("4.2 重点关注（持有但风险信号）", level=2)
    p = doc.add_paragraph()
    run = p.add_run("⚠️ 601138 工业富联")
    run.bold = True
    doc.add_paragraph("   踩踏-22.75亿（全市场最惨烈单只），但MA10尚未跌破(66.97)")
    doc.add_paragraph("   周一若低开破MA10 → 立即卖出，不等到4天")

    p = doc.add_paragraph()
    run = p.add_run("⚠️ 603392 万泰生物")
    run.bold = True
    doc.add_paragraph("   深度亏损-57.9%，池外。待补深度亏损处置规则")

    doc.add_heading("4.3 继续持有（无问题）", level=2)
    hold_list = "、".join([
        "601899 紫金矿业(抢筹+3.09亿)", "603019 中科曙光(抢筹+7.64亿)",
        "603259 药明康德(抢筹+7.50亿)", "002049 紫光国微(抢筹+3.40亿)",
        "002714 牧原股份", "600030 中信证券", "603156 养元饮品",
        "001965 招商公路", "400286 苏药发3"
    ])
    doc.add_paragraph(f"   {hold_list}")

    doc.add_heading("4.4 已确认卖出（更新CSV）", level=2)
    doc.add_paragraph("   ✅ 002008 大族激光 — 周五已清仓")
    doc.add_paragraph("   ✅ 002475 立讯精密 — 周五已清仓")

    # ════════════════════════════════════════
    # 五、资金面与宏观
    # ════════════════════════════════════════
    doc.add_heading("五、资金面与宏观环境", level=1)

    macro_items = [
        ("资金面评分", "-15/100 🟠 资金持续撤出"),
        ("两市成交额", "33,885亿 🟢 活跃(2.8-3.5万亿)"),
        ("融资余额", "14,713亿，连降7天 🔴"),
        ("指数位置", "中证800=5431，MA20下方 🔴"),
        ("沪指", "跌破4000点(-1%)"),
        ("科创50", "单日-5.53%，半导体/科技踩踏"),
        ("市场风格", "高位科技→低位医药/消费/军工，高低切换明确"),
        ("周末消息", "拓荆科技复牌(年内+500%)、长鑫IPO周三申购(冻300亿)、中报预增潮"),
        ("下周风险", "7/16长鑫IPO抽血、7/15宏观数据公布、科技中报验证期"),
    ]

    for k, v in macro_items:
        p = doc.add_paragraph()
        run = p.add_run(f"▸ {k}：")
        run.bold = True
        run.font.size = Pt(10)
        run = p.add_run(v)
        run.font.size = Pt(10)

    # ════════════════════════════════════════
    # 六、熊市应对预案
    # ════════════════════════════════════════
    doc.add_heading("六、熊市应对预案", level=1)

    doc.add_paragraph("以下为'如果这轮牛市结束'的预案，当前尚未触发，提前储备。")

    doc.add_heading("6.1 启动条件", level=2)
    doc.add_paragraph("不靠感觉，靠信号组合（需≥3个同时触发）：")
    doc.add_paragraph("  ① 中证800跌破MA200")
    doc.add_paragraph("  ② 融资余额连续流出>10天（当前7天）")
    doc.add_paragraph("  ③ 两市成交额<1.5万亿持续5天")
    doc.add_paragraph("  ④ 下跌股票>70%持续3天以上")

    doc.add_heading("6.2 熊市双引擎", level=2)

    p = doc.add_paragraph()
    run = p.add_run("引擎一：ETF期权买Put（急跌段）")
    run.bold = True
    doc.add_paragraph("  开户：20天50万资产+6月经验+C5测评+考试80分+模拟+临柜")
    doc.add_paragraph("  标的：510300(沪深300ETF)或510050(上证50ETF)")
    doc.add_paragraph("  合约：当月/下月，平值/浅虚值，1张≈¥500-1500")
    doc.add_paragraph("  风控：单笔≤总资金5%，浮亏30%止损，不做裸卖")

    p = doc.add_paragraph()
    run = p.add_run("引擎二：可转债（磨底段）")
    run.bold = True
    doc.add_paragraph("  基准回测：年化+7.4%/夏普0.90，30只组合夏普1.12")
    doc.add_paragraph("  选债：价格<110 + 纯债价值/价格>0.9 + YTM>0 + 评级≥AA-")
    doc.add_paragraph("  下修加分：触发条件满足+大股东持转债+有回售压力+PB>1")
    doc.add_paragraph("  仓位：20只等权，单只≤5%，月调仓，闲钱60-70%")

    doc.add_heading("6.3 节奏", level=2)
    stages = [
        "阶段1(急跌1-3月): Put为主 → 股票减到3成 → CB观望",
        "阶段2(磨底3-12月): Put止盈 → CB分散建仓 → 吃下修红利",
        "阶段3(反转确认): CB转股/卖出 → 切回TOP60+MA10-4d框架",
    ]
    for s in stages:
        doc.add_paragraph(f"  {s}")

    # ════════════════════════════════════════
    # 七、风险提示
    # ════════════════════════════════════════
    doc.add_heading("七、关键风险提醒", level=1)

    risks = [
        ("规则执行偏差", "回溯年化+9.28% vs 实盘-9.30%的差距来自执行而非策略。清仓=全清，不是减仓20%。"),
        ("池外持仓过多", f"{in_pool_count}/{holding_count}只在TOP60池内。7/15调仓日统一换血。"),
        ("集中度违规", "单只上限15%，池外12只需逐步清退换池内。"),
        ("深度亏损处置", "万泰生物-57.9%无对应规则，需补深度亏损强制清仓条款。"),
        ("长鑫IPO抽血", "7/16冻300亿，半导体板块流动性承压。已搁置CXMT买入计划。"),
        ("现金效率", "现金¥12.6万闲置(36%)，当前环境现金=最好的持仓。待信号确认后再部署。"),
    ]

    for title, desc in risks:
        p = doc.add_paragraph()
        run = p.add_run(f"⚠️ {title}：")
        run.bold = True
        run = p.add_run(desc)

    # ════════════════════════════════════════
    # 页脚
    # ════════════════════════════════════════
    doc.add_paragraph("")
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(f"— 报告由量化系统自动生成 | {now} —")
    run.font.size = Pt(8)
    run.font.color.rgb = RGBColor(0x80, 0x80, 0x80)

    # ── 保存 ──
    doc.save(str(OUTPUT))
    logger.info(f"策略文档已生成: {OUTPUT} ({OUTPUT.stat().st_size/1024:.0f}KB)")

    print(body := f"""
{'='*50}
  个人持仓策略分析报告
  生成时间: {now}
  文件: {OUTPUT.absolute()}
  大小: {OUTPUT.stat().st_size/1024:.0f}KB
{'='*50}

  内容包含:
    一、持仓总览 ({holding_count}只, ¥{total_mv:,.0f})
    二、策略框架规则
    三、个股持仓明细表
    四、周一操作计划
    五、资金面与宏观环境
    六、熊市应对预案
    七、关键风险提醒
{'='*50}
""")

    return str(OUTPUT)


if __name__ == '__main__':
    run()
