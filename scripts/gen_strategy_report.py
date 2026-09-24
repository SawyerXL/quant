"""主策略说明报告生成 (Word 版) — 2026-09-24
机器值口径: 组合/CB/股票数字引自账本定案(09-21~24 回测与审计),
持仓/信号数为生成时点实读。输出 docs/主策略说明报告_20260924.docx。
"""
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from docx import Document
from docx.shared import Pt

ROOT = Path(__file__).parent.parent
TODAY = date.today().strftime("%Y-%m-%d")
OUT = ROOT / "docs" / f"主策略说明报告_{TODAY.replace('-', '')}.docx"


def live_state() -> dict:
    st = {}
    try:
        sig = json.loads(
            (ROOT / "data_store/meta/signal_cb_latest.json").read_text("utf-8"))
        st["cb_holdings"] = len(sig.get("holdings", []))
        st["cb_capital"] = sig.get("capital")
    except Exception:
        st["cb_holdings"] = st["cb_capital"] = "?"
    try:
        pos = json.loads(
            (ROOT / "logs/qmt_positions_latest.json").read_text("utf-8"))
        cb = {k: v for k, v in pos.get("positions", {}).items()
              if str(k)[:3] in ("110", "111", "113", "118", "123", "127", "128")
              and v.get("volume", 0) > 0}
        st["cb_positions"] = len(cb)
        st["cb_mv"] = sum(v.get("market_value", 0) for v in cb.values())
    except Exception:
        st["cb_positions"] = st["cb_mv"] = "?"
    return st


