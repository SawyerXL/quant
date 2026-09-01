"""
Track A 每日信号生成 — v2 简化版

变更(vs A-4):
  - 选股: 成交额TOP30 (替代因子打分, 回测+1.2pp)
  - 权重: 等权 1/N (替代 compute_weights)
  - 砍掉: dynamic_grace, SECTOR_BOOST, hold_counts, entry_prices保护期
  - 保留: MA200择时, MA10连续3日跌破出清, 组合止损

运行: python scripts/daily_signal_a_v2.py
Cron: 25 14 * * 1-5
"""
import json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from datetime import date, datetime, timedelta
import numpy as np, pandas as pd
from loguru import logger

from run_backtest_a2 import get_position_ratio
from run_backtest_a import load_panels
from data.storage import load_meta, load_daily
from monitoring.alerts import send_alert

logger.add("logs/signal_a_v2_{time:YYYY-MM-DD}.log", rotation="1 day", retention="60 days")

TRACK_A_CAPITAL = 1_000_000
N_HOLDINGS      = 60
MAX_VOL20       = 5.0   # v2.1 拥挤度过滤: 剔除20日波动率>5%的票(严格口径, 不含最近一日)
MA10_EXIT_DAYS  = 3
SIGNAL_FILE     = Path("data_store/meta/signal_a_latest.json")

# ── 工具函数 ────────────────────────────────────────
def _get_trade_calendar() -> list[str]:
    cal = load_meta("trade_calendar")
    return sorted(cal["trade_date"].tolist()) if not cal.empty else []

def is_trade_day(today: str, calendar: list[str]) -> bool:
    return today in calendar

