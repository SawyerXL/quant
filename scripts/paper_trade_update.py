"""
纸面交易每日跟踪脚本。

数据源优先级（方向2实现）：
  1. QMT 实际持仓（logs/qmt_positions_latest.json，≤2天有效）
  2. 回退到 paper_trade_a_start.csv（信号理想持仓）

运行：
    python scripts/paper_trade_update.py
    python scripts/paper_trade_update.py --date 2026-05-12
"""
import sys, argparse, json, math
from pathlib import Path
from datetime import date, datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from loguru import logger
from data.source.mcp_source import MCPSource

logger.add("logs/paper_trade_update.log", rotation="1 day", retention="30 days")

START_CSV_A = Path("logs/paper_trade_a_start.csv")
START_CSV_B = Path("logs/paper_trade_b_start.csv")
START_CSV   = START_CSV_A
QMT_FILE    = Path("logs/qmt_positions_latest.json")
MAX_QMT_AGE = 2   # 超过2天的QMT快照视为过期


def _build_info_cache() -> dict:
    """预加载股票名称/行业映射（从 stock_info_full）。"""
    from data.storage import load_meta
    info = load_meta("stock_info_full")
    if info.empty:
        return {}
    info["code"] = info["code"].astype(str).str.zfill(6)
    cache = {}
    for _, row in info.iterrows():
        cache[row["code"]] = {
            "name": row.get("name", "?"),
            "industry": row.get("industry_l1", "其他"),
        }
    return cache


def load_qmt_positions() -> pd.DataFrame | None:
    """
    读取 QMT 真实持仓，返回 calc_pnl 所需的 DataFrame 格式。
    若文件不存在或过期返回 None，调用方应回退到旧 CSV。
    """
    if not QMT_FILE.exists():
        return None

    data = json.loads(QMT_FILE.read_text(encoding="utf-8"))

    # 检查新鲜度
    exported_at = data.get("exported_at", "")
    if exported_at:
        try:
            exp_dt = datetime.fromisoformat(exported_at)
            age_days = (datetime.now() - exp_dt).days
            if age_days > MAX_QMT_AGE:
                logger.warning(f"QMT快照已{age_days}天前过期，回退到信号持仓文件")
                return None
        except Exception:
            pass

    pos_raw = data.get("positions", {})
    if not pos_raw:
        logger.warning("QMT持仓为空，回退到信号持仓文件")
        return None

    info_cache = _build_info_cache()
    rows = []

    for code, p in pos_raw.items():
        # 去交易所后缀
        clean_code = code.split(".")[0].zfill(6)
        vol = p.get("volume", 0)
        if vol <= 0:
            continue
        cost_price = p.get("cost_price", 0)
        cost_value = round(cost_price * vol, 2)
        info = info_cache.get(clean_code, {"name": "?", "industry": "其他"})

        rows.append({
            "代码":       clean_code,
            "名称":       info["name"],
            "行业":       info["industry"],
            "建仓参考价":  cost_price,
            "股数(股)":   int(vol),
            "建仓金额(元)": cost_value,
            "备注":       f"QMT({exported_at[:10]})",
        })

    df = pd.DataFrame(rows).sort_values("建仓金额(元)", ascending=False)
    logger.info(f"读取QMT真实持仓: {len(df)} 只, 建仓总额: {df['建仓金额(元)'].sum():,.0f}, 快照: {exported_at[:16]}")

    # Track B 的判断：从 signal_b_latest.json 取持仓列表
    sig_b_path = Path("data_store/meta/signal_b_latest.json")
    if sig_b_path.exists():
        try:
            sig_b = json.loads(sig_b_path.read_text(encoding="utf-8"))
            b_codes = set(sig_b.get("holdings", []))
            # 不做过滤，只标记
            # df 中的每行我们直接传给 calc_pnl
        except Exception:
            pass

    return df


# ── 价格拉取 & PnL 计算（不变）───────────────────────────────────

def fetch_prices(codes: list[str], trade_date: str) -> dict[str, float]:
    """优先用本地日线数据拉取收盘价，失败时才走 MCP。"""
    from data.storage import load_daily
    prices = {}
    for code in codes:
        try:
            df = load_daily(code, trade_date, trade_date)
            if not df.empty and "close" in df.columns:
                val = float(df.iloc[-1]["close"])
                if not math.isnan(val) and val > 0:
                    prices[code] = val
                    continue
        except Exception:
            pass

        # 回退到 MCP
        try:
            src = MCPSource()
            df = src.get_daily(code, trade_date, trade_date)
            if not df.empty and "close" in df.columns:
                val = float(df.iloc[-1]["close"])
                if not math.isnan(val) and val > 0:
                    prices[code] = val
        except Exception as e:
            logger.debug(f"{code} MCP也失败: {e}")

    logger.info(f"获取到 {len(prices)}/{len(codes)} 只价格（优先本地日线）")
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
            pnl_pct   = round((cur_price / cost_price - 1) * 100, 2) if cost_price else 0
            status    = "正常"

        note = r.get("备注", "")
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
            "备注":         note,
        })

    df = pd.DataFrame(rows)

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
        "备注": "",
    }
    df = pd.concat([df, pd.DataFrame([summary])], ignore_index=True)
    return df


