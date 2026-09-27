"""
CB 引擎信号生成（2026-09-01，股票×CB 配比 40/60 部署用）。

口径与回测 backtest_cb_doublelow.py 一致：双低(价格+溢价率×100)最低25只，
等权月调仓，资金 60 万。信号输出 data_store/meta/signal_cb_latest.json，
Windows 端 fetch_and_execute --track cb 拉取执行（执行器已参数化 track）。

月调仓日：每月 15 日 + 月末倒数第二（与股票侧一致的双周节奏，但 CB 只在这些日
全量刷新；非调仓日仅提示强赎/退市）。
用法: python scripts/cb_signal.py
"""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
from loguru import logger

logger.add("logs/cb_signal_{time:YYYY-MM-DD}.log", rotation="1 day", retention="365 days")

from data.storage import load_meta
from data.cb_select import select_double_low
from monitoring.alerts import send_alert

CB_CAPITAL = 600_000
TOP_N = 25
# 2026-09-22 停牌过滤(甲乙丙定案=甲, N=10 与回测 V3 一致, 判据出数前锁):
# 候选最新日线 bar 距今 >N 个交易日 → 排除, 从全排名向下补足 25 只。
TRADABLE_MAX_GAP_DAYS = 10
# 2026-09-24 到期过滤(立项批准, 网格 N=60 暂定, 判据用户锁死):
# 距最后交易日(无则到期日−10天) < N 天 → 剔除(期权时间价值归零, 牛市掉队)。
# 数据=cb_expiry_watch.parquet(每日 09:05 cron 刷新, 含 jsl 公告日期)。
MATURITY_EXCLUDE_DAYS = 60
# 2026-09-24 用户裁定: 禁买阈值 M=120——新建仓时到期前 120 天内不买入
# (新建仓位要能持有至少一个完整调仓周期+N=60 剔除线≈两个月有效持有期)。
# N~M 之间=缓冲带: 可持有但不加仓(目标=当前持仓)。M 值后续网格验证, 暂定。
MATURITY_NO_BUY_DAYS = 120


def maturity_band(days_left: float, is_held: bool) -> str:
    """到期三档分类(纯函数, 可测): days_left=距交易末日天数。
    "exclude"=剔除卖出(已持有<60天) / "hold_no_buy"=可持有不加仓(60~120) /
    "ok"=正常。未持有的券在 60~120 带=禁买 → "exclude"。"""
    if days_left < MATURITY_EXCLUDE_DAYS:
        return "exclude"
    if days_left < MATURITY_NO_BUY_DAYS:
        return "hold_no_buy" if is_held else "exclude"
    return "ok"
SIGNAL_FILE = Path("data_store/meta/signal_cb_latest.json")
CB_DIR = Path("data_store/convertible_bonds")


