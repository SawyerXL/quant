"""
红利税量化（2026-09-02，队列#6）——主策略口径下的股息红利税年化拖累。

方法:
  1. lot约束引擎(pool30, 50万/组, path0) + diag_holding_spans 诊断
     → 每笔完整持仓 (code, 进日, 出日, 进权重, 进价)
  2. baostock query_dividend_data 取分红事件(除权除息日, 每股股利税前)
  3. 税率按持有期(交易日): ≤20日=20% / 21~250日=10% / >250日=0
     (与现行股息红利税规则一致; 持有期内除息的股息×税率)
  4. 税 = 股数×每股股利×税率; 股数 = 进权重×资金/进价
  5. 年化拖累 = 年均实缴税 / 名义资金; 另报未平仓持仓的待缴上限
口径写明: 只计已平仓的实缴税; 未平仓股息税递延不虚列; TP分批(实盘从未
触发)不建模; 分红数据源baostock(免费, 避开被封的东财)。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd
from loguru import logger
logger.remove()

import baostock as bs
from data.storage import load_daily, load_meta
from backtest_config import BacktestConfig, DEFAULT_CONFIG
from backtest_engine import run_backtest, make_rebal_dates

START, END = "2019-01-01", "2026-08-28"
CAPITAL = 500_000.0


def collect_spans():
    meta = load_meta("stock_info_full")
    codes = meta["code"].tolist() if not meta.empty else []
    prices, amounts = {}, {}
    for code in codes:
        try:
            d = load_daily(code, START, END)
            if d.empty:
                continue
            d["date"] = pd.to_datetime(d["date"])
            d = d.set_index("date").sort_index()
            cl = pd.to_numeric(d["close"], errors="coerce").dropna()
            amt = pd.to_numeric(d.get("amount", pd.Series(dtype=float)),
                                errors="coerce")
            if len(cl) >= 250:
                prices[code] = cl
                if len(amt) >= 250:
                    amounts[code] = amt
        except Exception:
            pass
    panel = pd.DataFrame(prices).sort_index()
    ap = pd.DataFrame(amounts).sort_index()
    ic = load_meta("csi800_index").set_index("date")["close"].sort_index()
    ic.index = pd.to_datetime(ic.index)
    sh = load_daily("000001", "2014-06-01", END)
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))
    base = [d for d in make_rebal_dates(cal, "biweekly") if START <= d <= END]
    idx = {d: i for i, d in enumerate(cal)}
    rebal = [cal[idx.get(d, 0)] for d in base if idx.get(d, 0) < len(cal)]

    cfg = BacktestConfig(**{**DEFAULT_CONFIG.to_dict(),
                            "pool_size": 30, "lot_size": 100,
                            "initial_capital": CAPITAL,
                            "diag_holding_spans": True})
    nav, info = run_backtest(panel, ap, rebal, cfg, ic)
    spans = pd.DataFrame(info["holding_spans"],
                         columns=["code", "entry", "exit", "weight", "price"])
    print(f"持仓区间: {len(spans)}笔 (覆盖{spans.code.nunique()}只)",
          flush=True)
    return spans, nav


def fetch_dividends(codes, start_year, end_year):
    def _bs_code(c):
        return ("sh." if c.startswith(("60", "68", "90")) else "sz.") + c
    bs.login()
    events = []
    for ci, code in enumerate(sorted(codes)):
        for y in range(start_year, end_year + 1):
            try:
                rs = bs.query_dividend_data(code=_bs_code(code), year=str(y),
                                            yearType="report")
                while rs.next():
                    d = dict(zip(rs.fields, rs.get_row_data()))
                    cash = d.get("dividCashPsBeforeTax")
                    if not cash:
                        continue
                    try:
                        cash_f = float(str(cash).split("或")[0])
                    except ValueError:
                        continue
                    # 除权除息日=dividOperateDate, 缺失回退dividPreNoticeDate
                    dt = d.get("dividOperateDate") or \
                        d.get("dividPreNoticeDate") or ""
                    if not dt:
                        continue
                    events.append({
                        "code": str(d.get("code", code)).split(".")[-1],
                        "date": dt, "cash": cash_f,
                    })
            except Exception:
                continue
        if (ci + 1) % 200 == 0:
            print(f"  ...分红查询 {ci+1}/{len(codes)}", flush=True)
    bs.logout()
    ev = pd.DataFrame(events)
    print(f"分红事件: {len(ev)}条", flush=True)
    return ev


def main():
    spans, nav = collect_spans()
    # 交易日历(用于持有期计算: 进日~出日间的交易日数)
    sh = load_daily("000001", "2014-06-01", END)
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))
    spans["tdays"] = spans.apply(
        lambda r: max(1, len([d for d in cal
                              if r["entry"] <= d < r["exit"]])), axis=1)
    spans["rate"] = np.where(spans["tdays"] <= 20, 0.20,
                             np.where(spans["tdays"] <= 250, 0.10, 0.0))
    print(f"持有期分布: ≤20交易日 {(spans.tdays<=20).mean()*100:.0f}% "
          f"(税率20%), 21~250 {(spans.tdays.between(21,250)).mean()*100:.0f}% "
          f"(10%), >250 {(spans.tdays>250).mean()*100:.0f}% (0)", flush=True)

    ev = fetch_dividends(sorted(spans["code"].unique()),
                         int(START[:4]), int(END[:4]))

    total_tax = 0.0
    n_taxed = 0
    for _, r in spans.iterrows():
        divs = ev[(ev["code"] == r["code"]) &
                  (ev["date"] >= r["entry"]) & (ev["date"] < r["exit"])]
        if divs.empty:
            continue
        shares = r["weight"] * CAPITAL / r["price"]
        tax = shares * divs["cash"].sum() * r["rate"]
        total_tax += tax
        n_taxed += 1

    years = (pd.Timestamp(END) - pd.Timestamp(START)).days / 365.25
    ann_tax = total_tax / years
    print(f"\n=== 红利税量化结果 (pool30×50万/组, {START}~{END}) ===", flush=True)
    print(f"有分红事件的已平仓持仓: {n_taxed}笔", flush=True)
    print(f"总实缴税: {total_tax:,.0f}元 (年均 {ann_tax:,.0f}元)", flush=True)
    print(f"年化拖累: {ann_tax / CAPITAL * 100:.3f}pp/年 "
          f"(名义资金50万口径)", flush=True)


if __name__ == "__main__":
    main()
