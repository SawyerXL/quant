# -*- coding: utf-8 -*-
"""
主策略 pTrade 移植版 v1（国金证券托管运行）

移植对象（逐条对照，不是新策略）:
  - 选股/择时/退出逻辑: scripts/daily_signal_a_v2.py
  - 唯一权威口径:       docs/qmt_strategy_spec.md v2.3 (2026-08-30)

移植原则（回测复验纪律的代码版）:
  上线前必须在 pTrade 仿真盘与 Linux 信号逐日对账（目标组合/仓位档/换手清单），
  对不上 = 移植失败，禁止直接实盘。见 docs/ptrade_migration_plan.md 对账规程。

验证路线（国金仿真/回测暂未开放, 2026-09-01）:
  ① DRY_RUN=True 观察模式: 只算不下单, 逐日对账 vs Linux 信号(零风险)
  ② 对账 PASS 后: DRY_RUN=False + CAPITAL_CAP=20000 小资金实盘, 再对账 ≥2 调仓周期
  ③ 全部通过 → 三批替换(见方案 doc Phase 2)

与 Linux 版的结构性差异（数据源差异，非逻辑差异，均已备案）:
  1. 数据源: pTrade get_history/get_snapshot 替代本地 parquet + 新浪
     - K线 fq='dypre'(动态前复权) 对齐本地前复权口径
     - 排名/波动率/MA10 用 include=False(T-1 完整K线)；Linux 版 14:25 跑可能含当日盘中bar
  2. 持仓真相: 查询自身 portfolio.positions，无 Linux 版"信号↔实盘漂移校正"问题
  3. multi_dim_check 裁剪: 无外网 → 保留过热/量比(K线可算)，盘口用 snapshot 盘口比，
     两融缓存无替代 → 砍掉
  4. 买得起过滤/股数计算用 snapshot last_px(真实可成交价)，Linux 版用前复权收盘价
     （边缘票可能差 1~2 只，对账时允许的已知差异）
  5. 策略资金 = 账户净资产 − 忽略仓市值（Linux 版固定 40 万基数）

风控红线（execution/risk.py 同款，策略内实现）:
  - 单笔订单 ≤ 10 万 | 涨停禁买/跌停禁卖 | 单票 ≤ 5%（等权 1/60≈1.7% 天然满足）
  - 行业集中度告警已关闭（行业映射因文件体积上限移除，2026-09-03；恢复方式见 _industry_check）
  - 回撤 25% → B方案：停新开仓+告警，持仓按 MA10 自然退出，恢复需人工确认

仿真上线前验证清单（pTrade 环境实测 + 官方API文档核对，2026-09-02）:
  [x] get_trade_days 可用（v1 实测）
  [x] order/order_target_value/get_orders/cancel_order 存在（v1 实测）
  [x] 持久化: 平台原生 pickle 持久化 g（每次事件后自动保存+重启自动恢复），无需 write_file
  [x] 告警: 仅平台日志 + set_email_info（策略报错终止时发邮件，可选，QQ邮箱）
  [x] run_daily 签名为 (context, func, time)（文档确认，已修正）
  [x] python3.11 下 get_history 多标的返回长表['code', field]（已按 pivot 适配）
  [x] 000906.SS 指数K线可用(210根, 最新5166.93 与本地parquet逐位一致) — v4 实测 2026-09-02
  [x] get_index_stocks 成分股 800只可用; dypre 可用; get_snapshot 涨跌停字段可用 — v4 实测
  [x] 多标的 get_history 长表['code',field] 已确认并适配 pivot
"""

import json
import pandas as pd

# ══════════════════════════════════════════════════════════════════
# 策略参数（与 Linux 版逐条一致；任何改动必须 A/B 回测 + 用户确认）
# ══════════════════════════════════════════════════════════════════
N_HOLDINGS      = 60
MAX_VOL20       = 5.0        # 拥挤度过滤：20日波动率>5%剔除（严格口径，只用T-1及以前）
MA10_EXIT_DAYS  = 4          # 权威口径 v2.3；⚠️ Linux 信号脚本仍是 3，实盘前必须对齐（决策点4）
MA200_PERIOD    = 200
MAX_TURNOVER    = 0.50       # 单次调仓最多替换 50% 持仓
MAX_SINGLE_ORDER_VALUE = 100_000   # 2026-09-02 对齐 settings.py(10万防错单红线)
MAX_ACCOUNT_DRAWDOWN   = 0.15   # 2026-09-02 约束引擎网格定案红线 15%(B方案); 回测日志实锤此前25%错配
MAX_INDUSTRY_SHARE     = 0.30
INDEX_CODE      = '000906.SS'     # 中证800：五档择时唯一输入。2026-09-02 探测实测: .SS 有K线, .XBHS 空数据; 成分股两种后缀均可
USE_INDEX_FALLBACK = False        # 仿真调试可开（降级 000300.XBHS 顶替）；实盘必须 False——换指数=参数改动
CHUNK_SIZE      = 400             # get_history 分批大小，仿真测性能后可调

# 策略不碰的代码。400286 老三板 pTrade 无法交易；
# 603392 万泰 / 588000 科创50ETF：处置待用户拍板（卖=人工卖出后从清单移除；留=永久保留在清单）
IGNORE_CODES = {'400286', '603392', '588000'}

# 策略资金上限(元)：None = 账户净资产−忽略仓市值 全部归策略；设值则封顶
CAPITAL_CAP = None

