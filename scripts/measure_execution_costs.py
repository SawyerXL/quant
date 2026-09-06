"""
静跑期成本实测聚合工具 — §6.8 条件决策规则(18bp 裁决)的取数/计算端（2026-09-06 建）
用法:
    python scripts/measure_execution_costs.py --pull            # 经2222隧道远程驱动Windows导出当日订单并拉回
    python scripts/measure_execution_costs.py --report          # 聚合全部已落盘订单+执行记录, 输出成本报表与18bp裁决
    python scripts/measure_execution_costs.py --pull --report   # 先拉再算(收盘后一条命令)

口径（与 §6.8 ④ 事前登记一致, 不得私自改动）:
  - 参考价: 一律用当日收盘价(local data_store), 与 §6.7 ①"成交价 vs 当日收盘价"同口径;
    历史 execution_result 里的"信号价"口径买入滑点仅作信息行展示, 不混入均值
  - 单边等效成本(bp) = 方向性滑点 + 佣金万2.5 + 印花税卖出千1, 按成交额加权平均
  - 标签: 做T=与 config/t_settle_log.csv 的腿(同code+同方向+时间±5分钟)匹配;
    调仓=当日存在 execution_result 记录; 其余股票单=MA10触发
  - 裁决对象: 调仓+MA10 的股票单(排除做T/CB/基金); 累计 ≥100 笔成交才可裁决
  - 阈值: 实测单边等效 >18bp → 切C; ≤18bp → 维持A(18bp 为 A/C 换手差与收益差的交叉点, 2026-09-06 钉死)
"""
import argparse
import json
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from loguru import logger
from config.settings import COMMISSION_RATE, STAMP_TAX

# ── 隧道/远端常量(与 qmt_sync.py 同一套) ─────────────────────────
SSH_KEY   = "~/.ssh/id_rsa"
SSH_PORT  = 2222
SSH_USER  = "Administrator"
SSH_HOST  = "127.0.0.1"
WIN_QUANT = "H:/quant"

LOGS_DIR      = ROOT / "logs"
ORDERS_PREFIX = "qmt_orders_"
EXEC_PREFIX   = "execution_result_"
T0_LOG        = ROOT / "config" / "t_settle_log.csv"

MEASURED_THRESHOLD_BP = 18.0   # §6.8 钉死: 实测单边等效交叉点
MIN_FILLS             = 100    # §6.8 钉死: 裁决所需最少成交笔数
A_TURNOVER            = 9.03   # 完整栈年换手 903%
C_TURNOVER            = 3.49   # 完整栈-MA10 年换手 349%

T0_MATCH_MINUTES = 5

# 正T: 涨2%抛1/3→回落1%接回(腿1卖/腿2买); 反T: 跌2%吸1/3→反弹1%卖(腿1买/腿2卖)
T0_LEG_SIDES = {"正T": ("sell", "buy"), "反T": ("buy", "sell")}


# ── 取数: 经隧道远程驱动 Windows ─────────────────────────────────
def pull_orders() -> bool:
    """Linux→Windows: 部署 dump_qmt_orders.py, 远程运行, scp 回结果并按 signal_date 归档。

    隧道断时静默失败(由 tunnel_watchdog 告警), 当日订单次日无法补取
    (get_today_orders 只覆盖当日), 故 crontab 15:55 收盘后拉取。
    """
    # 0. 先部署导出脚本(幂等; Windows 端 git 同步不可靠时的兜底)
    try:
        r0 = subprocess.run(
            ["scp", "-P", str(SSH_PORT), "-i", SSH_KEY,
             str(ROOT / "scripts" / "dump_qmt_orders.py"),
             f"{SSH_USER}@{SSH_HOST}:{WIN_QUANT}/scripts/dump_qmt_orders.py"],
            capture_output=True, timeout=30,
        )
        if r0.returncode != 0:
            logger.warning(f"导出脚本部署失败(SCP): {r0.stderr.strip()[:200]}")
            return False
    except Exception as e:
        logger.warning(f"导出脚本部署失败: {e}")
        return False

    export_cmd = (
        f'powershell -NoProfile -Command '
        f'"cd {WIN_QUANT}; python scripts/dump_qmt_orders.py"'
    )
    try:
        r1 = subprocess.run(
            ["ssh", "-i", SSH_KEY, "-p", str(SSH_PORT),
             "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
             "-o", "StrictHostKeyChecking=no",
             f"{SSH_USER}@{SSH_HOST}", export_cmd],
            capture_output=True, timeout=90,
        )
        if r1.returncode != 0:
            logger.warning(f"订单导出失败(SSH): {r1.stderr.strip()[:200]}")
            return False
    except Exception as e:
        logger.warning(f"订单导出失败: {e}")
        return False

    tmp = LOGS_DIR / "qmt_orders_tmp.json"
    try:
        r2 = subprocess.run(
            ["scp", "-P", str(SSH_PORT), "-i", SSH_KEY,
             f"{SSH_USER}@{SSH_HOST}:{WIN_QUANT}/logs/qmt_orders_latest.json",
             str(tmp)],
            capture_output=True, timeout=30,
        )
        if r2.returncode != 0 or not tmp.exists():
            logger.warning(f"订单拉取失败(SCP): {r2.stderr.strip()[:200]}")
            return False
    except Exception as e:
        logger.warning(f"订单拉取失败: {e}")
        return False

    payload = json.loads(tmp.read_text(encoding="utf-8"))
    signal_date = str(payload.get("signal_date") or date.today().strftime("%Y%m%d"))
    dest = LOGS_DIR / f"{ORDERS_PREFIX}{signal_date}.json"
    tmp.rename(dest)
    logger.info(f"订单拉取: {payload.get('order_count', 0)}笔 → {dest}")
    return True


