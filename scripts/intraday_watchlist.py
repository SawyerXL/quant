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
            # 超跌反弹检测
            bounce=False; bounce_drop=0
            high_30d=max(flat_c); drop=(cur/high_30d-1)*100
            opens=[float(o.item()) if hasattr(o,'item') else float(o[0]) for o in data['open'].values() if o is not None and (hasattr(o,'item') or o[0])>0]
            open_today=opens[-1] if len(opens)>0 else cur
            if drop<=-15 and cur>open_today: bounce=True; bounce_drop=round(drop,1)

            if below >= 3: sig = "SELL"
            elif bounce: sig = "BOUNCE"
            elif cur > ma10 and ret_5d > 2: sig = "BUY"
            elif cur > ma10: sig = "HOLD"
            else: sig = "WAIT"
            results.append({"code":code,"name":WATCHLIST[code],"cur":cur,"ma10":round(ma10,2),
                           "ret_5d":round(ret_5d,1),"below":below,"signal":sig,
                           "bounce":bounce,"bounce_drop":bounce_drop})
        except Exception as e:
            results.append({"code":code,"name":WATCHLIST[code],"error":str(e)[:40]})
    return results


def send_email(subject: str, body: str):
    """通过Linux环境变量中的SMTP发送邮件。"""
    import smtplib
    from email.mime.text import MIMEText
    from email.header import Header
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    user = os.getenv("SMTP_USER", ""); pw = os.getenv("SMTP_PASSWORD", "")
    if not user or not pw: raise RuntimeError("SMTP not configured")
    srv = os.getenv("SMTP_SERVER", "smtp.yeah.net")
    port = int(os.getenv("SMTP_PORT", "465"))
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"]=user; msg["To"]=os.getenv("ALERT_EMAIL", user)
    msg["Subject"]=Header(subject,"utf-8")
    with smtplib.SMTP_SSL(srv, port, timeout=10) as s:
        s.login(user, pw); s.sendmail(user, [msg["To"]], msg.as_string())


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

    # 检测重要信号（SELL/BOUNCE/BUY）并邮件告警
    alert_file = ROOT / "logs/intraday_alerts_sent.json"
    sent = {}
    if alert_file.exists():
        try: sent = json.loads(alert_file.read_text(encoding="utf-8"))
        except Exception: pass

    new_alerts = []
    for s in results:
        if s.get("signal") in ("SELL", "BOUNCE", "BUY") and "error" not in s:
            key = f"{s['code']}_{s['signal']}"
            if key not in sent:
                tag = {"SELL":"SELL","BOUNCE":"BOUNCE","BUY":"BUY"}[s["signal"]]
                extra = ""
                if s["signal"] == "SELL": extra = f" MA10连续{s['below']}天跌破"
                elif s["signal"] == "BOUNCE": extra = f" 从高点跌{s['bounce_drop']}%后反弹"
                elif s["signal"] == "BUY": extra = f" MA10收复+5日{s['ret_5d']:+.1f}%"
                new_alerts.append(f"{tag} {s['code']} {s['name']} {s['cur']:.2f}{extra}")
                sent[key] = now.strftime("%m-%d %H:%M")

    if new_alerts:
        try:
            msg = " | ".join(new_alerts)
            subject = f"Quant Intraday Alert: {len(new_alerts)} signals"
            body = f"{now.strftime('%H:%M')}\n" + "\n".join(new_alerts)
            send_email(subject, body)
            print(f"  Email sent: {len(new_alerts)} alerts")
        except Exception as e:
            print(f"  Email failed: {e}")

    # 仅保留今天的已发送记录
    today_prefix = now.strftime("%m-%d")
    sent = {k: v for k, v in sent.items() if v.startswith(today_prefix)}
    alert_file.parent.mkdir(exist_ok=True)
    alert_file.write_text(json.dumps(sent, ensure_ascii=False))

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