# 观察模式(国金仿真/回测暂未开放, 2026-09-01):
#   True  = 完整计算 + 日志SIGNAL + 逐日对账, 但不下任何单(零风险验证数据/逻辑)
#   False = 真实下单。切换条件: 对账连续≥4周 PASS 且 CAPITAL_CAP 已设为小资金起步值
DRY_RUN = True

# 熔断恢复开关: B方案要求"恢复需人工确认"。g 由平台持久化(重启后 halt 仍为 True),
# 人工确认 = 将本开关改 True 保存并重启策略(当日14:40生效), 确认后改回 False。
HALT_RESET = False

# 池子预算基数(元): 与 Linux 信号端 TRACK_A_CAPITAL 一致(40万)。
# 池子的"买得起过滤"永远用这个基数 → pTrade 与 Linux 选股同口径, 池子不随账户资金漂移;
# 账户资金(仿真20万/实盘账户)只影响实际下单股数。
POOL_CAPITAL_BASE = 400_000

# ══════════════════════════════════════════════════════════════════
# 基础工具
# ══════════════════════════════════════════════════════════════════

def _pt(code):
    """6位代码 → pTrade 格式（沪市/科创板 .SS，其余 .SZ；北交所不在池内）"""
    return code + ('.SS' if code.startswith(('60', '68')) else '.SZ')

def _plain(code):
    return str(code).split('.')[0]

def _chunk(codes, size=CHUNK_SIZE):
    return [codes[i:i + size] for i in range(0, len(codes), size)]

def _try_history(n, field, codes, fq=None, include=False):
    """get_history 包装：分批 + 全异常兜底（停牌/接口限制都不能炸掉整个策略）
    python3.11 下多标的返回长表（列=['code', field]），在此统一 pivot 成宽表（列为代码）。"""
    frames = []
    for chunk in _chunk(codes):
        try:
            df = get_history(n, '1d', field, [_pt(c) for c in chunk],
                             fq=fq, include=include)
            if df is not None and not df.empty:
                if 'code' in df.columns:
                    df = df.pivot(columns='code', values=field)
                frames.append(df)
        except Exception as e:
            log.warning(f'get_history({field}) 分块失败: {e}')
    return pd.concat(frames, axis=1) if frames else None

def _snapshot(codes):
    """get_snapshot 批量拉快照 → {6位代码: {last_px, up_px, down_px, ...}}
    回测环境不支持该接口: 首次空返回后置标志, 当日后续调用直接短路(省去平台告警刷屏)"""
    codes = list(codes)
    if not codes or getattr(g, 'snap_unavailable', False):
        return {}
    out = {}
    for chunk in _chunk(codes, 500):
        try:
            snap = get_snapshot([_pt(c) for c in chunk])
        except Exception as e:
            log.warning(f'get_snapshot 分块失败: {e}')
            continue
        for pt, v in (snap or {}).items():
            if isinstance(v, dict) and v.get('last_px'):
                out[_plain(pt)] = v
    if not out:
        g.snap_unavailable = True
    return out

def _build_fallback(codes):
    """快照不可用(回测)时, 用 dypre 收盘价+涨跌停价构造定价字典(卖出门控/限价需要)。
    熔断/熊市分支不加载池子数据, 持仓卖出必须走这个兜底, 否则回测里卖出全部失效。"""
    close_df = _try_history(5, 'close', codes, fq='dypre')
    if close_df is None:
        return {}
    hi_df = _try_history(5, 'high_limit', codes)
    lo_df = _try_history(5, 'low_limit', codes)
    hi = {_plain(c): float(hi_df[c].iloc[-1]) for c in hi_df.columns
          if pd.notna(hi_df[c].iloc[-1])} if hi_df is not None else {}
    lo = {_plain(c): float(lo_df[c].iloc[-1]) for c in lo_df.columns
          if pd.notna(lo_df[c].iloc[-1])} if lo_df is not None else {}
    out = {}
    for c in close_df.columns:
        if pd.notna(close_df[c].iloc[-1]):
            out[_plain(c)] = {'last_px': float(close_df[c].iloc[-1]),
                              'up_px': hi.get(_plain(c)),
                              'down_px': lo.get(_plain(c)),
                              'fallback': True}
    return out

def _notify(msg):
    """告警：优先券商短信/邮件通道，不可用则仅平台日志"""
    log.error(msg)
    try:
        send_message(msg)
    except Exception:
        pass

def _month_calendar(today):
    """当月交易日列表（get_trade_days），失败返回空 → 当日停摆。
    注意: 返回 numpy.ndarray 不能做真值判断(or 会抛 ambiguous)；月末日按日历计算(平台会校验日期)。"""
    import datetime
    y, m = int(today[:4]), int(today[5:7])
    nxt = datetime.date(y + 1, 1, 1) if m == 12 else datetime.date(y, m + 1, 1)
    last_day = (nxt - datetime.timedelta(days=1)).day
    try:
        arr = get_trade_days(f'{y:04d}-{m:02d}-01', f'{y:04d}-{m:02d}-{last_day:02d}')
        if arr is None:
            return []
        return [str(d)[:10] for d in arr]
    except Exception as e:
        log.error(f'get_trade_days 失败: {e}')
        return []

