"""
纸面交易每日跟踪脚本。
读取 logs/paper_trade_a_start.csv，
通过 MCP 拉取当日收盘价，计算持仓盈亏，
输出当日快照到 logs/paper_trade_YYYYMMDD.csv。

运行：
    python scripts/paper_trade_update.py
    python scripts/paper_trade_update.py --date 2026-05-12   # 指定日期
"""
import sys
import argparse
from pathlib import Path
from datetime import date

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from loguru import logger
from data.source.mcp_source import MCPSource

logger.add("logs/paper_trade_update.log", rotation="1 day", retention="30 days")

START_CSV_A = Path("logs/paper_trade_a_start.csv")
START_CSV_B = Path("logs/paper_trade_b_start.csv")
START_CSV   = START_CSV_A   # 默认 Track A，兼容旧调用


def fetch_prices(codes: list[str], trade_date: str) -> dict[str, float]:
    """通过 MCP 拉取当日收盘价。"""
    import math
    src = MCPSource()
    prices = {}
    for code in codes:
        try:
            df = src.get_daily(code, trade_date, trade_date)
            if not df.empty and "close" in df.columns:
                val = float(df.iloc[-1]["close"])
                # 过滤 NaN / 0 / 负数，否则 NaN 会导致合计行计算口径不一致
                if not math.isnan(val) and val > 0:
                    prices[code] = val
        except Exception as e:
            logger.warning(f"{code} 价格获取失败: {e}")
    logger.info(f"获取到 {len(prices)}/{len(codes)} 只价格")
    return prices


def calc_pnl(start: pd.DataFrame, prices: dict, trade_date: str) -> pd.DataFrame:
    """计算当日持仓盈亏。"""
    rows = []
    for _, r in start.iterrows():
        code        = str(r["代码"]).zfill(6)
        name        = r["名称"]
        industry    = r["行业"]
        cost_price  = float(r["建仓参考价"])
        shares      = int(r["股数(股)"])
        cost_value  = float(r["建仓金额(元)"])
        cur_price   = prices.get(code, None)

        if cur_price is None:
            cur_value = cost_value
            pnl       = 0.0
            pnl_pct   = 0.0
            status    = "无数据"
        else:
            cur_value = round(cur_price * shares, 2)
            pnl       = round(cur_value - cost_value, 2)
            pnl_pct   = round((cur_price / cost_price - 1) * 100, 2)
            status    = "正常"

        rows.append({
            "代码":         code,
            "名称":         name,
            "行业":         industry,
            "建仓价(元)":   cost_price,
            "股数(股)":     shares,
            "建仓金额(元)": cost_value,
            f"{trade_date}收盘(元)": cur_price if cur_price else "--",
            "当前市值(元)": cur_value,
            "浮动盈亏(元)": pnl,
            "涨跌幅(%)":    pnl_pct,
            "状态":         status,
        })

    df = pd.DataFrame(rows)

    # 组合汇总行
    total_cost = df["建仓金额(元)"].sum()
    total_cur  = df["当前市值(元)"].sum()
    total_pnl  = df["浮动盈亏(元)"].sum()
    total_pct  = round((total_cur / total_cost - 1) * 100, 2) if total_cost else 0

    summary = {
        "代码": "合计", "名称": "", "行业": "",
        "建仓价(元)": "", "股数(股)": df["股数(股)"].sum(),
        "建仓金额(元)": round(total_cost, 2),
        f"{trade_date}收盘(元)": "",
        "当前市值(元)": round(total_cur, 2),
        "浮动盈亏(元)": round(total_pnl, 2),
        "涨跌幅(%)": total_pct,
        "状态": f"{'盈利' if total_pnl >= 0 else '亏损'}",
    }
    df = pd.concat([df, pd.DataFrame([summary])], ignore_index=True)
    return df


def _run_track(track: str, trade_date: str):
    """单个 track 的纸面交易更新逻辑。"""
    csv = START_CSV_B if track == "b" else START_CSV_A
    prefix = f"paper_trade_b" if track == "b" else "paper_trade"

    if not csv.exists():
        logger.warning(f"Track {track.upper()} 建仓文件不存在: {csv}，跳过")
        return

    start = pd.read_csv(csv, dtype={"代码": str}, encoding="utf-8-sig")
    start["代码"] = start["代码"].astype(str).str.zfill(6)
    codes = start["代码"].tolist()

    prices   = fetch_prices(codes, trade_date)
    result   = calc_pnl(start, prices, trade_date)

    out_path = Path(f"logs/{prefix}_{trade_date.replace('-', '')}.csv")
    result.to_csv(out_path, index=False, encoding="utf-8-sig")

    summary = result[result["代码"] == "合计"].iloc[0]
    pnl     = float(summary["浮动盈亏(元)"])
    pnl_pct = float(summary["涨跌幅(%)"])
    sign    = "+" if pnl >= 0 else ""
    tag     = f"Track {'B' if track == 'b' else 'A'} 纸面交易日报"
    logger.info(
        f"【{tag} {trade_date}】\n"
        f"  建仓总额: {summary['建仓金额(元)']:,.0f} 元\n"
        f"  当前市值: {summary['当前市值(元)']:,.0f} 元\n"
        f"  浮动盈亏: {sign}{pnl:,.0f} 元  ({sign}{pnl_pct:.2f}%)\n"
        f"  结果文件: {out_path}"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date",  default=date.today().strftime("%Y-%m-%d"))
    parser.add_argument("--track", default="both", choices=["a", "b", "both"],
                        help="a=Track A, b=Track B, both=两个都跑（默认）")
    args = parser.parse_args()
    trade_date = args.date

    logger.info(f"纸面交易更新: {trade_date}")

    tracks = ["a", "b"] if args.track == "both" else [args.track]
    for t in tracks:
        _run_track(t, trade_date)


if __name__ == "__main__":
    main()