def build():
    st = live_state()
    doc = Document()
    doc.add_heading(f"主策略说明报告（{TODAY} 版）", level=0)

    # ── 一、总览 ──
    doc.add_heading("一、策略总览", level=1)
    doc.add_paragraph(
        "架构：单轨双腿——股票主策略 + CB 双低腿，固定 40/60 配比、月频再平衡。"
        "资金：股票 40 万 + CB 60 万；打新底仓（511360）为独立资金线。")
    t = doc.add_table(rows=5, cols=4)
    t.style = "Table Grid"
    rows = [
        ("对象", "年化", "夏普", "回撤"),
        ("40/60 组合（月频再平衡）", "+11.7%", "1.33", "−13.4%"),
        ("CB 双低腿（单独）", "≈+15%", "1.39（10bp 成本档）", "−21.2%"),
        ("股票主策略 A3", "+4.0~4.4%", "0.28~0.32", "−12.6~−13.3%"),
        ("口径", "活跃窗口 2019-02~2026-09，V3 剔404 口径；股票腿为价格口径（不含分红，+2.3pp）", "", ""),
    ]
    for i, r in enumerate(rows):
        for j, v in enumerate(r):
            t.rows[i].cells[j].text = v
    doc.add_paragraph(
        "关键定性：组合收益来源 = CB 腿 + 分散化（ρ≈0.21）+ 月频再平衡；"
        "股票选股 alpha 与 0 不可分辨；择时（动态配比）为负贡献（−0.55 夏普），已弃用。")

    # ── 二、股票腿 ──
    doc.add_heading("二、股票腿（Track A）", level=1)
    doc.add_paragraph(
        "选股：全市场成交额 TOP30（pool30），等权，月调仓。"
        "择时：MA200 五档仓位（纯函数，无滞后带）。"
        "退出：MA10 破位出清（豁免：V反/RSI超卖/浮盈+50% 改 MA20）。")
    doc.add_paragraph("当前状态：熊档，持仓 0（引擎熊档语义 30% 与实盘 0 的差异为已登记的开放问题）。")

    # ── 三、CB 腿 ──
    doc.add_heading("三、CB 腿（Track CB）", level=1)
    doc.add_paragraph(
        "策略：双低（价格 + 溢价率×100）最低 25 只，等权，月调仓（每月 15 日 + 月末倒数第二）。")
    doc.add_paragraph(
        "过滤（共享唯一实现 data/cb_select.py，信号与回测同一份代码）："
        "①评级 ≥A- ②规模 ≥0.5 亿 ③强赎区排除（价>130 且溢价<5%）④404 退市板块排除 "
        "⑤停牌过滤：日线最新 bar ≤10 个交易日 + 新浪实时成交量=0 兜底 "
        "⑥临期剔除 N=60（距最后交易日 <60 天剔除）"
        "⑦禁买 M=120（60~120 天缓冲带：已持有不加仓、未持有禁买）。")
    doc.add_paragraph(
        "执行：差量幂等（目标−持仓），买入张数取整 10 整数倍（47→50），"
        "当日废单买入不重试，滑点按订单成交回报口径。")

    # ── 四、执行链与档位 ──
    doc.add_heading("四、执行链与档位机制", level=1)
    doc.add_paragraph(
        "链路：Linux 信号（14:25 股票 / 14:28 CB）→ Windows fetch_and_execute → QMT。"
        "执行前预检：域隔离 / 价格新鲜度 / 资金口径（过渡期豁免仅限人工触发）。")
    t2 = doc.add_table(rows=4, cols=3)
    t2.style = "Table Grid"
    for i, r in enumerate([
        ("档位", "条件", "行为"),
        ("档 1 免确认", "单笔 <2 万 且 券非新增", "链直接执行"),
        ("档 2 清单确认", "其余（默认）", "先出清单（逐笔明细/差量对照/异常标记），人工审阅后 --approve 执行"),
        ("档 3 逐笔人工", "总交易额>30万 / 新增>8只 / 单笔>5万 / 闲置资金首次部署（任一）", "拒绝自动执行，按清单逐笔人工下单"),
    ]):
        for j, v in enumerate(r):
            t2.rows[i].cells[j].text = v
    doc.add_paragraph("原则：确认的对象是清单，不是“要不要跑”——没有清单就没有执行。")

    # ── 五、风控红线 ──
    doc.add_heading("五、风控红线", level=1)
    for line in [
        "单只仓位上限：Track A 5%；CB 等权（1/25，风控单券上限 5%×轨道资金）。",
        "行业/板块集中度上限：30%。",
        "单笔订单金额上限：10 万（防错单）。",
        "委托价必须为价位（tick）整数倍（qmt_client 单一收口）。",
        "涨停禁止买入、跌停禁止卖出。",
        "账户回撤熔断 15% → B 方案：停止新开仓+告警人工确认，持仓按 MA10 纪律退出。",
    ]:
        doc.add_paragraph(line, style="List Bullet")

    # ── 六、当前实盘状态 ──
    doc.add_heading("六、当前实盘状态（生成时点实读）", level=1)
    doc.add_paragraph(
        f"CB 腿：信号 {st['cb_holdings']} 只（capital 口径 {st['cb_capital']} 元，过渡期部分部署）；"
        f"账户持仓 {st['cb_positions']} 只、市值约 {st['cb_mv']:,.0f} 元（差量已对齐）。"
        "股票腿：熊档 0 持仓。打新底仓 511360 独立。")
    doc.add_paragraph(
        "近期事件：09-24 已清仓 4 只临期券（128134/110075/113605/110076）；"
        "停牌 8 只 + CC 评级 1 只已剔除出信号；09-30 调仓日将执行大换血。")

    # ── 七、09-30 调仓计划 ──
    doc.add_heading("七、09-30 调仓计划（预览）", level=1)
    doc.add_paragraph(
        "预览（09-24 机器值，模拟调仓日三层过滤走查）：预计卖出 5 笔、买入 19 笔，"
        "总交易额约 51.6 万元，档位=档 3 逐笔人工（总>30万+新增24只+闲置资金首次部署）。"
        "当日以 --plan-only 正式清单为准。该次调仓含一次性成分修正（快照重建+三层过滤+"
        "评级/强赎共享过滤首生效），换手成本不计入常态统计，执行需人工确认。")

    # ── 八、近期关键修复与闸门 ──
    doc.add_heading("八、近期关键修复与闸门（09-22~09-24）", level=1)
    for line in [
        "CB 废单机制定案：四类（数量非整手/债券停牌/仿真源冻结/09-01 残留），零笔归因价格偏离。",
        "停牌污染审计：V3 时点新鲜度口径 = 对外参考值；甲乙丙裁定=甲（可交易池补足 25 只）。",
        "档位分流机制：档1/2/3，先出清单再执行（今日“以为4笔实际7笔”从机制上关死）。",
        "到期三态：N=60 剔除 / M=120 禁买 / 60~120 缓冲带持有不加仓（规则驱动，无手工特例）。",
        "滑点口径修复：成交价用订单成交回报（T+5min 持仓成本价的假滑点 +4% 虚警已结案）。",
        "闸门二十一：信号与回测共用过滤代码；闸门二十二：数据边界不是世界事实。",
    ]:
        doc.add_paragraph(line, style="List Bullet")

    doc.add_paragraph(
        f"\n报告生成：{TODAY}。数字口径：组合/CB/股票数字为账本定案机器值；持仓/信号数为生成时点实读。")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    doc.save(OUT)
    print("已生成:", OUT, f"({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    build()
