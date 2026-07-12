"""
数据回溯 — 仅回溯CSI800+持仓+指数(~850只), Sina直连, 8秒超时
用法: python -u scripts/backfill_daily_data.py
"""
import sys, time, pandas as pd, akshare as ak
from pathlib import Path
from datetime import date
sys.path.insert(0, str(Path(__file__).parent.parent))
from data.storage import load_meta, save_daily, load_daily

COLS = ["date","code","open","high","low","close","volume","amount","pct_chg"]
TIMEOUT = 10  # seconds per stock max

def fetch_sina(code, target_date):
    prefix = "sh" if code.startswith("6") else ("bj" if code[:1] in "489" else "sz")
    date_str = target_date.replace("-", "")
    try:
        import signal
        def handler(s, f): raise TimeoutError()
        signal.signal(signal.SIGALRM, handler)
        signal.alarm(TIMEOUT)
        df = ak.stock_zh_a_daily(symbol=f"{prefix}{code}",
            start_date=date_str, end_date=date_str, adjust="qfq")
        signal.alarm(0)
        if df is None or df.empty:
            return None
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
        df["code"] = code
        df["pct_chg"] = (df["close"].astype(float).pct_change() * 100).round(4)
        return df[COLS].sort_values("date")
    except:
        try: signal.alarm(0)
        except: pass
        return None

def get_target_codes():
    """CSI800 + 持仓 + 指数 = 约850只"""
    codes = set()
    for meta_name in ['csi800']:
        try:
            df = load_meta(meta_name)
            for c in df['code'].tolist():
                codes.add(str(c).zfill(6))
        except: pass
    try:
        hdf = pd.read_csv("config/my_holdings.csv", dtype={"code": str})
        for c in hdf['code']:
            codes.add(c.zfill(6))
    except: pass
    # 指数码(000001等)与同名个股共用parquet路径, 用fetch_sina(个股API)拉会污染指数日线,
    # 坏掉MA200择时。指数由 rebuild_index_data.py / daily_data_update._update_index_daily 专门处理。
    return sorted(codes)

# 与个股代码冲突的指数码: 绝不用个股API回补(否则污染指数parquet → 坏MA200)
INDEX_SKIP = {'000001', '399006', '000300', '000905', '000688', '000906'}

def main():
    codes = [c for c in get_target_codes() if c not in INDEX_SKIP]
    missing = ['2026-07-01', '2026-07-02', '2026-07-03', '2026-07-06']
    print(f"目标: {len(codes)}只 × {len(missing)}天 = {len(codes)*len(missing)}次请求", flush=True)

    for target_date in missing:
        t0 = time.time()
        ok, fail = 0, 0
        for i, code in enumerate(codes):
            df = fetch_sina(code, target_date)
            if df is not None and not df.empty:
                save_daily(code, df)
                ok += 1
            else:
                fail += 1

            if (i + 1) % 200 == 0:
                elapsed = time.time() - t0
                rate = (i+1) / elapsed
                eta = (len(codes) - i - 1) / rate / 60
                print(f"  {target_date} [{i+1}/{len(codes)}] ✓{ok} ✗{fail} {rate:.1f}只/s 剩余{eta:.0f}min", flush=True)

        elapsed = time.time() - t0
        print(f"✅ {target_date}: ✓{ok} ✗{fail} ({elapsed:.0f}s)", flush=True)

    # 验证
    print("\n验证:", flush=True)
    for code in ['600030','300059','002475','000001','399006']:
        df = load_daily(code, '2026-06-30', '2026-07-06')
        if not df.empty:
            df['date_s'] = pd.to_datetime(df['date'])
            dates = sorted(df['date_s'].unique())
            print(f"  {code}: {dates[0].strftime('%m/%d')}~{dates[-1].strftime('%m/%d')} ({len(dates)}天)", flush=True)
        else:
            print(f"  {code}: 无数据", flush=True)

    print("\n✅ 回溯完成", flush=True)

if __name__ == '__main__':
    main()