def _load_prev():
    if not SIGNAL_FILE.exists():
        return {}
    try:
        return json.loads(SIGNAL_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _load_actual_cb():
    """读QMT快照的转债持仓(11/12/127前缀), 判断CB建仓是否已实盘成交。"""
    import json as _j
    snap = Path("logs/qmt_positions_latest.json")
    if not snap.exists():
        return None
    try:
        d = _j.loads(snap.read_text(encoding="utf-8"))
        pos = d.get("positions", {})
        cb = {str(c).split(".")[0] for c, v in pos.items()
              if isinstance(v, dict) and v.get("volume", 0) > 0 and str(c)[:3] in ("110", "111", "113", "118", "123", "127", "128")}
        return cb if cb else None
    except Exception:
        return None


def run():
    from datetime import date
    today = str(date.today())
    # 2026-09-27: 非交易日跳过(与股票链同款)——09-25 中秋实证: 不跳过会生成
    # 幽灵信号(日期领先交易日历)+健康行假"CB信号停滞"告警; 国庆长假前必修
    from daily_signal_a_v2 import _get_trade_calendar
    _cal = _get_trade_calendar()
    if _cal and today not in _cal:
        logger.info(f"{today} 非交易日，跳过")
        return
    snaps = load_meta("cb_snapshots")
    if snaps.empty:
        logger.error("cb_snapshots 为空，请先跑 build_cb_data.py")
        return
    snaps["snap_date"] = pd.to_datetime(snaps["snap_date"])
    latest = snaps["snap_date"].max()
    if (pd.Timestamp(today) - latest).days > 35:
        send_alert(f"CB信号: 快照过期(最新{latest.date()})，跳过生成", level="warning")
        return
    snap = snaps[snaps["snap_date"] == latest]
    ranked = select_double_low(snap)
    if not ranked:
        logger.error("选股为空")
        return

    # ── 2026-09-22 停牌过滤(甲方案, N=10, 与回测 V3 同口径) ──
    # 两道: ①新浪实时 prev>0 且现价=0 → 现时停牌, 立即排除(实时 belt,
    # 回测无此项, 覆盖"N日内新停牌"盲区; 新浪无数据不算停牌) ②最新日线
    # bar 距今 >10 个交易日 → 排除(可进回测的时点新鲜度规则)。排除是事件:
    # 记入 signal["excluded"] + 邮件, 不静默(闸门十二)。
    import time as _t
    import urllib.request
    import akshare as _ak
    from daily_signal_a_v2 import _get_trade_calendar
    cal = _get_trade_calendar()
    today_ts = pd.Timestamp(today)
    cal_ts = sorted(pd.to_datetime(cal))

    def _trading_days_between(start_ts):
        return len([d for d in cal_ts if start_ts < d <= today_ts])

    def _sina_suspended(batch_codes):
        """停牌判据=昨收>0 且成交量=0。注: 沪深停牌现价约定不同(沪=冻结在
        昨收, 深=0.000), 但成交量=0 两所一致, 且信号时点(14:28盘中)正常
        交易券成交量必>0。新浪无覆盖(空响应)不算停牌。"""
        out = set()
        try:
            syms = ",".join(
                ("sh" if str(c).startswith("11") else "sz") + str(c)
                for c in batch_codes)
            req = urllib.request.Request(
                "https://hq.sinajs.cn/list=" + syms,
                headers={"Referer": "https://finance.sina.com.cn"})
            txt = urllib.request.urlopen(req, timeout=15).read().decode("gbk", "ignore")
            for line in txt.splitlines():
                if "=" not in line:
                    continue
                head, body = line.split("=", 1)
                code = head.rsplit("_", 1)[-1][2:]
                fields = body.strip().strip('";').split(",")
                # 字段: 0名称 1今开 2昨收 3现价 4最高 5最低 6买一 7卖一 8成交量
                if len(fields) >= 9:
                    try:
                        prev = float(fields[2])
                        vol = float(fields[8])
                    except ValueError:
                        continue
                    if prev > 0 and vol == 0:
                        out.add(code)
        except Exception:
            pass
        return out

    suspended_now = _sina_suspended(ranked[:80])
    excluded = {}
    snap_price = {}
    codes = []
    _tradable_cache = {}   # code -> (ok, reason|None, close|None), 供非调仓日路径复用
    _buffer_codes = set()  # 60~120天缓冲带: 可持有但不加仓
    # 提前加载上一信号(缓冲带判定需要"是否已持有")
    prev = _load_prev()
    prev_hold = set(prev.get("holdings", []))

    # 到期过滤表: {code: 交易末日}(最后交易日, 无则到期日−10天)
    expiry_watch = {}
    try:
        _ew = pd.read_parquet(Path("data_store/meta/cb_expiry_watch.parquet"))
        for _, r in _ew.iterrows():
            c = str(r["代码"])
            last = pd.Timestamp(r["最后交易日"]) if pd.notna(r["最后交易日"]) else None
            exp = pd.Timestamp(r["到期日"]) if pd.notna(r["到期日"]) else None
            if last is not None:
                expiry_watch[c] = last
            elif exp is not None:
                expiry_watch[c] = exp - pd.Timedelta(days=10)
    except Exception:
        pass

    def _check_tradable(c):
        """单券可交易检查: 停牌/临期/日线停更/无数据/价格无效 → (False, 原因)。
        结果 memo 到 _tradable_cache; akshare 优先, 失败回退本地 parquet。"""
        if c in suspended_now:
            return False, "停牌(新浪实时价=0)", None
        if c in expiry_watch:
            days_left = (expiry_watch[c] - today_ts).days
            band = maturity_band(days_left, c in prev_hold)
            if band == "exclude":
                return False, f"临期(交易末日{expiry_watch[c].date()}, 剩{days_left}天)", None
            if band == "hold_no_buy":
                _buffer_codes.add(c)   # 可持有不加仓, 目标钳到当前持仓
        last_date = None
        p = None
        try:
            sym = ("sh" if str(c).startswith(("11",)) else "sz") + str(c)
            d = _ak.bond_zh_hs_cov_daily(symbol=sym)
            if d is not None and len(d) > 0:
                d = d.sort_values("date")
                last_date = pd.Timestamp(d.iloc[-1]["date"])
                p = float(d.iloc[-1]["close"])
            _t.sleep(0.8)
        except Exception:
            pass
        if last_date is None:
            local = CB_DIR / "daily" / f"{c}.parquet"
            if local.exists():
                try:
                    ld = pd.read_parquet(local)
                    dc = "date" if "date" in ld.columns else ld.columns[0]
                    ld = ld.sort_values(dc)
                    last_date = pd.Timestamp(ld.iloc[-1][dc])
                    p = float(ld.iloc[-1]["close"])
                except Exception:
                    pass
        if last_date is None:
            return False, "无日线数据", None
        gap = _trading_days_between(last_date)
        if gap > TRADABLE_MAX_GAP_DAYS:
            return False, f"日线停更{last_date.date()}({gap}个交易日)", None
        if not p or p <= 0:
            return False, "价格无效", None
        return True, None, p

    for c in ranked[:80]:
        if len(codes) >= TOP_N:
            break
        if c not in _tradable_cache:
            _tradable_cache[c] = _check_tradable(c)
        ok, reason, p = _tradable_cache[c]
        if not ok:
            excluded[c] = reason
            continue
        snap_price[c] = p
        codes.append(c)
    if len(codes) < TOP_N:
        send_alert(f"CB信号: 可交易池不足25只(实际{len(codes)}, 排除{len(excluded)}只), 以实际数量出信号", level="warning")
    if not codes:
        logger.error("全部候选被排除, 信号中止")
        return

    # 等权股数(转债一手=10张); 缓冲带券(60~120天)目标钳到上一信号张数=不加仓
    budget = CB_CAPITAL / len(codes)
    shares, prices = {}, {}
    buffer_targets = {}
    for c in _buffer_codes:
        ps = prev.get("shares", {}).get(c, 0)
        if ps and ps > 0:
            buffer_targets[c] = ps
    for c in codes:
        p = snap_price.get(c)
        if not p or p <= 0:
            continue
        if c in buffer_targets:
            shares[c] = buffer_targets[c]
            prices[c] = round(p, 2)
            continue
        lots = int(budget / p / 10)
        shares[c] = lots * 10
        prices[c] = round(p, 2)

    new_hold = set(shares.keys())
    actual_cb = _load_actual_cb()
    # 月度调仓日才全量轮换；非调仓日保持持仓(仅输出状态)
    from daily_signal_a_v2 import _get_trade_calendar, is_rebalance_day
    cal = _get_trade_calendar()
    interim = False  # 2026-09-24: 过渡期标记(非调仓日沿用旧持仓=部分部署)
    if is_rebalance_day(today, cal):
        buy = sorted(new_hold - prev_hold)
        sell = sorted(prev_hold - new_hold)
    elif not prev or not actual_cb:
        # 首次运行 或 实盘CB尚未建仓: 保持全量买入信号直到成交(自愈, 防错过建仓窗口)
        buy = sorted(new_hold)
        sell = []
        if not prev:
            logger.warning("[CB] 首次信号: 全量建仓信号保持到实盘确认")
        else:
            logger.warning(f"[CB] 实盘CB持仓缺失(快照无转债), 建仓信号保持: {len(buy)}只")
    else:
        # 非调仓日: 沿用上一信号持仓——但2026-09-22起先过停牌过滤:
        # 旧持仓里的停牌/停更券必须剔除(否则交易端每天对它们重试必废单),
        # 不补足(补足是月度调仓日的甲方案动作)。价格优先用新鲜拉取值,
        # 拿不到才回退上一信号价(冻结价×1.05 不再作为唯一来源)。
        buy, sell = [], []
        if prev_hold:
            keep = []
            for c in sorted(prev_hold):
                if c not in _tradable_cache:
                    _tradable_cache[c] = _check_tradable(c)
                ok, reason, p = _tradable_cache[c]
                if not ok:
                    excluded[c] = reason
                    continue
                keep.append(c)
                if p and p > 0:
                    snap_price[c] = p
            codes = keep
            shares = {c: prev.get("shares", {}).get(c, 0) for c in codes}
            prices = {c: snap_price.get(c) or prev.get("prices", {}).get(c, 0)
                      for c in codes}
            # 2026-09-24: 过渡期部分部署(停牌/临期剔除后张数未重配)——capital
            # 保持 60万(风控单券上限的分母=轨道全量资金, 不可缩小), 另置
            # interim_deployment 标记供预检豁免低配拦截(预检按实际比对)。
            interim = True

    signal = {
        "signal_date": today,
        "track": "cb",
        "capital": CB_CAPITAL,
        "interim_deployment": interim,
        "holdings": codes,
        "buy": buy,
        "sell": sell,
        "shares": shares,
        "prices": prices,
        "position_ratio": 1.0,
        "excluded": excluded,
        "note": f"CB双低25只月调仓 | 资金60万 | 最新快照{latest.date()}",
    }
    SIGNAL_FILE.write_text(json.dumps(signal, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"CB信号: {today} 持仓{len(codes)}只 买入{len(buy)} 卖出{len(sell)} 排除{len(excluded)}只")
    send_alert(f"[CB] 信号更新: 持仓{len(codes)}只, 买入{len(buy)}, 卖出{len(sell)}, "
               f"排除{len(excluded)}只({latest.date()}快照)")


if __name__ == "__main__":
    run()