def is_rebalance_day(today: str, calendar: list[str]) -> bool:
    """双周调仓: 月末倒数第二 + 月中"""
    year_month  = today[:7]
    month_dates = sorted([d for d in calendar if d.startswith(year_month)])
    if not month_dates: return False
    end_idx = -2 if len(month_dates) >= 2 else -1
    is_month_end = (today == month_dates[end_idx])
    n = len(month_dates)
    mid_idx = max(0, n // 2 - 1)
    is_month_mid = (n >= 2 and today == month_dates[mid_idx])
    return is_month_end or is_month_mid

def _get_position_ratio(today: str) -> float:
    idx_df = load_meta("csi800_index")
    if idx_df.empty: return 0.70
    idx_df["date"] = pd.to_datetime(idx_df["date"])
    close = idx_df.set_index("date")["close"].sort_index()
    close = pd.to_numeric(close, errors="coerce").dropna()
    return get_position_ratio(close, pd.Timestamp(today))

def _load_prev_signal() -> dict:
    if not SIGNAL_FILE.exists(): return {}
    try: return json.loads(SIGNAL_FILE.read_text(encoding="utf-8"))
    except Exception: return {}

def _check_ma10_exits(holdings, today, prev_days_below):
    """检查持仓是否连续 MA10_EXIT_DAYS 跌破 MA10。用 load_daily 直接加载(只需15天)。"""
    if not holdings: return [], {}
    start = (date.fromisoformat(today) - timedelta(days=25)).strftime("%Y-%m-%d")
    exits, new_days = [], {}
    for code in holdings:
        try:
            df = load_daily(code, start, today)
        except Exception:
            new_days[code] = prev_days_below.get(code, 0); continue
        if df.empty or len(df) < 10:
            new_days[code] = 0; continue
        df = df.sort_values("date")
        closes = pd.to_numeric(df["close"], errors="coerce").dropna()
        if len(closes) < 10: new_days[code] = 0; continue
        ma10, cur_p = closes.iloc[-10:].mean(), closes.iloc[-1]
        new_days[code] = prev_days_below.get(code, 0) + 1 if cur_p < ma10 else 0
        if new_days[code] >= MA10_EXIT_DAYS: exits.append(code)
    return exits, new_days

def _select_top_turnover(amt_panel, prices, capital, n=60):
    """
    选成交额最大的 N 只（v2.2: TOP60），v2.1 拥挤度过滤（vol20>5% 剔除），
    再跳过买不起一手的科创板高价股。capital: 总资金 → 单只预算 = capital/n
    """
    avg = amt_panel.tail(20).mean().dropna()
    budget = capital / n
    today = str(date.today())
    vol_start = (date.today() - timedelta(days=150)).strftime("%Y-%m-%d")
    # 先取 2N 候补, 拥挤度过滤后逐个检查买得起, 直到凑满 N
    affordable = []
    for code in avg.nlargest(n * 2).index:
        if len(affordable) >= n:
            break
        # v2.1 拥挤度过滤: 只用 T-1 及以前(严格口径, 与回测 backtest_engine 一致)
        try:
            d = load_daily(code, vol_start, today)
            if not d.empty and len(d) >= 22:
                d = d.sort_values("date")
                cl = pd.to_numeric(d["close"], errors="coerce").dropna()
                if len(cl) >= 22:
                    vol20 = float(cl.pct_change().iloc[-21:-1].std() * 100)
                    if vol20 > MAX_VOL20:
                        continue
        except Exception:
            pass
        p = prices.get(code)
        if p and not pd.isna(p) and p > 0:
            min_lot = 200 if str(code).startswith("688") else 100
            if p * min_lot <= budget:
                affordable.append(code)
    return affordable[:n]

def _calc_shares(holdings, prices, total_capital):
    """等权计算股数，不足一手跳过。"""
    shares = {}
    n = len(holdings)
    for code in holdings:
        price = prices.get(code)
        if price and not np.isnan(float(price)) and float(price) > 0:
            capital  = total_capital / n
            min_lot  = 200 if str(code).startswith("688") else 100
            lots     = int(capital / float(price) / min_lot)
            shares[code] = lots * min_lot
        else:
            shares[code] = 0
    return shares

# ══════════════════════════════════════════════════════════════════
def _load_actual_qmt_holdings(calendar):
    """读实盘QMT快照(logs/qmt_positions_latest.json)的真实持仓代码, 把信号校正到实盘。
    Windows的stop_monitor会独立止损卖出, 信号并不知情 → 不校正就会漂移。
    快照缺失/早于上一交易日/为空 → 返回None(退回信号记忆, 不强行校正)。"""
    snap = Path("logs/qmt_positions_latest.json")
    if not snap.exists():
        return None
    try:
        d = json.loads(snap.read_text(encoding="utf-8"))
        exported = (d.get("exported_at") or "")[:10]
        today_str = date.today().strftime("%Y-%m-%d")
        past = [x for x in calendar if x < today_str]
        prev_td = past[-1] if past else today_str
        if exported and exported < prev_td:
            logger.warning(f"[持仓校正] QMT快照({exported})早于上一交易日({prev_td}), 不校正")
            return None
        pos = d.get("positions", {})
        codes = sorted({str(c).split(".")[0] for c, v in pos.items()
                        if isinstance(v, dict) and v.get("volume", 0) > 0})
        return codes if codes else None
    except Exception as e:
        logger.warning(f"[持仓校正] 读QMT快照失败: {e}")
        return None


def run():
    today    = date.today().strftime("%Y-%m-%d")
    calendar = _get_trade_calendar()
    if not is_trade_day(today, calendar):
        logger.info(f"{today} 非交易日，跳过"); return

    prev_signal      = _load_prev_signal()
    current_holdings = prev_signal.get("holdings", [])
    days_below_ma10  = {str(k): int(v) for k, v in prev_signal.get("days_below_ma10", {}).items()}

    # 把信号记忆校正到实盘真相: stop_monitor(Windows)独立止损卖出, 信号并不知情。
    # 不校正 → holdings漂移(信号以为30只,实盘13只) → 调仓增量算错 + reconcile天天假报差异。
    actual = _load_actual_qmt_holdings(calendar)
    if actual is not None and set(actual) != set(current_holdings):
        gone = sorted(set(current_holdings) - set(actual))
        logger.warning(f"[持仓漂移] 信号{len(current_holdings)}只 vs 实盘{len(actual)}只 → 以实盘为准; 实盘已无: {gone}")
        current_holdings = actual
        days_below_ma10  = {k: v for k, v in days_below_ma10.items() if k in set(actual)}

    # ── 每日: MA10出清检查(含自动补买) ──
    ma10_exits, days_below_ma10 = _check_ma10_exits(current_holdings, today, days_below_ma10)
    if ma10_exits:
        logger.warning(f"[MA10出清] {today}: {ma10_exits}")
        current_holdings = [c for c in current_holdings if c not in set(ma10_exits)]
        days_below_ma10  = {k: v for k, v in days_below_ma10.items() if k not in set(ma10_exits)}

        # ── 自动补买: 从成交额TOP80候补中选 ──
        replacements = []
        try:
            start = (date.today() - timedelta(days=350)).strftime("%Y-%m-%d")
            csi800 = load_meta("csi800"); codes = sorted(csi800["code"].tolist())
            rpanel, ramt = load_panels(codes, start, today)
            if not ramt.empty:
                rprices = rpanel.ffill().iloc[-1]
                rbudget = TRACK_A_CAPITAL * _get_position_ratio(today) / N_HOLDINGS
                # 候选 = 持仓规模的1.5倍(60→90), 跳过已持有
                candidates = _select_top_turnover(ramt, rprices, rbudget * N_HOLDINGS, int(N_HOLDINGS * 1.5))
                for c in candidates:
                    if c not in current_holdings and c not in replacements:
                        replacements.append(c)
                    if len(replacements) >= len(ma10_exits):
                        break
                if replacements:
                    current_holdings += replacements
                    logger.info(f"[MA10补买] {today}: 补入 {replacements}")
        except Exception as e:
            logger.warning(f"[MA10补买失败] {e}")

        partial = dict(prev_signal); partial.update({
            "signal_date": today, "ma10_exits": ma10_exits,
            "holdings": current_holdings, "sell": ma10_exits,
            "buy": replacements, "days_below_ma10": days_below_ma10,
            "note": f"MA10出清: {ma10_exits}, 补买: {replacements}" if replacements else f"MA10出清: {ma10_exits}, 无候补可买",
        })
        SIGNAL_FILE.write_text(json.dumps(partial, ensure_ascii=False, indent=2), encoding="utf-8")
        send_alert(f"【Track A v2 MA10出清】{today}\n⚠️ 技术走弱出清 {ma10_exits}\n持仓剩余 {len(current_holdings)}只")

    force = os.getenv("FORCE_REBAL", "0") == "1"
    if not is_rebalance_day(today, calendar) and not force:
        # 即使非调仓日, 也必须刷新信号文件日期, 防止QMT用过期信号对账
        if not ma10_exits:
            # 更新日期+重新保存, 持仓池不变
            if SIGNAL_FILE.exists():
                sig = json.loads(SIGNAL_FILE.read_text(encoding="utf-8"))
                sig["date"] = today
                sig["signal_date"] = today
                # 非调仓日无新增买卖: 清空buy/sell, 否则执行器(按signal_date==today判新鲜)
                # 会把上次调仓的buy/sell当"今天的单"每天重复下 → 靠reconcile(目标对账)兜底补漏
                sig["buy"] = []
                sig["sell"] = []
                # 持仓校正到实盘真相(止损可能已卖掉一些), 否则reconcile天天报假差异
                sig["holdings"] = current_holdings
                sig["days_below_ma10"] = days_below_ma10
                if isinstance(sig.get("shares"), dict):
                    sig["shares"] = {c: s for c, s in sig["shares"].items() if c in set(current_holdings)}
                SIGNAL_FILE.write_text(json.dumps(sig, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.info(f"{today} 非调仓日, MA10正常, 持仓{len(current_holdings)}只, 已刷新信号日期")
            return
        # MA10 exits detected: fall through to save signal with updated holdings
        logger.info(f"{today} 非调仓日, 但MA10出清{len(ma10_exits)}只→需保存信号")

    # ── 调仓日: 成交额TOP30 + MA200择时 ──
    logger.info(f"\n{'='*60}\n[Track A v2] 调仓日: {today}\n{'='*60}")

    pos_ratio = _get_position_ratio(today)
    # 仓位覆盖(环境变量, 用于人工保守/激进调整, 如 POSITION_RATIO_OVERRIDE=0.6)
    _override = os.getenv("POSITION_RATIO_OVERRIDE", "")
    if _override:
        pos_ratio = float(_override)
        logger.warning(f"[人工覆盖] 仓位档覆盖为 {pos_ratio:.0%} (环境变量 POSITION_RATIO_OVERRIDE)")
    logger.info(f"[大势] CSI800/MA200 → 仓位: {pos_ratio:.0%}")

    if pos_ratio <= 0.30:
        signal = {"signal_date": today, "strategy": "A-v2", "regime": "bear",
                  "position_ratio": pos_ratio, "cash_action": "money_market",
                  "holdings": [], "buy": [], "sell": current_holdings,
                  "weights": {}, "shares": {}, "days_below_ma10": {}}
        _save_and_alert(signal, pos_ratio); return

    # 加载数据（需要>200个交易日以满足load_panels的最低要求，约需350个日历日）
    start = (date.today() - timedelta(days=350)).strftime("%Y-%m-%d")
    csi800 = load_meta("csi800"); codes = sorted(csi800["code"].tolist())
    panel, amt = load_panels(codes, start, today)
    if panel.empty: send_alert("[Track A v2] 价格数据加载失败", level="error"); return

    # 成交额TOP30选股(跳过买不起的科创板高价股)
    latest_prices = panel.ffill().iloc[-1]
    effective_cap = TRACK_A_CAPITAL * pos_ratio
    selected = _select_top_turnover(amt, latest_prices, effective_cap, N_HOLDINGS)

    new_set  = set(selected); prev_set = set(current_holdings)
    buy_list  = [c for c in selected if c not in prev_set]
    sell_list = [c for c in current_holdings if c not in new_set]

    # ── 调仓换股控制 ─────────────────────────────────
    MAX_TURNOVER = 0.50  # 单次最多替换50%持仓(15只)

    turnover = (len(buy_list) + len(sell_list)) / (2 * N_HOLDINGS)
    if turnover > MAX_TURNOVER:
        logger.warning(f"换手率{turnover:.0%}超过上限{MAX_TURNOVER:.0%}, 分批执行")
        max_replace = int(N_HOLDINGS * MAX_TURNOVER)

        # 优先保留在TOP30中的旧持仓(它们排名更高)
        old_in_topn = [c for c in current_holdings if c in new_set]
        # 需要替换的旧持仓：不在TOP30中的
        old_to_replace = [c for c in current_holdings if c not in new_set]

        # 只替换最差的max_replace只
        sell_list = old_to_replace[:max_replace]
        # 从selected中取前max_replace只不在旧持仓中的
        keep_old = set(current_holdings) - set(sell_list)
        buy_list = [c for c in selected if c not in keep_old][:max_replace]
        # 最终持仓: 保留的旧持仓 + 新买入
        selected = list(keep_old) + buy_list
        selected = selected[:N_HOLDINGS]

        logger.info(f"分批调仓: 卖{len(sell_list)}只 + 买{len(buy_list)}只 = 换{len(buy_list)}只")

    # 单票权重检查(等权=3.3%, 在5%风控线内, 无超额)
    max_single_wt = 1.0 / N_HOLDINGS
    if max_single_wt > 0.05:
        logger.warning(f"单票权重{max_single_wt:.1%}超5%风控线, 需增加持仓数或降低仓位")

    # 等权配置
    weights = {c: 1.0/N_HOLDINGS for c in selected}
    shares = _calc_shares(selected, latest_prices, effective_cap)

    zero_shares = [c for c, s in shares.items() if s == 0]
    if zero_shares: logger.warning(f"资金不足一手: {zero_shares}")

    new_days_below = {c: days_below_ma10.get(c, 0) for c in selected}

    signal = {
        "signal_date":     today, "strategy": "A-v2",
        "regime":          "bull" if pos_ratio >= 0.70 else "neutral",
        "position_ratio":  pos_ratio,
        "cash_ratio":      round(1.0 - pos_ratio, 2),
        "cash_action":     "equity",
        "holdings":        selected,
        "buy":             buy_list, "sell": sell_list,
        "weights":         weights,
        "shares":          shares,
        "prices":          {c: round(float(latest_prices.get(c, 0)), 2) for c in selected},
        "effective_capital": round(effective_cap),
        "days_below_ma10": new_days_below,
        "note":            f"v2简化: 成交额TOP{N_HOLDINGS}+等权+MA200择时 | 14:57前竞价收盘委托",
    }
    # ── 多维验证(量比/内外盘/两融/过热) ──
    info_df = load_meta("stock_info_full") if 'load_meta' in dir() else load_meta("stock_info_full")
    signal["multi_dim_check"] = _multi_dimension_validate(
        selected, buy_list, panel, amt)

    _save_and_alert(signal, pos_ratio)

# ══════════════════════════════════════════════════════════════════
# 多维验证: 量比/内外盘/两融/过热 → 每只信号股标注是否适合建仓
# ══════════════════════════════════════════════════════════════════
def _multi_dimension_validate(holdings, buy_list, panel, amt_panel):
    import json, re, requests
    from pathlib import Path

    results = []
    for code in holdings:
        item = {"code": code, "flags": [], "risks": [], "score": 0}

        # ① 过热检查
        if code in panel.columns:
            cl = panel[code].dropna()
            if len(cl) >= 20:
                cur = cl.iloc[-1]; high20 = cl.iloc[-20:].max()
                ret5 = (cur/cl.iloc[-6]-1)*100 if len(cl)>=6 else 0
                ret20 = (cur/cl.iloc[-21]-1)*100 if len(cl)>=21 else 0
                dh = (cur/high20-1)*100
                cons_up = sum(1 for i in range(len(cl)-1,max(0,len(cl)-15),-1) if cl.iloc[i]>cl.iloc[i-1])
                if ret20 > 50: item["risks"].append(f"20日+{ret20:.0f}%暴涨"); item["score"] -= 6
                elif ret20 > 30: item["risks"].append(f"20日+{ret20:.0f}%偏热"); item["score"] -= 3
                if ret5 > 15: item["risks"].append(f"5日+{ret5:.0f}%"); item["score"] -= 2
                if cons_up >= 5: item["risks"].append(f"连涨{cons_up}天"); item["score"] -= 2
                if dh > -1 and cons_up >= 2: item["risks"].append("追高"); item["score"] -= 1
                item["ret5"] = round(ret5,1); item["ret20"] = round(ret20,1)
                item["cons_up"] = cons_up; item["dist_high"] = round(dh,1)

        # ② 量比(本地成交额)
        try:
            adf = load_daily(code,
                (pd.Timestamp.today()-pd.Timedelta(days=60)).strftime("%Y-%m-%d"),
                pd.Timestamp.today().strftime("%Y-%m-%d"))
            if not adf.empty and 'amount' in adf.columns:
                adf['date'] = pd.to_datetime(adf['date']); adf = adf.set_index('date').sort_index()
                amts = pd.to_numeric(adf['amount'], errors='coerce').dropna()
                if len(amts) >= 21:
                    vr = amts.iloc[-1] / amts.iloc[-21:-1].mean()
                    item["vol_ratio"] = round(vr,2)
                    if vr > 3.0: item["risks"].append(f"量比{vr:.1f}x过高")
                    elif vr > 1.5: item["flags"].append(f"放量{vr:.1f}x")
                    elif vr < 0.5: item["flags"].append(f"缩量{vr:.2f}x")
        except: pass

        # ③ 盘口主力方向(新浪实时买卖盘比)
        exchange = 'sh' if code.startswith('6') else 'sz'
        try:
            resp = requests.get(f'http://hq.sinajs.cn/list={exchange}{code}',
                headers={'Referer':'https://finance.sina.com.cn'}, timeout=5)
            m = re.search(r'"([^"]+)"', resp.text)
            if m:
                f = m.group(1).split(',')
                b1v = int(f[12]) if len(f)>12 and f[12] and f[12].isdigit() else 0
                s1v = int(f[22]) if len(f)>22 and f[22] and f[22].isdigit() else 0
                if b1v>0 and s1v>0:
                    r = b1v/s1v
                    if r>3: item["flags"].append(f"买方强({b1v//100}万手)"); item["score"] += 1
                    elif r<0.3: item["risks"].append(f"卖方强({s1v//100}万手)"); item["score"] -= 1
        except: pass

        # ④ 两融(从QMT缓存)
        try:
            qmt_files = sorted(Path('logs').glob('qmt_positions_*.json'))
            if qmt_files:
                qmt = json.loads(qmt_files[-1].read_text(encoding='utf-8'))
                for pos in qmt.get('positions',[]):
                    if pos.get('code')==code:
                        mb=pos.get('margin_buy',0); ms=pos.get('margin_sell',0)
                        if mb>ms*3: item["flags"].append("融资净买(杠杆看多)"); item["score"]+=1
                        elif ms>mb*3: item["risks"].append("融券堆积(空头)"); item["score"]-=1
                        break
        except: pass

        # ⑤ 综合判定
        in_buy = code in buy_list
        if item["score"] >= 2 and not item["risks"]: item["verdict"] = "🟢 适合建仓"
        elif item["score"] >= 0: item["verdict"] = "🟡 可考虑" if in_buy else "🟡 观望"
        elif item["score"] >= -3: item["verdict"] = "🟠 谨慎" if in_buy else "🟠 不适合"
        else: item["verdict"] = "🔴 过热回避"

        results.append(item)

    return results


def _save_and_alert(signal, pos_ratio):
    signal["generated_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    SIGNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    SIGNAL_FILE.write_text(json.dumps(signal, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"信号已保存 → {SIGNAL_FILE}")

    today, holdings = signal["signal_date"], signal.get("holdings", [])
    buy, sell = signal.get("buy", []), signal.get("sell", [])
    if signal["regime"] == "bear":
        msg = f"【Track A v2】{today}\n⚠️ 熊市清仓 → 货基\n卖出 {sell}"
    else:
        stock_info = load_meta("stock_info_full")
        ind_str = "—"
        if not stock_info.empty and "industry_l1" in stock_info.columns:
            ind_map = stock_info.set_index("code")["industry_l1"].to_dict()
            ind_cnt = {}
            for c in holdings:
                ind = ind_map.get(c, "其他"); ind_cnt[ind] = ind_cnt.get(ind, 0) + 1
            ind_str = "  ".join(f"{k}({v})" for k, v in sorted(ind_cnt.items(), key=lambda x: x[1], reverse=True)[:3])
        # Build execution checklist
        exec_lines = ""
        if sell or buy:
            exec_lines = "\n\n📋 执行清单:\n"
            if sell:
                exec_lines += f"  ① 卖出 {len(sell)}只: {', '.join(sell)}\n"
                exec_lines += f"     时间: 14:50 挂卖出委托(竞价/限价)\n"
            if buy:
                exec_lines += f"  ② 补买 {len(buy)}只: {', '.join(buy)}\n"
                exec_lines += f"     时间: 卖出确认后立即挂买入, 等权分配\n"
            exec_lines += f"  ③ 确认: 持仓数应回到 {len(holdings)-len(sell)+len(buy)}只\n"
            exec_lines += f"  ④ 若卖出未成交 → 次日开盘继续卖; 买入也顺延"

        msg = (
            f"【Track A v2 信号】{today}\n"
            f"📊 仓位: {pos_ratio:.0%} | 持仓: {len(holdings)}只 | 行业: {ind_str}\n"
            f"🔴 卖出({len(sell)}): {', '.join(sell[:4])}{'...' if len(sell)>4 else ''}\n"
            f"🟢 买入({len(buy)}): {', '.join(buy[:4])}{'...' if len(buy)>4 else ''}\n"
            f"💰 投入: {signal.get('effective_capital',0):,}元 | 等权 1/{N_HOLDINGS}\n"
            f"⏰ 14:57前提交竞价收盘委托"
            f"{exec_lines}"
        )
    logger.info(msg); send_alert(msg)

if __name__ == "__main__":
    run()