def _run_track_qmt(trade_date: str, label: str, start: pd.DataFrame,
                   out_prefix: str):
    """基于 QMT 持仓做纸面跟踪。"""
    if start.empty:
        return
    codes   = start["代码"].tolist()
    prices  = fetch_prices(codes, trade_date)
    result  = calc_pnl(start, prices, trade_date)

    out_path = Path(f"logs/{out_prefix}_{trade_date.replace('-', '')}.csv")
    result.to_csv(out_path, index=False, encoding="utf-8-sig")

    summary = result[result["代码"] == "合计"].iloc[0]
    pnl     = float(summary["浮动盈亏(元)"])
    pnl_pct = float(summary["涨跌幅(%)"])
    tag     = f"{label} 纸面交易日报"
    source_info = start.iloc[0].get("备注", "")
    logger.info(
        f"【{tag} {trade_date}】来源={source_info}\n"
        f"  建仓总额: {summary['建仓金额(元)']:,.0f} 元\n"
        f"  当前市值: {summary['当前市值(元)']:,.0f} 元\n"
        f"  浮动盈亏: {'+' if pnl >= 0 else ''}{pnl:,.0f} 元  ({'+' if pnl_pct >= 0 else ''}{pnl_pct:.2f}%)\n"
        f"  结果文件: {out_path}"
    )


def _run_track_csv(track: str, trade_date: str):
    """旧版：基于纸面建仓CSV（信号理想持仓）做跟踪。"""
    csv = START_CSV_B if track == "b" else START_CSV_A
    prefix = f"paper_trade_b" if track == "b" else "paper_trade"

    if not csv.exists():
        logger.warning(f"Track {track.upper()} 建仓文件不存在: {csv}，跳过")
        return

    start = pd.read_csv(csv, dtype={"代码": str}, encoding="utf-8-sig")
    start["代码"] = start["代码"].astype(str).str.zfill(6)
    _run_track_qmt(trade_date, f"Track {'B' if track == 'b' else 'A'}(信号)",
                   start, prefix)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date",  default=date.today().strftime("%Y-%m-%d"))
    parser.add_argument("--track", default="both", choices=["a", "b", "both"],
                        help="a=Track A, b=Track B, both=both")
    args = parser.parse_args()
    trade_date = args.date

    logger.info(f"纸面交易更新: {trade_date}")

    # ── 优先用 QMT 真实持仓 ───────────────────────────────────
    qmt_df = load_qmt_positions()

    if qmt_df is not None and not qmt_df.empty:
        # 纯 QMT 数据：整个账户（不分 Track A/B）
        _run_track_qmt(trade_date, "QMT", qmt_df, "paper_trade")

        # 同时生成 Track B 的 file（如果 signal_b 有持仓）
        sig_b_path = Path("data_store/meta/signal_b_latest.json")
        if sig_b_path.exists():
            sig_b = json.loads(sig_b_path.read_text(encoding="utf-8"))
            b_codes = set(sig_b.get("holdings", []))
            b_df = qmt_df[qmt_df["代码"].isin(b_codes)].copy()
            if not b_df.empty:
                _run_track_qmt(trade_date, "Track B(QMT)", b_df, "paper_trade_b")

        # 也保留旧的信号CSV快照作为对照
        if args.track != "b":
            csv_a = START_CSV_A
            if csv_a.exists():
                start_a = pd.read_csv(csv_a, dtype={"代码": str}, encoding="utf-8-sig")
                start_a["代码"] = start_a["代码"].astype(str).str.zfill(6)
                _run_track_qmt(trade_date, "Track A(信号对照)", start_a, "paper_trade_signal")
        return

    # ── 回退：无 QMT 数据时用旧 CSV ────────────────────────────
    logger.info("无QMT持仓数据，回退到信号理想持仓文件")
    tracks = ["a", "b"] if args.track == "both" else [args.track]
    for t in tracks:
        _run_track_csv(t, trade_date)


if __name__ == "__main__":
    main()
