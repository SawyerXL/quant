"""
盘中实时监测 — 每小时拉取持仓+QMT实时数据
监测: 个人持仓(11只) + QMT持仓(从Windows同步)
数据: MCP优先 → akshare兜底
告警: 止损触发/量比异常/内外盘失衡
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import json, pandas as pd, numpy as np
from datetime import datetime, timedelta
from loguru import logger

CACHE_FILE = Path("logs/intraday_snapshot.json")
HOLDINGS_FILE = Path("config/my_holdings.csv")
STOP_THRESHOLDS = {"absolute": -0.12, "trailing": -0.18, "ma10_days": 3}

logger.add("logs/intraday_monitor.log", rotation="7 days", retention="14 days")

def get_all_codes():
    """获取所有需要监测的股票代码"""
    codes = set()

    # 个人持仓
    if HOLDINGS_FILE.exists():
        df = pd.read_csv(HOLDINGS_FILE, dtype={"code": str})
        df["code"] = df["code"].str.zfill(6)
        for _, r in df.iterrows():
            if r.get("monitor", True):
                codes.add(r["code"])

    # QMT持仓
    try:
        qmt_file = Path("logs/qmt_positions_cache.json")
        if qmt_file.exists():
            qmt = json.loads(qmt_file.read_text(encoding="utf-8"))
            for code in qmt.get("positions", {}):
                codes.add(code)
    except Exception:
        pass

    return sorted(codes)

def fetch_realtime_mcp(codes):
    """MCP实时数据(盘中快照, 含量比/内外盘)"""
    try:
        from data.source.mcp_source import MCPSource
        src = MCPSource()
        today = datetime.now().strftime("%Y-%m-%d")
        results = {}
        for code in codes:
            try:
                df = src.get_daily(code, today, today)
                if df.empty: continue
                cols = list(df.columns)
                if '最新价(元)' in cols:
                    results[code] = {
                        "price": float(df['最新价(元)'].iloc[0]),
                        "chg_pct": float(df.get('pct_chg', [0]).iloc[0]),
                        "vol_ratio": float(df.get('量比', [0]).iloc[0]) if df.get('量比', ['-']).iloc[0] != '-' else None,
                        "outer": float(df.get('外盘(万手)', [0]).iloc[0]) if '外盘(万手)' in cols else None,
                        "inner": float(df.get('内盘(万手)', [0]).iloc[0]) if '内盘(万手)' in cols else None,
                        "name": str(df.get('股票名称', [code]).iloc[0]),
                        "source": "mcp_intraday",
                    }
                elif 'close' in cols:
                    results[code] = {
                        "price": float(df['close'].iloc[0]),
                        "chg_pct": float(df.get('pct_chg', [0]).iloc[0]),
                        "source": "mcp_eod",
                    }
            except Exception:
                continue
        return results
    except Exception as e:
        logger.warning(f"MCP失败: {e}")
        return {}

def fetch_realtime_akshare(codes):
    """akshare实时行情(主数据源, 稳定, 含量比/内外盘)"""
    import akshare as ak
    results = {}
    spot = ak.stock_zh_a_spot_em()
    for code in codes:
        row = spot[spot['代码'] == code]
        if row.empty: continue
        r = row.iloc[0]
        vol_ratio = float(r['量比']) if r.get('量比') and str(r['量比']) != '-' else None
        results[code] = {
            "price": float(r['最新价']),
            "chg_pct": float(r['涨跌幅']),
            "vol_ratio": vol_ratio,
            "amount": float(r.get('成交额', 0)) if r.get('成交额') else 0,
            "name": str(r.get('名称', code)),
            "source": "akshare",
        }
    return results

def get_stock_name(code):
    """多源查股票名称"""
    # 1. 从 stock_info_full 查
    try:
        from data.storage import load_meta
        info = load_meta('stock_info_full')
        row = info[info['code'] == code]
        if not row.empty:
            return str(row['name'].iloc[0])
    except: pass
    # 2. 从持仓文件查
    if HOLDINGS_FILE.exists():
        try:
            df = pd.read_csv(HOLDINGS_FILE, dtype={"code": str})
            df["code"] = df["code"].str.zfill(6)
            row = df[df["code"] == code]
            if not row.empty and 'name' in row.columns:
                return str(row['name'].iloc[0])
        except: pass
    return code  # 兜底返回代码

def check_alerts(snapshots, cost_prices):
    """检查止损触发"""
    alerts = []
    for code, snap in snapshots.items():
        price = snap.get("price", 0)
        cost = cost_prices.get(code)
        name = snap.get("name", "") or get_stock_name(code)
        if not cost or not price: continue

        pnl = price / cost - 1
        if pnl <= STOP_THRESHOLDS["absolute"]:
            alerts.append(f"🔴 {code} {name}: 绝对止损{pnl:+.1%} (¥{price:.2f})")
        elif pnl <= -0.08:
            alerts.append(f"🟡 {code} {name}: 接近止损{pnl:+.1%}")

        # 量比异常
        vr = snap.get("vol_ratio")
        if vr and vr > 3.0:
            outer = snap.get("outer", 0) or 0
            inner = snap.get("inner", 0) or 0
            direction = "买入↑" if outer > inner else ("卖出↓" if inner > outer else "均衡")
            alerts.append(f"📊 {code} {name}: 极端放量{vr:.1f}x {direction}")

    return alerts

def run():
    now = datetime.now()
    codes = get_all_codes()
    logger.info(f"盘中监测: {now:%H:%M} 共{len(codes)}只")

    # 读取成本价
    cost_prices = {}
    if HOLDINGS_FILE.exists():
        df = pd.read_csv(HOLDINGS_FILE, dtype={"code": str})
        df["code"] = df["code"].str.zfill(6)
        for _, r in df.iterrows():
            if pd.notna(r.get("cost_price")) and float(r["cost_price"]) > 0:
                cost_prices[r["code"]] = float(r["cost_price"])

    # 拉数据(akshare主源, MCP兜底)
    snapshots = {}
    try:
        snapshots = fetch_realtime_akshare(codes)
        logger.info(f"  akshare: {len(snapshots)}只")
    except Exception as e:
        logger.warning(f"  akshare失败: {e}")

    # MCP兜底(只补akshare没拉到的)
    missing = [c for c in codes if c not in snapshots]
    if missing:
        try:
            mcp_data = fetch_realtime_mcp(missing)
            snapshots.update(mcp_data)
            logger.info(f"  MCP兜底: +{len(mcp_data)}只")
        except Exception:
            pass

    # 检查告警
    alerts = check_alerts(snapshots, cost_prices)

    # 保存
    output = {
        "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
        "total_codes": len(codes),
        "snapshots": len(snapshots),
        "akshare_count": len(snapshots),
        "alerts": alerts,
        "data": {},
    }

    # 格式化每只股票
    for code in sorted(snapshots.keys()):
        s = snapshots[code]
        output["data"][code] = {
            "name": s.get("name", "?"),
            "price": s["price"],
            "chg_pct": s.get("chg_pct", 0),
            "vol_ratio": s.get("vol_ratio"),
            "outer": s.get("outer"),
            "inner": s.get("inner"),
            "source": s.get("source", "?"),
        }

    CACHE_FILE.parent.mkdir(exist_ok=True)
    CACHE_FILE.write_text(json.dumps(output, ensure_ascii=False, indent=2))

    # 打印摘要
    print(f"\n{'='*50}")
    print(f"  盘中监测 {now:%H:%M} | {len(snapshots)}/{len(codes)}只 | akshare实时")
    print(f"{'='*50}")
    for code in sorted(snapshots.keys()):
        s = snapshots[code]
        name = s.get("name") or get_stock_name(code)
        vr = s.get("vol_ratio")
        vr_str = f"量比{vr:.1f}x" if vr else ""
        outer = s.get("outer"); inner = s.get("inner")
        oi_str = ""
        if outer and inner:
            oi_str = f"外{outer:.1f}/内{inner:.1f}"
        print(f"  {code} {name:<8} ¥{s['price']:.2f} {s.get('chg_pct',0):+.2f}% {vr_str} {oi_str}")

    if alerts:
        print(f"\n  ⚠️ 告警:")
        for a in alerts: print(f"    {a}")
        try:
            from monitoring.alerts import send_alert
            send_alert(f"【盘中监测 {now:%H:%M}】\n" + "\n".join(alerts))
        except Exception:
            pass

    print(f"{'='*50}\n")

if __name__ == "__main__":
    run()
