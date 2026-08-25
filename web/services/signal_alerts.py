"""持仓信号提醒 — 外盘领先信号 → 持仓操作建议（全部回测验证）"""
import sys, os
from pathlib import Path
from datetime import datetime, date

_project_root = Path(__file__).parent.parent.parent
os.chdir(_project_root)
sys.path.insert(0, str(_project_root))
sys.path.insert(0, str(_project_root / "scripts"))

# 信号→持仓映射（回测验证版）
SIGNAL_MAP = {
    # 2026-08-24 五个信号全部做了 gap/intraday 拆解回测（scripts/backtest_overseas_signal.py）。
    # 统一结论：预测力几乎全在隔夜跳空里，而跳空持仓者躲不掉；"开盘减仓"在10个组合里8个扣费后
    # 跑输躺平，金/SOX/恒生三个的盘中段方向甚至是反的（低开后反弹，减仓=卖在坑里）。
    # 所以 up_ok/down_ok 全部关掉操作建议，只保留跳空幅度提示。阈值一律未动。
    # rt = 新浪实时symbol：美股日线在美股盘中会滞后一个交易日，必须用实时值覆盖。
    #
    # 伦铜(CAD)：唯一方向正确的，扣费后紫金+1.8pp/洛钼+7.0pp，但盘中t仅-1.32/-2.16，
    # 多重检验校正后不显著。原用 HG 是 COMEX 美铜却标名"伦铜"，名实不符，一并换掉。
    'copper':  {'ak_func': 'futures_foreign_hist', 'symbol': 'CAD', 'thresh': 2.0, 'name': '伦铜LME',
                'up_ok': False, 'up_advice': '{stock}偏多，回测无操作价值',
                'down_advice': '{stock}预计低开1.4~1.8%；回测开盘减仓扣费后无显著收益，仅提示不操作'},
    # 纽约金：跌后盘中平均反弹+0.55%(t=1.74)，减半扣费-3.6pp且回撤恶化 -48.9%→-51.3%
    'gold':    {'ak_func': 'futures_foreign_hist', 'symbol': 'GC', 'thresh': 2.0, 'name': '纽约金',
                'up_ok': False, 'up_advice': '{stock}偏多，但次日盘中平均-0.45%，别追高',
                'down_advice': '{stock}预计低开2.5%；但开盘后平均反弹+0.55%，别恐慌减仓（减半扣费-3.6pp）'},
    # NBI：利好侧药明t=2.22是五个信号唯一超2的，但~20次检验下属期望内假阳性，不据此加仓
    'nbi':     {'ak_func': 'index_us_stock_sina', 'symbol': '.NBI', 'thresh': 2.0, 'name': 'NBI生科',
                'rt': 'gb_nbi', 'up_ok': False,
                'up_advice': '{stock}偏多，次日盘中+0.26~0.49%（五信号中唯一正向证据，未过多重检验）',
                'down_advice': '{stock}预计低开0.7~0.9%；回测开盘减仓扣费后无收益，仅提示不操作'},
    # SOX：跌后盘中反弹+0.25~0.32%，减半扣费-3.8~-5.3pp，科创50ETF/曙光回撤反而恶化
    'sox':     {'ak_func': 'index_us_stock_sina', 'symbol': '.SOX', 'thresh': 3.0, 'name': 'SOX半导体',
                'rt': 'gb_sox', 'up_ok': False,
                'up_advice': '{stock}偏多，但次日盘中平均-0.07~-0.15%，别追高',
                'down_advice': '{stock}预计低开0.8~1.1%；但开盘后平均反弹+0.25~0.32%，'
                               '别恐慌减仓（减半扣费-3.8~-5.3pp）'},
    # 恒生：与A股同步而非领先 —— 同日相关0.21~0.28，次日仅0.003~0.039，等于零预测力。
    # down_ok 一并关掉：它不该在持仓行里显示成红色利空。
    'hsi':     {'ak_func': 'stock_hk_index_daily_sina', 'symbol': 'HSI', 'thresh': 2.0, 'name': '恒生',
                'rt': 'rt_hkHSI', 'up_ok': False, 'down_ok': False,
                'up_advice': '恒生与A股同步而非领先（次日相关≈0.00），不构成{stock}操作依据',
                'down_advice': '恒生与A股同步而非领先（次日相关≈0.00），不构成{stock}操作依据'},
}

