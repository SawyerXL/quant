"""
隔夜美盘→A股盘前判断
数据: 标普500/纳斯达克/道指/VIV/富时A50期货
输出: 今日A股走向预判(高开低走/低开高走/单边大跌/震荡等)
"""
import pandas as pd, numpy as np
from datetime import datetime, timedelta
import akshare as ak
from loguru import logger

def get_overnight_analysis() -> dict:
    """返回隔夜市场的完整分析dict，供监测邮件使用。"""
    result = {
        "sp500": None, "sp500_chg": None,
        "nasdaq": None, "nasdaq_chg": None,
        "dow": None, "dow_chg": None,
        "judgment": "数据获取失败",
        "detail": [],
        "confidence": "低",
    }

    try:
        # ── 1. 标普500 ────────────────────────────────
        sp = ak.index_us_stock_sina(symbol=".INX")
        if not sp.empty:
            result["sp500"] = float(sp.iloc[-1]["close"])
            prev = float(sp.iloc[-2]["close"]) if len(sp) >= 2 else result["sp500"]
            result["sp500_chg"] = (result["sp500"] / prev - 1) * 100 if prev else None
    except Exception as e:
        logger.warning(f"标普500: {e}")

    try:
        # ── 2. 纳斯达克 ───────────────────────────────
        nq = ak.index_us_stock_sina(symbol=".IXIC")
        if not nq.empty:
            result["nasdaq"] = float(nq.iloc[-1]["close"])
            prev = float(nq.iloc[-2]["close"]) if len(nq) >= 2 else result["nasdaq"]
            result["nasdaq_chg"] = (result["nasdaq"] / prev - 1) * 100 if prev else None
    except Exception as e:
        logger.warning(f"纳斯达克: {e}")

    try:
        # ── 3. 道琼斯 ─────────────────────────────────
        dj = ak.index_us_stock_sina(symbol=".DJI")
        if not dj.empty:
            result["dow"] = float(dj.iloc[-1]["close"])
            prev = float(dj.iloc[-2]["close"]) if len(dj) >= 2 else result["dow"]
            result["dow_chg"] = (result["dow"] / prev - 1) * 100 if prev else None
    except Exception as e:
        logger.warning(f"道琼斯: {e}")

    # ── 5. 生成判断 ──────────────────────────────────
    sp_chg = result["sp500_chg"]
    nq_chg = result["nasdaq_chg"]

    if sp_chg is None:
        return result

    details = []
    confidence = "中"

    # 纳指是否领涨/领跌
    nq_lead = nq_chg is not None and abs(nq_chg) > abs(sp_chg) * 1.3

    if sp_chg > 0:
        if sp_chg > 1.5:
            details.append(f"美股大涨(标普{sp_chg:+.1f}%)")
            if nq_lead and nq_chg > sp_chg:
                details.append(f"纳指领涨{nq_chg:+.1f}%,科技股强势")
                judgment = "大概率高开,科技/成长板块领涨。高开>1%后防高开低走,前30分钟不追"
            else:
                judgment = "美股大涨,大概率高开。高开>1%后防高开低走"
            confidence = "中高"
        elif sp_chg > 0.5:
            details.append(f"美股温和上涨(标普{sp_chg:+.1f}%)")
            judgment = "偏正面,小幅高开或平开。方向跟随美股"
        else:
            details.append(f"美股微涨(标普{sp_chg:+.1f}%)")
            judgment = "影响有限,今日A股独立运行概率大"

    elif sp_chg < 0:
        if sp_chg < -2.0:
            details.append(f"美股暴跌(标普{sp_chg:+.1f}%)")
            judgment = "大概率大幅低开。低开>2%且缩量→可能是低开高走机会;放量低开→真跌,持仓止损优先"
            confidence = "高"
        elif sp_chg < -1.0:
            details.append(f"美股明显下跌(标普{sp_chg:+.1f}%)")
            if nq_lead and nq_chg < -1.0:
                details.append(f"纳指领跌{nq_chg:+.1f}%,成长股承压")
            judgment = "大概率低开。关注低开后能否企稳——若开盘放量下跌,持仓触及止损的优先处理"
        elif sp_chg < -0.5:
            details.append(f"美股小跌(标普{sp_chg:+.1f}%)")
            judgment = "偏负面,可能小幅低开。影响有限,观察开盘后资金流向"
        else:
            details.append(f"美股微跌(标普{sp_chg:+.1f}%)")
            judgment = "影响有限,今日A股独立运行概率大"
    else:
        judgment = "美股平收,今日A股走势取决于国内因素"

    # 纳指结构
    if nq_lead and nq_chg is not None:
        if nq_chg < -1.5:
            details.append(f"纳指领跌{nq_chg:+.1f}%,成长股杀跌,小盘/科技承压")
        elif nq_chg > 1.5:
            details.append(f"纳指领涨{nq_chg:+.1f}%,科技/创业板受益")

    # ── 6. 北向资金 ──────────────────────────────────
    try:
        hist_sh = ak.stock_hsgt_hist_em(symbol="沪股通")
        valid = hist_sh[hist_sh["当日成交净买额"].notna()]
        if len(valid) >= 5:
            recent = valid.tail(5)
            flows = recent["当日成交净买额"].values
            flow_5d = sum(float(f) for f in flows if pd.notna(f))
            if all(f > 0 for f in flows): trend = "持续流入"
            elif all(f < 0 for f in flows): trend = "持续流出"
            else: trend = "进出波动"
            result["northbound_flow"] = {"flow_5d": flow_5d, "trend": trend}
    except Exception:
        pass

    result["judgment"] = judgment
    result["detail"] = details
    result["confidence"] = confidence
    return result


def format_overnight_report(analysis: dict) -> str:
    """格式化隔夜分析为邮件文本。"""
    lines = ["🌍 隔夜美盘 → 今日A股预判", "─" * 36]

    # 数据行
    data_parts = []
    if analysis["sp500"] is not None:
        sp = analysis["sp500"]; chg = analysis["sp500_chg"]
        data_parts.append(f"标普{sp:.0f}({chg:+.2f}%)")
    if analysis["nasdaq"] is not None:
        nq = analysis["nasdaq"]; chg = analysis["nasdaq_chg"]
        data_parts.append(f"纳指{nq:.0f}({chg:+.2f}%)")
    if analysis["dow"] is not None:
        dj = analysis["dow"]; chg = analysis["dow_chg"]
        data_parts.append(f"道指{dj:.0f}({chg:+.2f}%)")
    if data_parts:
        lines.append("  " + " | ".join(data_parts))

    # 细节
    for d in analysis.get("detail", []):
        lines.append(f"  {d}")

    # 北向资金
    nf = analysis.get("northbound_flow")
    if nf is not None:
        lines.append(f"  💰 北向资金近5日: {nf['flow_5d']:+.0f}亿 | 趋势: {nf['trend']}")

    # 判断
    conf = analysis.get("confidence", "中")
    lines.append("")
    lines.append(f"  📌 今日判断(置信度:{conf})")
    lines.append(f"  {analysis.get('judgment', '数据不足')}")
    lines.append("─" * 36)

    return "\n".join(lines)


if __name__ == "__main__":
    # 独立运行: 查看今晚美盘数据
    print("获取隔夜美盘数据...")
    result = get_overnight_analysis()
    print(format_overnight_report(result))