# ── 数据装载 ─────────────────────────────────────────────────────
def load_exec_days() -> dict:
    """扫描 execution_result_*.json → {date: {code: 信号价}}。

    同日多 track(a/cb)合并; 信号价仅用于信息行, 不参与成本均值。
    """
    days = {}
    for f in sorted(LOGS_DIR.glob(f"{EXEC_PREFIX}*.json")):
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        d = str(rec.get("signal_date", "")).replace("-", "")
        if not d:
            continue
        days.setdefault(d, {})
        for s in rec.get("slippage_detail", []):
            code = str(s.get("code", "")).zfill(6)
            if s.get("assumed"):
                days[d].setdefault(code, s["assumed"])
    return days


def load_t0_legs() -> list:
    """t_settle_log.csv → [(date, code, side, seconds_of_day)] 已成交腿。

    强平腿 leg2_time 是完整日期时间, 分钟腿是 HH:MM:SS——统一转秒。
    """
    legs = []
    if not T0_LOG.exists():
        return legs
    df = pd.read_csv(T0_LOG, dtype={"code": str})
    for _, r in df.iterrows():
        d = str(r.get("date", "")).replace("-", "")
        code = str(r.get("code", "")).zfill(6)
        dirn = str(r.get("direction", ""))
        sides = T0_LEG_SIDES.get(dirn)
        if not sides:
            continue
        for i, side in enumerate(sides):
            t_col = "leg1_time" if i == 0 else "leg2_time"
            filled_col = "leg1_filled" if i == 0 else "leg2_filled"
            if str(r.get(filled_col, "")).strip() != "是":
                continue
            secs = _parse_time(r.get(t_col))
            if secs is not None:
                legs.append((d, code, side, secs))
    return legs


def _parse_time(t):
    t = str(t).strip()
    m = re.search(r"(\d{1,2}):(\d{2}):(\d{2})", t)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    if t.isdigit():  # epoch 秒/毫秒
        v = int(t)
        if v > 10**12:
            v //= 1000
        dt = datetime.fromtimestamp(v)
        return dt.hour * 3600 + dt.minute * 60 + dt.second
    return None


def load_close(code: str, d: str) -> float | None:
    from data.storage import load_daily
    try:
        df = load_daily(code, d, d)
        if not df.empty and "close" in df.columns:
            return float(df["close"].iloc[-1])
    except Exception:
        pass
    return None


def load_order_fills() -> list:
    """全部已落盘订单文件 → 已成交股票/ETF 单的标准化列表。

    price 优先成交价(traded_price), 缺失用限价单委托价近似。
    """
    fills = []
    for f in sorted(LOGS_DIR.glob(f"{ORDERS_PREFIX}*.json")):
        try:
            payload = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        d = str(payload.get("signal_date", "")).replace("-", "")
        for o in payload.get("orders", []):
            if int(o.get("filled", 0) or 0) <= 0:
                continue
            if o.get("status") == 57:  # 柜台废单
                continue
            code = str(o.get("code", "")).split(".")[0].zfill(6)
            price = float(o.get("fill_price", 0) or 0)
            price_src = "traded"
            if price <= 0:
                price = float(o.get("limit_price", 0) or 0)
                price_src = "limit"
            if price <= 0:
                continue
            fills.append({
                "date": d, "code": code,
                "side": o.get("direction", ""),
                "price": price, "price_src": price_src,
                "shares": int(o.get("filled", 0) or 0),
                "secs": _parse_time(o.get("order_time", "")),
            })
    return fills