# 持仓 → 关注的信号
HOLDING_SIGNALS = {
    '601899': ['copper', 'gold'],       # 紫金矿业
    '603993': ['copper'],                # 洛阳钼业
    '603259': ['nbi'],                   # 药明康德
    '600276': ['nbi'],                   # 恒瑞医药
    '588000': ['sox', 'hsi'],            # 科创50ETF
    '603019': ['sox', 'hsi'],            # 中科曙光
    '002409': ['sox'],                   # 雅克科技
}

_SIGNAL_CACHE = {"ts": 0, "data": None}

# 实时行情映射（新浪盘中接口，美股盘中/港股当日可见）
RT_QUOTES = {
    'gb_sox': 'SOX实时', 'gb_ixic': '纳指实时', 'gb_inx': '标普实时',
    'gb_nbi': 'NBI实时',
    'rt_hkHSI': '恒生实时', 'rt_hkHSTECH': '恒生科技实时',
}


def _is_live(ts: str) -> bool:
    """行情时间戳距今15分钟内才算盘中。

    新浪收盘后会把时间戳冻结在收盘那一刻（美股04:xx、港股16:09），并不会继续跳动，
    所以"取自实时接口"≠"市场正在交易"，必须用时间戳判断，否则半夜也显示"盘中"。
    """
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
        try:
            return (datetime.now() - datetime.strptime(ts[:len(fmt) + 2], fmt)).total_seconds() < 900
        except ValueError:
            continue
    return False


def _fetch_realtime() -> dict:
    """拉取美股/港股实时行情（盘中可见，弥补日线滞后）"""
    import urllib.request
    results = {}
    try:
        url = 'https://hq.sinajs.cn/list=' + ','.join(RT_QUOTES.keys())
        req = urllib.request.Request(url, headers={'Referer': 'https://finance.sina.com.cn'})
        resp = urllib.request.urlopen(req, timeout=5)
        data = resp.read().decode('gbk')
        for line in data.strip().split('\n'):
            if '=' not in line or '"' not in line: continue
            sym = line.split('=')[0].strip()
            # 去掉 var hq_str_ 前缀
            for prefix in ('var hq_str_', 'var hq_str_rt_'):
                if sym.startswith(prefix):
                    sym = sym[len(prefix):]
                    break
            name = RT_QUOTES.get(sym, sym)
            parts = line.split('"')[1].split(',')
            # 美股: 0名称,1现价,2涨跌幅%,3北京时间,4涨跌额,...,26昨收
            # 港股: 0代码,1名称,...,6现价,7涨跌额,8涨跌幅%,...,17日期,18时间
            if sym.startswith('gb_') and len(parts) > 3 and parts[1]:
                try:
                    price = float(parts[1])
                    chg = float(parts[2]) if parts[2] else 0
                    ts = parts[3].strip()
                    results[sym] = {'name': name, 'price': round(price, 2), 'chg_pct': round(chg, 2),
                                    'asof': ts[:16], 'date': ts[:10], 'live': _is_live(ts)}
                except: pass
            elif sym.startswith('rt_hk') and len(parts) > 18:
                try:
                    price = float(parts[6])
                    chg = float(parts[8]) if parts[8] else 0
                    d = parts[17].strip().replace('/', '-')
                    ts = d + ' ' + parts[18].strip()
                    results[sym] = {'name': name, 'price': round(price, 2), 'chg_pct': round(chg, 2),
                                    'asof': ts[:16], 'date': d, 'live': _is_live(ts)}
                except: pass
    except Exception:
        pass
    return results


def _fetch_signal(key: str) -> dict:
    """拉取单个信号的最新日变化%。返回 {name, chg_pct, date}"""
    import akshare as ak
    cfg = SIGNAL_MAP[key]
    try:
        df = getattr(ak, cfg['ak_func'])(symbol=cfg['symbol'])
        if df is None or len(df) < 2:
            return None
        df = df.sort_values('date')
        last = df.iloc[-1]; prev = df.iloc[-2]
        chg = (float(last['close']) / float(prev['close']) - 1) * 100
        return {
            'key': key, 'name': cfg['name'],
            'chg_pct': round(chg, 2),
            'date': str(last['date'])[:10],
        }
    except Exception:
        return None


