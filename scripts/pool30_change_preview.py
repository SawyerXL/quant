"""
pool30 变动预览 (2026-09-12 用户要求的周一实盘防护) —
归一改变了成交额排名 → 周一的信号将首次在正确排名上计算, 若元口径
票此前长期霸占 TOP30, 单次调仓换手可能远超正常。本脚本在 14:25
信号生成前用归一后的库重算 pool30, 与现网持仓 diff:
  预期买卖笔数 > 15 → 提示暂停自动执行/人工确认/分批摊平
顺带产出污染程度实测: diff 越大=此前排名错得越厉害。
口径: 与 daily_signal 的池逻辑一致(20日均成交额 top60 → vol20>5
剔除 → 取30; 黑名单排除; 用库内最新交易日截面)。
用法: python scripts/pool30_change_preview.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import json
import numpy as np
import pandas as pd

from data.storage import load_daily, load_meta
from backtest_return_attribution import load_blacklist

MAX_VOL20 = 5.0


def main():
    blacklist = load_blacklist()
    meta = load_meta("stock_info_full")
    codes = [str(c).zfill(6) for c in meta["code"].tolist()
             if str(c).zfill(6) not in blacklist]

    # 库内最新交易日
    sh = load_daily("SH000001", "2026-09-01", "2026-12-31")
    if sh.empty:
        print("指数无数据")
        return
    last = str(sh["date"].max())[:10]
    print(f"截面日期: {last}")

    # 20 日均成交额 top60
    rows = []
    for c in codes:
        d = load_daily(c, "2026-01-01", last)
        if d.empty:
            continue
        d = d[d["date"] <= last].sort_values("date")
        amt = pd.to_numeric(d["amount"], errors="coerce")
        cl = pd.to_numeric(d["close"], errors="coerce")
        amt = amt[amt > 0].tail(20)
        if len(amt) < 10 or cl.empty or cl.iloc[-1] <= 0:
            continue
        hist = cl.dropna()
        rets = hist.pct_change()
        win = rets.iloc[-21:-1]  # T-1 严格
        vol20 = float(win.std() * 100) if len(win) >= 15 else 999.0
        rows.append((c, float(amt.mean()), vol20))
    rows.sort(key=lambda x: -x[1])
    pool60 = [c for c, _, _ in rows[:60]]
    pool30 = [c for c, _, v in rows[:60] if v <= MAX_VOL20][:30]
    print(f"top60 门槛: {rows[59][1]:.0f} 万元/日(库已万元口径, 异常小=池子漂移)")
    print(f"pool30({len(pool30)}只): {sorted(pool30)}")

    # 现网持仓: 最新信号文件
    cur = set()
    sig_regime = "unknown"
    for g in ("g0", "g1"):
        p = Path(f"data_store/meta/signal_a_{g}.json")
        if p.exists():
            try:
                sig = json.loads(p.read_text(encoding="utf-8"))
                sig_regime = sig.get("regime", sig_regime)
                cur |= {str(h).zfill(6) for h in sig.get("holdings", [])}
                print(f"现网信号 {g}: 日期{sig.get('signal_date')} "
                      f"regime={sig.get('regime')} 持仓{len(sig.get('holdings', []))}只")
            except Exception:
                pass
    target = set(pool30)
    buys = sorted(target - cur)
    sells = sorted(cur - target)
    n = len(buys) + len(sells)
    regime_note = ""
    if sig_regime == "bear":
        # bear 档清仓语义: 目标持仓=0, pool30 仅作档位恢复后的预备信息
        buys, sells, n = [], [], 0
        regime_note = f"\n注: 现网 regime=bear(pos_ratio≤0.3, 清仓档), 目标持仓=0, "
        regime_note += "实际预期交易=0; 上表 pool30 为档位恢复后的预备池"
    print(f"\n预期变动: 买 {len(buys)} / 卖 {len(sells)} / 总 {n} 笔{regime_note}")
    if buys:
        print("  买入:", buys)
    if sells:
        print("  卖出:", sells)
    if n > 15:
        print(f"🔴 变动 {n} 笔 > 15 阈值: 建议暂停自动执行→人工确认→分2~3次摊平")
    elif n > 0:
        print(f"🟡 变动 {n} 笔, 属可接受范围(≤15)")
    else:
        print("✅ 无变动")


if __name__ == "__main__":
    main()