def _is_rebalance_day(today, cal):
    """双周调仓：月末倒数第二个交易日 + 月中第 len//2-1 个交易日（与 Linux 版一致）"""
    days = [d for d in cal if d.startswith(today[:7])]
    if not days:
        return False
    end_idx = -2 if len(days) >= 2 else -1
    if today == days[end_idx]:
        return True
    mid = max(0, len(days) // 2 - 1)
    return len(days) >= 2 and today == days[mid]

# ══════════════════════════════════════════════════════════════════
# 数据加载（与 Linux 版 load_panels / load_meta("csi800_index") 对应）
# ══════════════════════════════════════════════════════════════════

def _load_universe():
    """CSI800 成分，三层降级：000906成分 → 300+500成分（定义等价）→ 内置清单。
    后两层不改变策略口径（同一个池子），可自动降级；全部失败返回空 → 当日停摆告警。"""
    try:
        stocks = list(get_index_stocks(INDEX_CODE) or [])
        if stocks:
            return sorted({_plain(s) for s in stocks})
    except Exception:
        pass
    try:
        s300 = list(get_index_stocks('000300.XBHS') or [])
        s500 = list(get_index_stocks('000905.XBHS') or [])
        if s300 or s500:
            return sorted({_plain(s) for s in s300 + s500})
    except Exception:
        pass
    return CSI800_LIST

def _index_position_ratio():
    """CSI800 收盘/MA200 → 五档仓位。指数拿不到返回 None（按纪律停摆，不静默换指数）"""
    try:
        df = get_history(MA200_PERIOD + 10, '1d', 'close', INDEX_CODE,
                         fq=None, include=False)
        closes = None
        if df is not None and not df.empty:
            closes = pd.to_numeric(df.iloc[:, 0], errors='coerce').dropna()
        if closes is None or len(closes) < MA200_PERIOD:
            if USE_INDEX_FALLBACK:
                df2 = get_history(MA200_PERIOD + 10, '1d', 'close', '000300.XBHS',
                                  fq=None, include=False)
                closes = pd.to_numeric(df2.iloc[:, 0], errors='coerce').dropna()
            else:
                return None
        if len(closes) < MA200_PERIOD:
            return 0.85          # 数据不足时与 Linux 版同款保守值
        ratio = float(closes.iloc[-1] / closes.rolling(MA200_PERIOD).mean().iloc[-1])
        if ratio >= 1.05:   return 1.00
        if ratio >= 1.02:   return 0.85
        if ratio >= 0.98:   return 0.70
        if ratio >= 0.95:   return 0.50
        return 0.30
    except Exception as e:
        log.error(f'指数K线加载失败: {e}')
        return None

# ══════════════════════════════════════════════════════════════════
# 策略核心（逐条对照 daily_signal_a_v2.py）
# ══════════════════════════════════════════════════════════════════

def _select_top_turnover(amt_df, close_df, prices, capital, n):
    """20日均成交额 top(2n) → 拥挤度过滤 → 逐个检查买得起一手 → 取前 n。
    prices: 真实价(snapshot last_px)；close_df: dypre 收盘(只用于波动率)"""
    avg = amt_df.tail(20).mean()
    budget = capital / n
    rets = close_df.pct_change()
    vol20 = (rets.iloc[-21:-1].std() * 100)      # 严格口径：20个收益率止于T-1，不含当日
    picked = []
    for ptcode in avg.nlargest(n * 2).index:
        if len(picked) >= n:
            break
        code = _plain(ptcode)
        if code in IGNORE_CODES:
            continue
        v = vol20.get(ptcode)
        if pd.notna(v) and v > MAX_VOL20:
            continue
        p = prices.get(code)
        if p and p > 0:
            lot = 200 if code.startswith('688') else 100
            if p * lot <= budget:
                picked.append(code)
    return picked[:n]

def _calc_shares(holdings, prices, total_capital):
    """等权计算股数（整手取整）。买不起一手的剔除后按剩余只数重新摊分,
    消除"池子过滤(40万口径)与账户资金(20万)两套基数错配"造成的资金闲置(2026-09-03归因)"""
    shares = {}
    n = len(holdings) or 1
    for code in holdings:
        p = prices.get(code)
        if p and p > 0:
            lot = 200 if code.startswith('688') else 100
            shares[code] = int(total_capital / n / p / lot) * lot
        else:
            shares[code] = 0
    pos = [c for c in holdings if shares.get(c)]
    if pos and len(pos) < n:
        for code in pos:
            p = prices.get(code)
            lot = 200 if code.startswith('688') else 100
            shares[code] = int(total_capital / len(pos) / p / lot) * lot
    return shares

def _check_ma10(held_codes, g):
    """连续 MA10_EXIT_DAYS 日收盘<MA10 → 出清。停牌数据缺失按 Linux 版口径重置计数"""
    exits = []
    if not held_codes:
        return exits
    df = _try_history(15, 'close', held_codes, fq='dypre')
    for code in held_codes:
        pt = _pt(code)
        s = df[pt].dropna() if df is not None and pt in df.columns else None
        if s is None or len(s) < 10:
            g.days_below[code] = 0
            continue
        if float(s.iloc[-1]) < float(s.iloc[-10:].mean()):
            g.days_below[code] = g.days_below.get(code, 0) + 1
            if g.days_below[code] >= MA10_EXIT_DAYS:
                exits.append(code)
        else:
            g.days_below[code] = 0
    return exits

def _managed_positions(context):
    """账户持仓中策略管的代码（排除 IGNORE）→ {6位代码: Position}"""
    out = {}
    for pt, pos in (context.portfolio.positions or {}).items():
        code = _plain(pt)
        if getattr(pos, 'amount', 0) and code not in IGNORE_CODES:
            out[code] = pos
    return out

def _strategy_capital(context, snap):
    """策略资金 = 账户净资产 − 忽略仓市值；CAPITAL_CAP 可封顶（只约束下单规模）"""
    total = context.portfolio.portfolio_value
    ignored_val = 0.0
    for pt, pos in (context.portfolio.positions or {}).items():
        if _plain(pt) in IGNORE_CODES:
            amt = getattr(pos, 'amount', 0) or 0
            px = (snap.get(_plain(pt)) or {}).get('last_px') or getattr(pos, 'last_sale_price', 0) or 0
            ignored_val += amt * px
    cap = total - ignored_val
    return min(cap, CAPITAL_CAP) if CAPITAL_CAP else cap

def _account_nav(context, snap):
    """账户真实净值(策略口径): 净资产 − 忽略仓市值。不封顶——CAPITAL_CAP 只约束下单规模,
    净值计量必须用真实数字, 否则回撤熔断和绩效全失真(2026-09-03 回测实锤: 净值被卡在20万整)。"""
    total = context.portfolio.portfolio_value
    ignored_val = 0.0
    for pt, pos in (context.portfolio.positions or {}).items():
        if _plain(pt) in IGNORE_CODES:
            amt = getattr(pos, 'amount', 0) or 0
            px = (snap.get(_plain(pt)) or {}).get('last_px') or getattr(pos, 'last_sale_price', 0) or 0
            ignored_val += amt * px
    return total - ignored_val

def _industry_check(selected):
    """行业集中度告警已关闭(2026-09-03): 20KB 行业映射使文件超过客户端粘贴上限, 已移除。
    等权60只+CSI800结构下极少触达30%红线; 如需恢复, 用本地数据重新生成映射贴回。"""
    return None

# ══════════════════════════════════════════════════════════════════
# 下单（风控红线内嵌）
# ══════════════════════════════════════════════════════════════════

def _sell_all(code, amt, price):
    """显式按当前持仓量卖出。不用 order_target_value：交易场景持仓同步有6秒时滞，
    循环里连续调 target 类接口会重复下单（官方文档明确警告）。"""
    if not price or price <= 0 or amt <= 0:
        return False
    try:
        # 限价归一到 tick 整数倍(前复权兜底价可能带多位小数, 柜台会废单)
        order(_pt(code), -int(amt), limit_price=round(price, 2))
        return True
    except Exception as e:
        log.error(f'卖出失败 {code}: {e}')
        return False

def _sell_shares(code, shares, price):
    """按指定股数减仓(权重调整用)。全清仓走 _sell_all。"""
    if not price or price <= 0 or shares <= 0:
        return False
    try:
        order(_pt(code), -int(shares), limit_price=round(price, 2))
        return True
    except Exception as e:
        log.error(f'减仓失败 {code}: {e}')
        return False

def _buy(code, shares, price):
    if not price or price <= 0 or shares <= 0:
        return False
    if shares * price > MAX_SINGLE_ORDER_VALUE:
        log.error(f'{code} 单笔 {shares * price:,.0f} 超10万红线, 跳过')
        return False
    try:
        # 限价归一到 tick 整数倍(前复权兜底价可能带多位小数, 柜台会废单)
        order(_pt(code), int(shares), limit_price=round(price, 2))
        return True
    except Exception as e:
        log.error(f'买入失败 {code}: {e}')
        return False

def _cancel_unfilled(context):
    """撤掉未成交的旧委托，防止次日重复下单。Order 对象字段是 id，状态 str: 8=已成 9=废单。"""
    try:
        for o in (get_orders() or []):
            if str(getattr(o, 'status', '8')) not in ('8', '9'):
                try:
                    cancel_order(getattr(o, 'id', ''))
                except Exception:
                    pass
    except Exception:
        pass

# ══════════════════════════════════════════════════════════════════
# 生命周期
# ══════════════════════════════════════════════════════════════════

def initialize(context):
    g.days_below = {}
    g.pending_buys = {}        # code -> 计划股数（涨停禁买/废单 → 次日重试）
    g.pending_sells = []       # 跌停禁卖 → 次日重试
    g.nav_high = None
    g.halt = False             # 回撤25%熔断B方案：停新开仓，持仓按MA10自然退出
    _restore_state()
    # 回测成本口径(仅回测生效, 实盘按柜台实际费率): 2026-09-03 谈判结果=万1 但不免5(最低5元/笔)
    # 单笔3千多的组合下最低费起决定作用, 回测必须用真实口径才能反映真实经济性
    try:
        set_commission(commission_ratio=0.0001, min_commission=5, type="STOCK")
    except Exception:
        pass
    # 官方签名: run_daily(context, func, time)，第一个参数必须是 context（2026-09-02 文档修正）
    run_daily(context, retry_orders, '9:35')
    run_daily(context, daily_main, '14:40')
    run_daily(context, eod, '15:10')
    # 可选: 策略报错终止时邮件提醒(需QQ邮箱+SMTP授权码, 咨询券商是否支持)
    # set_email_info('你的QQ邮箱@qq.com', 'QQ邮箱SMTP授权码', 'PTrade主策略告警')
    log.info(f'[ptrade主策略v1] N={N_HOLDINGS} MA10={MA10_EXIT_DAYS}d vol20≤{MAX_VOL20}% '
             f'指数={INDEX_CODE} 忽略仓={sorted(IGNORE_CODES)} DRY_RUN={DRY_RUN}')

def daily_main(context):
    today = str(context.blotter.current_dt.date())[:10]
    cal = _month_calendar(today)
    if not cal or today not in cal:
        return
    if HALT_RESET and g.halt:
        g.halt = False
        log.error('[熔断] 人工确认恢复, HALT_RESET 生效, 重新开仓; 请将 HALT_RESET 改回 False')

    managed = _managed_positions(context)
    held = sorted(managed)
    snap = _snapshot(held)
    if not snap and held:
        snap = _build_fallback(held)   # 回测: 快照不可用, 持仓定价兜底(熔断/熊市卖出需要)
    ratio = _index_position_ratio()

    # ── 每日 MA10 出清检查（熔断B方案下也照常执行：持仓按MA10自然退出）──
    exits = _check_ma10(held, g) if held else []
    for c in exits:
        g.days_below.pop(c, None)

    rebal = _is_rebalance_day(today, cal)
    if DRY_RUN or rebal or exits:
        log.info(f'==== {today} 主策略运行 ====')
    sells, buys_plan, selected = set(exits), {}, held
    trim_plan, add_plan = {}, {}   # 持仓 diff 重加权: 减仓/加仓(对齐 QMT 执行器)
    pool_snap = {}
    note = ''

    if ratio is None:
        note = '指数行情不可用, 当日停摆(仅MA10出清)'
        _notify(f'[ptrade主策略] {today} 000906指数K线取不到 → 停摆待人工检查')
    elif g.halt:
        note = '熔断B方案生效: 停新开仓, 仅MA10自然退出; 恢复需人工确认'
        selected = [c for c in held if c not in exits]
    elif ratio <= 0.30:
        # 熊市档: 清仓观望。现金升级国债久期待用户拍板后实现(TODO 决策点3.5)
        sells |= set(held)
        selected, buys_plan = [], {}
        note = f'熊市档({ratio:.0%})清仓观望, 现金留存(国债ETF升级待定)'
        g.pending_buys = {}
    elif rebal or exits:
        universe = _load_universe()
        amt_df = _try_history(45, 'money', universe)
        close_df = _try_history(45, 'close', universe, fq='dypre')
        # 全池快照: 买得起过滤+真实价股数计算都要用
        pool_snap = _snapshot(universe)
        if amt_df is None or close_df is None:
            _notify(f'[ptrade主策略] {today} 池子数据加载失败 → 当日停摆')
            note = '池子数据加载失败, 当日停摆'
        else:
            # 快照缺失(回测模块无实时行情/接口故障)时用 dypre 收盘价兜底, 保证回测也能走通。
            # 键必须是6位代码(close_df 的列是 pt 格式, 与选股函数的查询键不一致)。
            # 附带涨跌停价: 回测按当日收盘撮合, 限价挂到涨跌停才能保证成交(见 fallback 标记)
            if not pool_snap:
                hi_df = _try_history(5, 'high_limit', universe)
                lo_df = _try_history(5, 'low_limit', universe)
                hi = {_plain(c): float(hi_df[c].iloc[-1]) for c in hi_df.columns
                      if pd.notna(hi_df[c].iloc[-1])} if hi_df is not None else {}
                lo = {_plain(c): float(lo_df[c].iloc[-1]) for c in lo_df.columns
                      if pd.notna(lo_df[c].iloc[-1])} if lo_df is not None else {}
                pool_snap = {}
                for c in close_df.columns:
                    if pd.notna(close_df[c].iloc[-1]):
                        pool_snap[_plain(c)] = {
                            'last_px': float(close_df[c].iloc[-1]),
                            'up_px': hi.get(_plain(c)),
                            'down_px': lo.get(_plain(c)),
                            'fallback': True}
            if not pool_snap:
                _notify(f'[ptrade主策略] {today} 价格数据缺失 → 当日停摆')
                note = '价格数据缺失, 当日停摆'
            else:
                # 选股/股数计算要纯数字价格; 涨跌停门控要字典(up_px/down_px) → 分开
                prices_map = {k: v.get('last_px') for k, v in pool_snap.items()
                              if v and v.get('last_px')}
                cap = POOL_CAPITAL_BASE if DRY_RUN else _strategy_capital(context, pool_snap)
                effective_cap = cap * ratio
                pool_budget = POOL_CAPITAL_BASE * ratio
                if exits:
                    # MA10出清自动补买: 从TOP候补(1.5×N)中选, 跳过已持有（与 Linux 版同款）
                    candidates = _select_top_turnover(
                        amt_df, close_df, prices_map, pool_budget, int(N_HOLDINGS * 1.5))
                    repl = []
                    for c in candidates:
                        if c not in held and c not in exits and c not in repl:
                            repl.append(c)
                        if len(repl) >= len(exits):
                            break
                    held_after = [c for c in held if c not in exits] + repl
                    g.days_below = {k: v for k, v in g.days_below.items() if k in held_after}
                    log.info(f'[MA10出清] {exits} → 补买 {repl}')
                else:
                    held_after = list(held)

                if rebal:
                    # ── 调仓日: 全量重算（对照 Linux 版 run() 调仓分支）──
                    selected = _select_top_turnover(amt_df, close_df, prices_map,
                                                    pool_budget, N_HOLDINGS)
                    new_set, prev_set = set(selected), set(held_after)
                    # MA10 当日出清的不当天买回(同日卖买纯烧成本, 重入等下次调仓)
                    buy_list = [c for c in selected if c not in prev_set and c not in set(exits)]
                    sell_list = [c for c in held_after if c not in new_set]

                    # 换手控制: 单次最多替换50%
                    turnover = (len(buy_list) + len(sell_list)) / (2 * N_HOLDINGS)
                    if turnover > MAX_TURNOVER:
                        max_replace = int(N_HOLDINGS * MAX_TURNOVER)
                        old_to_replace = [c for c in held_after if c not in new_set]
                        sell_list = old_to_replace[:max_replace]
                        keep_old = set(held_after) - set(sell_list)
                        buy_list = [c for c in selected if c not in keep_old
                                    and c not in set(exits)][:max_replace]
                        selected = list(keep_old) + buy_list
                        selected = selected[:N_HOLDINGS]
                        log.info(f'换手{turnover:.0%}超上限 → 分批: 卖{len(sell_list)} 买{len(buy_list)}')

                    sells |= set(sell_list)
                    shares = _calc_shares(selected, prices_map, effective_cap)
                    buys_plan = {c: shares[c] for c in buy_list if shares.get(c)}
                    # 持仓 diff 重加权: 存量仓位调整到目标股数(消除资金闲置的完整修复,
                    # 也修复"调仓日MA10补买候选从不下单"的遗漏——不在managed的按0股算)
                    for c in selected:
                        if c in buy_list:
                            continue
                        cur = getattr(managed.get(c), 'amount', 0) or 0
                        tgt = shares.get(c, 0)
                        if tgt > 0 and cur > tgt + 100:
                            trim_plan[c] = cur - tgt
                        elif tgt > cur + 100:
                            add_plan[c] = tgt - cur
                    if trim_plan or add_plan:
                        log.info(f'[调仓] 权重调整: 减{len(trim_plan)}只 加{len(add_plan)}只')
                    g.days_below = {k: v for k, v in g.days_below.items() if k in selected}
                    _industry_check(selected)
                else:
                    # 非调仓日仅MA10补买: 用补买候选的股数
                    if exits:
                        shares = _calc_shares(held_after, prices_map, effective_cap)
                        buys_plan = {c: shares.get(c, 0) for c in held_after
                                     if c not in held and shares.get(c)}
                    selected = held_after

    # ── 执行: 先卖后买 ──
    if DRY_RUN:
        log.warning('[DRY_RUN] 跳过下单: 卖%s 减%s 买%s' % (
            sorted(sells), sorted(trim_plan), list(buys_plan)))
        sells, trim_plan, buys_plan, add_plan = [], {}, {}, {}
    for code in sorted(sells):
        pos = managed.get(code)
        if pos is None:
            continue
        amt = getattr(pos, 'amount', 0) or 0
        if amt <= 0:
            continue
        sd = snap.get(code) or pool_snap.get(code)
        if not sd or not sd.get('last_px'):
            g.pending_sells.append(code)          # 无行情(停牌?) → 次日再试
            continue
        if sd.get('down_px') and sd['last_px'] <= sd['down_px']:
            g.pending_sells.append(code)          # 跌停禁卖
            log.warning(f'{code} 跌停禁卖, 延至次日')
            continue
        # 回测兜底: 限价挂跌停价, 保证收盘撮合可成交; 实盘用真实快照价
        px = sd.get('down_px') if sd.get('fallback') and sd.get('down_px') else sd['last_px']
        if _sell_all(code, amt, px):
            g.days_below.pop(code, None)

    # 减仓(权重调整): 只挂一次, 不进入次日重试(下个调仓日会重新对齐)
    for code, sh in sorted(trim_plan.items()):
        pos = managed.get(code)
        if pos is None:
            continue
        amt = getattr(pos, 'amount', 0) or 0
        if amt <= 0:
            continue
        sd = snap.get(code) or pool_snap.get(code)
        if not sd or not sd.get('last_px'):
            continue
        if sd.get('down_px') and sd['last_px'] <= sd['down_px']:
            log.warning(f'{code} 跌停禁卖(减仓跳过)')
            continue
        px = sd.get('down_px') if sd.get('fallback') and sd.get('down_px') else sd['last_px']
        _sell_shares(code, min(sh, amt), px)

    exec_buys = dict(buys_plan)
    exec_buys.update(add_plan)
    for code, sh in sorted(exec_buys.items()):
        sd = pool_snap.get(code) or snap.get(code)
        if not sd or not sd.get('last_px'):
            g.pending_buys[code] = sh              # 无行情 → 次日再试
            continue
        if sd.get('up_px') and sd['last_px'] >= sd['up_px']:
            g.pending_buys[code] = sh              # 涨停禁买
            log.warning(f'{code} 涨停禁买, 延至次日')
            continue
        # 回测兜底: 限价挂涨停价, 保证收盘撮合可成交; 实盘用真实快照价
        px = sd.get('up_px') if sd.get('fallback') and sd.get('up_px') else sd['last_px']
        if _buy(code, sh, px):
            g.pending_buys.pop(code, None)

    g.pending_sells = sorted(set(g.pending_sells))
    # 对账用标准行（与 Linux 信号 signal_a_latest.json 逐字段比对）。
    # 回测日志瘦身: 安静日不输出日频日志(防日志超客户端显示上限, 2026-09-03);
    # DRY_RUN 观察期始终输出(逐日对账需要每天有 SIGNAL 行)
    if DRY_RUN or rebal or exits or sells or buys_plan or (ratio is not None and ratio <= 0.30):
        log.info('SIGNAL|%s|ratio=%.2f|rebal=%s|halt=%s|dryrun=%s|selected=%s|sell=%s|buy=%s|note=%s' % (
            today, ratio or 0, rebal, g.halt, DRY_RUN,
            ','.join(selected), ','.join(sorted(sells)),
            ','.join(sorted(buys_plan)), note))
        # 日常摘要用 info 级别; log.error 只留给停摆/熔断等真异常(避免日志天天"报错")
        log.info(f'[ptrade主策略] {today} 仓位{ratio or 0:.0%} 持仓{len(selected)}只 '
                 f'卖{len(sells)} 买{len(buys_plan)} {note}')
    _persist_state()

def retry_orders(context):
    """次日09:35重试昨日未成交（跌停禁卖/涨停禁买/废单），撤旧单防重复"""
    if DRY_RUN:
        g.pending_buys, g.pending_sells = {}, []
        return
    _cancel_unfilled(context)
    managed = _managed_positions(context)
    snap = _snapshot(list(managed) + list(g.pending_buys))

    still_sell = []
    for code in g.pending_sells:
        pos = managed.get(code)
        if pos is None:
            continue                               # 昨日已成交
        amt = getattr(pos, 'amount', 0) or 0
        if amt <= 0:
            continue
        sd = snap.get(code)
        if not sd or not sd.get('last_px'):
            still_sell.append(code)
            continue
        if sd.get('down_px') and sd['last_px'] <= sd['down_px']:
            still_sell.append(code)                # 仍跌停
            continue
        if not _sell_all(code, amt, sd['last_px']):
            still_sell.append(code)
    g.pending_sells = still_sell

    still_buy = {}
    for code, sh in g.pending_buys.items():
        pos = managed.get(code)
        gap = sh - (getattr(pos, 'amount', 0) or 0)   # 昨日可能已部分成交
        if gap <= 0:
            continue
        sd = snap.get(code)
        if not sd or not sd.get('last_px'):
            still_buy[code] = gap
            continue
        if sd.get('up_px') and sd['last_px'] >= sd['up_px']:
            still_buy[code] = gap                  # 仍涨停
            continue
        if not _buy(code, gap, sd['last_px']):
            still_buy[code] = gap
    g.pending_buys = still_buy
    _persist_state()

def eod(context):
    """收盘后: 撤未成交、更新净值、回撤熔断检查、持久化"""
    _cancel_unfilled(context)
    snap = _snapshot(list(_managed_positions(context)))
    nav = _account_nav(context, snap)
    if g.nav_high is None or nav > g.nav_high:
        g.nav_high = nav
    dd = 1 - nav / g.nav_high if g.nav_high else 0.0
    if dd >= MAX_ACCOUNT_DRAWDOWN and not g.halt:
        g.halt = True
        _notify(f'[熔断B方案] 回撤 {dd:.1%} ≥ {MAX_ACCOUNT_DRAWDOWN:.0%}: '
                f'停止新开仓, 持仓按MA10自然退出, 恢复需人工确认')
    log.info(f'[EOD] nav={nav:,.0f} 高水位={g.nav_high:,.0f} 回撤={dd:.1%} halt={g.halt} '
             f'待卖={g.pending_sells} 待买={list(g.pending_buys)}')
    _persist_state()

# ══════════════════════════════════════════════════════════════════
# 状态持久化（2026-09-02 官方文档确认：平台原生 pickle 持久化 g，
# 每次事件后自动保存；重启时先跑 initialize 再用持久化值覆盖 → 无需自建文件持久化）
# ══════════════════════════════════════════════════════════════════

def _persist_state():
    pass

def _restore_state():
    pass

CSI800_LIST = [
    '000001', '000002', '000009', '000021', '000027', '000032', '000034', '000039', '000050', '000060',
    '000062', '000063', '000088', '000100', '000155', '000157', '000166', '000301', '000333', '000338',
    '000400', '000408', '000415', '000423', '000425', '000429', '000513', '000519', '000528', '000537',
    '000538', '000539', '000559', '000568', '000582', '000591', '000596', '000598', '000617', '000623',
    '000625', '000629', '000630', '000651', '000657', '000661', '000683', '000703', '000708', '000709',
    '000723', '000725', '000728', '000729', '000733', '000737', '000738', '000739', '000750', '000768',
    '000776', '000783', '000785', '000786', '000792', '000800', '000807', '000825', '000830', '000831',
    '000858', '000878', '000883', '000887', '000893', '000895', '000898', '000921', '000932', '000937',
    '000938', '000951', '000959', '000960', '000963', '000967', '000975', '000977', '000983', '000987',
    '000988', '000997', '000999', '001203', '001221', '001280', '001286', '001309', '001386', '001389',
    '001391', '001696', '001965', '001979', '002001', '002007', '002008', '002025', '002027', '002028',
    '002032', '002044', '002049', '002050', '002056', '002064', '002065', '002074', '002078', '002085',
    '002120', '002126', '002130', '002131', '002138', '002142', '002152', '002153', '002155', '002157',
    '002179', '002185', '002195', '002202', '002203', '002223', '002230', '002236', '002241', '002244',
    '002252', '002261', '002262', '002265', '002266', '002271', '002273', '002281', '002299', '002304',
    '002311', '002312', '002318', '002335', '002340', '002352', '002353', '002371', '002384', '002402',
    '002407', '002409', '002410', '002414', '002415', '002422', '002423', '002429', '002430', '002432',
    '002436', '002444', '002460', '002461', '002463', '002465', '002466', '002472', '002475', '002487',
    '002493', '002500', '002508', '002517', '002532', '002558', '002568', '002583', '002594', '002600',
    '002601', '002602', '002603', '002608', '002624', '002625', '002648', '002670', '002673', '002683',
    '002709', '002714', '002736', '002738', '002739', '002756', '002773', '002797', '002812', '002821',
    '002831', '002837', '002841', '002850', '002851', '002916', '002920', '002926', '002938', '002939',
    '002945', '002966', '002984', '003021', '003022', '003031', '003035', '003816', '300001', '300002',
    '300003', '300012', '300014', '300015', '300017', '300024', '300033', '300037', '300054', '300058',
    '300059', '300073', '300100', '300115', '300122', '300124', '300136', '300140', '300142', '300144',
    '300146', '300207', '300223', '300251', '300274', '300285', '300308', '300316', '300339', '300346',
    '300373', '300383', '300390', '300394', '300395', '300408', '300413', '300418', '300432', '300433',
    '300442', '300450', '300454', '300458', '300474', '300475', '300476', '300487', '300496', '300498',
    '300502', '300548', '300558', '300567', '300570', '300604', '300620', '300623', '300627', '300628',
    '300661', '300666', '300676', '300677', '300679', '300699', '300718', '300724', '300735', '300748',
    '300750', '300751', '300757', '300759', '300760', '300763', '300803', '300832', '300857', '300866',
    '300888', '300896', '300919', '300953', '300957', '300972', '300999', '301165', '301200', '301236',
    '301269', '301301', '301308', '301358', '301377', '301498', '301526', '301536', '301606', '301611',
    '302132', '600000', '600004', '600008', '600009', '600010', '600011', '600015', '600016', '600018',
    '600019', '600021', '600023', '600025', '600026', '600027', '600028', '600029', '600030', '600031',
    '600032', '600036', '600038', '600039', '600048', '600050', '600060', '600061', '600062', '600066',
    '600085', '600089', '600095', '600098', '600100', '600104', '600105', '600109', '600111', '600115',
    '600118', '600126', '600131', '600132', '600141', '600143', '600150', '600153', '600157', '600160',
    '600161', '600166', '600170', '600171', '600176', '600177', '600183', '600188', '600196', '600208',
    '600219', '600221', '600233', '600256', '600276', '600282', '600292', '600295', '600298', '600299',
    '600309', '600312', '600316', '600329', '600332', '600339', '600346', '600348', '600350', '600352',
    '600362', '600363', '600369', '600372', '600377', '600378', '600380', '600390', '600392', '600398',
    '600406', '600415', '600426', '600435', '600436', '600438', '600460', '600482', '600483', '600486',
    '600489', '600497', '600498', '600499', '600511', '600515', '600516', '600517', '600519', '600521',
    '600522', '600535', '600536', '600546', '600547', '600549', '600562', '600563', '600566', '600570',
    '600578', '600582', '600583', '600584', '600585', '600588', '600595', '600598', '600600', '600601',
    '600602', '600606', '600637', '600642', '600655', '600660', '600663', '600674', '600685', '600688',
    '600690', '600699', '600704', '600707', '600711', '600737', '600741', '600754', '600760', '600763',
    '600764', '600765', '600795', '600801', '600803', '600808', '600809', '600816', '600820', '600845',
    '600848', '600862', '600863', '600871', '600873', '600875', '600879', '600884', '600885', '600886',
    '600887', '600893', '600900', '600901', '600905', '600906', '600909', '600918', '600919', '600926',
    '600927', '600930', '600938', '600941', '600958', '600967', '600968', '600970', '600977', '600985',
    '600988', '600989', '600995', '600998', '600999', '601000', '601001', '601006', '601009', '601012',
    '601016', '601018', '601019', '601021', '601058', '601059', '601066', '601077', '601088', '601098',
    '601099', '601100', '601106', '601108', '601111', '601112', '601117', '601118', '601127', '601128',
    '601136', '601138', '601139', '601155', '601156', '601162', '601166', '601169', '601179', '601186',
    '601198', '601211', '601212', '601216', '601225', '601228', '601229', '601233', '601236', '601238',
    '601288', '601298', '601318', '601319', '601328', '601336', '601360', '601377', '601390', '601398',
    '601399', '601456', '601555', '601567', '601577', '601598', '601600', '601601', '601607', '601608',
    '601611', '601615', '601618', '601628', '601633', '601658', '601665', '601666', '601668', '601669',
    '601688', '601689', '601696', '601698', '601699', '601717', '601727', '601728', '601766', '601788',
    '601799', '601800', '601808', '601816', '601818', '601825', '601838', '601857', '601865', '601866',
    '601868', '601869', '601872', '601877', '601878', '601880', '601881', '601888', '601898', '601899',
    '601901', '601916', '601919', '601928', '601939', '601958', '601966', '601985', '601988', '601990',
    '601991', '601995', '601997', '601998', '603000', '603019', '603049', '603077', '603087', '603092',
    '603119', '603129', '603156', '603160', '603175', '603179', '603225', '603233', '603256', '603259',
    '603260', '603288', '603290', '603296', '603298', '603308', '603338', '603341', '603345', '603369',
    '603379', '603392', '603444', '603486', '603501', '603529', '603565', '603568', '603589', '603596',
    '603605', '603606', '603650', '603658', '603659', '603688', '603699', '603728', '603737', '603766',
    '603786', '603799', '603806', '603816', '603833', '603858', '603885', '603893', '603899', '603920',
    '603939', '603979', '603986', '603993', '605117', '605358', '605499', '605589', '688002', '688008',
    '688009', '688012', '688017', '688018', '688019', '688027', '688036', '688037', '688041', '688047',
    '688052', '688065', '688072', '688082', '688099', '688111', '688114', '688120', '688122', '688126',
    '688166', '688169', '688172', '688180', '688183', '688187', '688188', '688192', '688200', '688213',
    '688220', '688223', '688234', '688235', '688248', '688256', '688266', '688271', '688278', '688281',
    '688295', '688297', '688301', '688303', '688313', '688318', '688322', '688331', '688336', '688343',
    '688347', '688349', '688361', '688363', '688375', '688385', '688387', '688396', '688411', '688425',
    '688469', '688472', '688475', '688498', '688506', '688520', '688521', '688538', '688561', '688563',
    '688568', '688578', '688582', '688599', '688608', '688615', '688617', '688629', '688676', '688692',
    '688702', '688708', '688709', '688728', '688772', '688777', '688778', '688819', '688981', '689009',
]

# END_MARKER_9F3K