def _is_stock(code: str) -> bool:
    return code[:1] in ("6", "0", "3")


# ── 聚合 ─────────────────────────────────────────────────────────
def build_report():
    exec_days = load_exec_days()
    t0_legs = load_t0_legs()
    fills = load_order_fills()

    rows = []
    for fl in fills:
        tag = "其他"
        if _is_stock(fl["code"]):
            tag = "调仓" if fl["date"] in exec_days else "MA10"
            for (d, code, side, secs) in t0_legs:
                if (d == fl["date"] and code == fl["code"] and side == fl["side"]
                        and fl["secs"] is not None
                        and abs(secs - fl["secs"]) <= T0_MATCH_MINUTES * 60):
                    tag = "做T"
                    break
        ref = load_close(fl["code"], fl["date"])
        slip_bp = None
        if ref and ref > 0:
            if fl["side"] == "buy":
                slip_bp = (fl["price"] / ref - 1) * 1e4
            else:
                slip_bp = (1 - fl["price"] / ref) * 1e4
        rows.append({**fl, "tag": tag, "ref": ref, "slip_bp": slip_bp})

    if not rows:
        return exec_days, [], []

    df = pd.DataFrame(rows)
    # 单边等效: 滑点 + 佣金(双向) + 印花税(仅卖出), 成交额加权
    df["fees_bp"] = COMMISSION_RATE * 1e4 + df["side"].map(
        lambda s: STAMP_TAX * 1e4 if s == "sell" else 0.0)
    df["cost_bp"] = df.apply(
        lambda r: (r["slip_bp"] + r["fees_bp"]) if r["slip_bp"] is not None else None,
        axis=1)
    df["notional"] = df["price"] * df["shares"]

    strategy = df[df["tag"].isin(["调仓", "MA10"])]
    measured = strategy[strategy["slip_bp"].notna()]

    return exec_days, df, measured


def side_eq_of(m: pd.DataFrame) -> float | None:
    """成交额加权的单边等效成本(bp)。"""
    if not len(m):
        return None
    return (m["cost_bp"] * m["notional"]).sum() / m["notional"].sum()


def _verdict_alert(measured: pd.DataFrame):
    """裁决状态翻转时告警一次(状态持久化, 避免每天刷屏)。"""
    from monitoring.alerts import send_alert
    n = len(measured)
    if n < MIN_FILLS:
        state = f"样本不足({n}/{MIN_FILLS})"
    else:
        eq = side_eq_of(measured)
        state = f"切C({eq:.1f}bp>18)" if eq > MEASURED_THRESHOLD_BP else f"维持A({eq:.1f}bp)"

    state_file = LOGS_DIR / "cost_verdict_state.json"
    prev = ""
    if state_file.exists():
        try:
            prev = json.loads(state_file.read_text(encoding="utf-8")).get("state", "")
        except Exception:
            pass
    if state != prev:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(
            {"state": state, "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
            indent=2), encoding="utf-8")
        send_alert(f"[成本实测裁决] 状态变更: {prev or '无'} → {state}\n"
                   f"规则(§6.8 ④): 策略单累计≥{MIN_FILLS}笔后, 单边等效>18bp切C, ≤18bp维持A")


