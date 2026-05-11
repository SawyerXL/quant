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

START_CSV = Path("logs/paper_trade_a_start.csv")


def fetch_prices(codes: list[str], trade_date: str) -> dict[str, float]:
    """通过 MCP 拉取当日收盘价。"""
    src = MCPSource()
    prices = {}
    for code in codes:
        try:
            df = src.get_daily(code, trade_date, trade_date)
            if not df.empty and "close" in df.columns:
                prices[code] = float(df.iloc[-1]["close"])
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=date.today().strftime("%Y-%m-%d"))
    args = parser.parse_args()
    trade_date = args.date

    logger.info(f"纸面交易更新: {trade_date}")

    if not START_CSV.exists():
        logger.error(f"建仓文件不存在: {START_CSV}")
        sys.exit(1)

    start = pd.read_csv(START_CSV, dtype={"代码": str})
    start["代码"] = start["代码"].astype(str).str.zfill(6)
    codes = start["代码"].tolist()

    prices   = fetch_prices(codes, trade_date)
    result   = calc_pnl(start, prices, trade_date)

    out_path = Path(f"logs/paper_trade_{trade_date.replace('-', '')}.csv")
    result.to_csv(out_path, index=False, encoding="utf-8-sig")

    # 控制台摘要
    summary = result[result["代码"] == "合计"].iloc[0]
    pnl     = float(summary["浮动盈亏(元)"])
    pnl_pct = float(summary["涨跌幅(%)"])
    sign    = "+" if pnl >= 0 else ""
    logger.info(
        f"【纸面交易日报 {trade_date}】\n"
        f"  建仓总额: {summary['建仓金额(元)']:,.0f} 元\n"
        f"  当前市值: {summary['当前市值(元)']:,.0f} 元\n"
        f"  浮动盈亏: {sign}{pnl:,.0f} 元  ({sign}{pnl_pct:.2f}%)\n"
        f"  结果文件: {out_path}"
    )
    print(result[result["代码"] != "合计"][
        ["代码", "名称", "涨跌幅(%)", "浮动盈亏(元)"]
    ].sort_values("涨跌幅(%)", ascending=False).to_string(index=False))


if __name__ == "__main__":
    main()