def get_holding_signal_alerts() -> dict:
    """为持仓生成信号提醒。缓存10分钟。"""
    global _SIGNAL_CACHE
    import time
    now = time.time()
    if _SIGNAL_CACHE["data"] and (now - _SIGNAL_CACHE["ts"]) < 600:
        return _SIGNAL_CACHE["data"]

    # 拉全部5个信号（日线）
    signals = {}
    for key in SIGNAL_MAP:
        s = _fetch_signal(key)
        if s:
            signals[key] = s

    realtime = _fetch_realtime()

    # 盘中优先：新浪日线在美股开盘时段(北京21:30-04:00)只到上一交易日，
    # 会让SOX显示上周五的-0.5%而实际盘中已-3.4%，导致横幅报警但持仓行写"无信号"。
    # 实时值永远不比日线旧（收盘后两者相等），所以有实时就用实时。
    for key, cfg in SIGNAL_MAP.items():
        rt = realtime.get(cfg.get('rt') or '')
        if not rt:
            continue
        # 收盘后实时值=最新收盘值，依然比日线新（日线在美股盘中滞后一天），所以照用，
        # 只是 live 要如实反映市场是否真的在交易
        signals[key] = {
            'key': key, 'name': cfg['name'],
            'chg_pct': rt['chg_pct'],
            'date': rt.get('asof') or rt.get('date', ''),
            'live': rt.get('live', False),
        }

    # 读持仓
    holdings = []
    try:
        import csv
        with open('config/my_holdings.csv') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get('monitor', 'True') == 'True' and int(float(row.get('shares', 0))) > 0:
                    holdings.append({
                        'code': row['code'].zfill(6),
                        'name': row['name'],
                        'shares': int(float(row['shares'])),
                    })
    except Exception:
        pass

    # 生成每个持仓的提醒（全部展示，含未触发）
    alerts = []
    for h in holdings:
        sig_keys = HOLDING_SIGNALS.get(h['code'], [])
        if not sig_keys:
            continue  # 无外盘锚的持仓不显示（如中信、万泰等）
        for sk in sig_keys:
            sig = signals.get(sk)
            if not sig:
                continue
            cfg = SIGNAL_MAP[sk]
            thresh = cfg['thresh']
            chg = sig['chg_pct']
            if chg >= thresh:
                # up_ok/down_ok 为 False 时仍显示方向，但不升级成操作建议（回测证伪）
                level = 'up' if cfg.get('up_ok', True) else 'neutral'
                advice = (cfg.get('up_advice') or '利好{stock}，可持有/低吸').format(stock=h['name'])
            elif chg <= -thresh:
                level = 'down' if cfg.get('down_ok', True) else 'neutral'
                advice = (cfg.get('down_advice') or '利空{stock}，开盘考虑减仓').format(stock=h['name'])
            else:
                level = 'neutral'
                advice = '无信号'
            alerts.append({
                'code': h['code'], 'stock': h['name'],
                'signal': sig['name'], 'signal_chg': chg,
                'level': level, 'advice': advice,
                'signal_date': sig['date'], 'live': sig.get('live', False),
                'threshold': thresh,
            })

    # 全局市场警报 — 与上方持仓行同源，避免"横幅报警但持仓写无信号"的自相矛盾。
    # 恒生→当晚美股：2026-08-25 复验通过（SOX -1.15%/S&P -1.13%, t=-1.86/-2.55，3019/3120样本）。
    # 唯一用途是防御"今晚美股"，不预测次日A股（那条件链已证零预测力）。
    market_alert = None
    sox = signals.get('sox')
    hsi = signals.get('hsi')
    if sox and sox['chg_pct'] <= -SIGNAL_MAP['sox']['thresh']:
        tag = '盘中' if sox.get('live') else '收盘'
        market_alert = {
            'signal': f"SOX{tag}", 'chg': sox['chg_pct'],
            'advice': f"美股{tag}SOX {sox['chg_pct']}% → 明早科技股大概率低开0.8~1.1%。"
                      f"回测：低开后盘中平均反弹+0.25~0.32%，开盘减半扣费后反而少赚2.5~5.3pp/年 —— "
                      f"做好低开心理准备即可，不要开盘杀跌。",
        }
    elif hsi and hsi.get('live') and hsi['chg_pct'] <= -2.0:
        market_alert = {
            'signal': '恒生→美股', 'chg': hsi['chg_pct'],
            'advice': f"恒生盘中{hsi['chg_pct']}% → 今晚美股大概率跟跌（复验：SOX当晚-1.15%/S&P-1.13%，"
                      f"3019样本，全框架唯一两轮验证存活的外盘信号）。仅提示今晚美股风险，"
                      f"不用于预测明天A股，也不建议据此对A股持仓操作。",
        }

    result = {
        'alerts': alerts,
        'market_alert': market_alert,
        'realtime': list(realtime.values()),
        'updated_at': datetime.now().strftime('%H:%M:%S'),
        'signal_status': [
            {'name': s['name'], 'chg_pct': s['chg_pct'], 'date': s['date'],
             'live': s.get('live', False)}
            for s in signals.values()
        ],
    }
    _SIGNAL_CACHE = {"ts": now, "data": result}
    return result