# ── 输出 ─────────────────────────────────────────────────────────
def print_report(exec_days, df, measured):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"\n{'='*72}\n静跑期成本实测报表  {now}\n{'='*72}")

    # 数据覆盖
    dump_days = sorted(df["date"].unique()) if len(df) else []
    print(f"\n[数据覆盖] 订单文件日: {', '.join(dump_days) or '无'}"
          f" | 执行记录日: {', '.join(sorted(exec_days)) or '无'}")
    if dump_days:
        gaps = sorted(set(exec_days) - set(dump_days))
        if gaps:
            print(f"  ⚠️ 以下日期有执行记录但无订单文件(MA10/卖出单不可见): {', '.join(gaps)}")

    if not len(df):
        print("\n尚无订单数据 —— 待周一 15:55 首拉(或隧道恢复后手动 --pull)")
        return

    # 逐日明细
    print(f"\n[逐日明细]")
    print(f"{'日期':<10}{'调仓买':>7}{'调仓卖':>7}{'MA10卖':>7}{'做T':>6}{'其他':>6}{'策略单边bp':>11}")
    for d, g in df.groupby("date"):
        side_eq = side_eq_of(g[g["tag"].isin(["调仓", "MA10"]) & g["slip_bp"].notna()])
        def cnt(tag, side=None):
            q = g[g["tag"] == tag]
            return len(q) if side is None else len(q[q["side"] == side])
        eq_s = f"{side_eq:>10.1f}" if side_eq is not None else f"{'  -':>11}"
        print(f"{d:<10}{cnt('调仓','buy'):>7}{cnt('调仓','sell'):>7}{cnt('MA10','sell'):>7}"
              f"{cnt('做T'):>6}{cnt('其他'):>6}{eq_s}")

    # 分侧/分型
    print(f"\n[成本分解(单边等效 bp, 成交额加权)]")
    cuts = [("买入侧", df[df["side"] == "buy"]),
            ("卖出侧", df[df["side"] == "sell"]),
            ("  调仓单", df[df["tag"] == "调仓"]),
            ("  MA10单", df[df["tag"] == "MA10"]),
            ("  做T单(不计入裁决)", df[df["tag"] == "做T"]),
            ("  CB/其他(不计入裁决)", df[~df["tag"].isin(["调仓", "MA10", "做T"])])]
    for label, sub in cuts:
        m = sub[sub["slip_bp"].notna()]
        if not len(m):
            print(f"  {label}: 无有效参考价样本")
            continue
        slip = (m["slip_bp"] * m["notional"]).sum() / m["notional"].sum()
        fees = (m["fees_bp"] * m["notional"]).sum() / m["notional"].sum()
        tot = side_eq_of(m)
        print(f"  {label}: 滑点{slip:+.1f} + 费用{fees:.1f} = {tot:.1f} bp"
              f"  ({len(m)}笔)")

    # 裁决
    print(f"\n[18bp 裁决]")
    n_total = len(measured)
    if n_total < MIN_FILLS:
        print(f"  ⏳ 样本不足: 策略单 {n_total}/{MIN_FILLS} 笔 → 顺延, 不裁决")
    else:
        side_eq = side_eq_of(measured)
        print(f"  策略单累计 {n_total} 笔, 实测单边等效 = {side_eq:.1f} bp")
        if side_eq > MEASURED_THRESHOLD_BP:
            print(f"  🔴 {side_eq:.1f} > 18bp → A 严格劣于 C, 按 §6.8 ④ 切 C")
        else:
            print(f"  🟢 {side_eq:.1f} ≤ 18bp → 维持 A, 配置不动")
        print(f"  对照(年化成本 = 单边等效 × 换手): A(903%) {side_eq * A_TURNOVER / 100:.2f}%/年"
              f" vs C(349%) {side_eq * C_TURNOVER / 100:.2f}%/年")

    # 防御性提示
    ma10_n = len(df[df["tag"] == "MA10"])
    if not ma10_n and len(df[df["tag"] == "调仓"]):
        print("\n  ⚠️ MA10 触发单计数为 0: 若近期确实有 MA10 出清, 说明引擎订单"
              "不在 QMT_ACCOUNT_ID 账户下, 需查引擎账户归属(否则卖出侧成本被低估)")
    no_ref = df[df["slip_bp"].isna()]
    if len(no_ref):
        print(f"\n  ⚠️ {len(no_ref)} 笔无当日收盘参考价(数据未更新?), 未计入成本")
    print()


def main():
    parser = argparse.ArgumentParser(description="静跑期成本实测(§6.8 18bp 裁决)")
    parser.add_argument("--pull", action="store_true", help="远程驱动Windows导出并拉取当日订单")
    parser.add_argument("--report", action="store_true", help="输出成本报表与裁决")
    args = parser.parse_args()

    if args.pull:
        pull_orders()
    if args.report or not args.pull:
        exec_days, df, measured = build_report()
        print_report(exec_days, df, measured)
        if len(df):
            _verdict_alert(measured)


if __name__ == "__main__":
    main()
