"""
盘中实时监测：每5分钟拉取监测股实时行情，推送到Linux。
Windows 计划任务：09:30-15:00 每5分钟运行一次。

用法: python scripts/intraday_watchlist.py
"""
import json, os, subprocess, sys
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

WATCHLIST = {"601899": "紫金矿业","603392": "万泰生物","600893": "航发动力",
             "300124": "汇川技术","002559": "亚威股份"}

LINUX_SERVER = os.getenv("LINUX_SERVER", "47.116.166.139")
LINUX_USER   = os.getenv("LINUX_USER", "root")
SSH_KEY      = os.getenv("SSH_KEY", "")


def get_quotes() -> list[dict]:
    """通过 xtdata 获取实时行情。"""
    from xtquant import xtdata
    xt_codes = []
    for code in WATCHLIST:
        if code.startswith(("60","68")): xt_codes.append(code + ".SH")
        else: xt_codes.append(code + ".SZ")

    results = []
    for xt_code in xt_codes:
        code = xt_code.split(".")[0]
        try:
            data = xtdata.get_market_data(['close','open','high','low','volume'],
                                          [xt_code], period='1d', count=15)
            if not data or 'close' not in data: continue
            raw = data['close'].values()
            closes = []
            for v in raw:
                try:
                    val = float(v.item()) if hasattr(v, 'item') else float(v[0])
                    if val > 0: closes.append(val)
                except Exception:
                    continue
            if len(closes) < 5: continue
            flat_c = closes
            cur = flat_c[-1]; ma10 = sum(flat_c[-10:]) / min(len(flat_c), 10)
            ret_5d = (cur / flat_c[-6] - 1) * 100 if len(flat_c) >= 6 else 0
            below = 0
            for c in reversed(flat_c):
                if c < ma10: below += 1
                else: break
            if below >= 3: sig = "SELL"
            elif cur > ma10 and ret_5d > 2: sig = "BUY"
            elif cur > ma10: sig = "HOLD"
            else: sig = "WAIT"
            results.append({"code":code,"name":WATCHLIST[code],"cur":cur,"ma10":round(ma10,2),
                           "ret_5d":round(ret_5d,1),"below":below,"signal":sig})
        except Exception as e:
            results.append({"code":code,"name":WATCHLIST[code],"error":str(e)[:40]})
    return results


def main():
    now = datetime.now()
    # 非交易时段跳过
    if now.weekday() >= 5: return
    t = now.hour * 60 + now.minute
    if t < 570 or t > 900: return  # 09:30前或15:00后

    print(f"  {now.strftime('%H:%M')} 拉取行情...", end=" ", flush=True)
    try:
        results = get_quotes()
    except Exception as e:
        print(f"xtdata失败: {e}")
        return

    data = {"updated": now.strftime("%Y-%m-%dT%H:%M:%S"), "stocks": results}
    local = ROOT / "logs/intraday_watchlist.json"
    local.parent.mkdir(exist_ok=True)
    local.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"OK ({len(results)}只)")

    # SCP推回Linux
    ssh_opts = ["-o","StrictHostKeyChecking=no","-o","ConnectTimeout=8"]
    if SSH_KEY: ssh_opts += ["-i", SSH_KEY]
    remote = f"{LINUX_USER}@{LINUX_SERVER}:/root/quant/logs/intraday_watchlist.json"
    try:
        r = subprocess.run(["scp"] + ssh_opts + [str(local), remote],
                          capture_output=True, text=True, timeout=15)
    except Exception:
        pass  # 推送失败不阻塞


if __name__ == "__main__":
    main()
