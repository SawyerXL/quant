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
# Windows 上必须用装了 xtquant 的那个解释器全路径; 裸 python 在非交互 ssh
# 里不是它 → 导出静默失败(与 qmt_snapshot.py 同一教训)
WIN_PY = r"C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe"

LOGS_DIR      = ROOT / "logs"
ORDERS_PREFIX = "qmt_orders_"
EXEC_PREFIX   = "execution_result_"
T0_LOG        = ROOT / "config" / "t_settle_log.csv"

MEASURED_THRESHOLD_BP = 18.0   # §6.8 钉死: 实测单边等效交叉点
MIN_FILLS             = 100    # §6.8 钉死: 裁决所需最少成交笔数
A_TURNOVER            = 9.03   # 完整栈年换手 903%
C_TURNOVER            = 3.49   # 完整栈-MA10 年换手 349%

from data.storage import INDEX_CODES as INDEX_SHADOW_CODES

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

    # 直接 ssh 传全路径解释器(与 qmt_snapshot.trigger_export 同款, 避开
    # powershell 嵌套引号搅碎问题); 脚本内部 ROOT 由 __file__ 解析, 不依赖 cwd
    export_cmd = f'"{WIN_PY}" H:\\quant\\scripts\\dump_qmt_orders.py'
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
    _archive_signals(signal_date)
    return True


SIGNAL_ARCHIVE_DIR = LOGS_DIR / "signal_archive"


def _archive_signals(signal_date: str):
    """归档当日分组信号(供 bear_clear 标签判定)。

    信号文件每天 14:25 被覆盖, 而 bear_clear 判定依赖当日 regime——
    必须在拉取订单的当日把信号存底(用户 2026-09-07 定: 集中清仓滑点
    是 regime 事件, 不得混入 MA10 成本样本)。
    """
    d = signal_date.replace("-", "")
    archived = 0
    for g in ("g0", "g1"):
        src = Path("data_store/meta") / f"signal_a_{g}.json"
        if not src.exists():
            continue
        try:
            sig = json.loads(src.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(sig.get("signal_date", "")).replace("-", "") != d:
            continue  # 信号非当日, 不归档(防把旧信号贴到新日期上)
        dst = SIGNAL_ARCHIVE_DIR / f"{d}_{g}.json"
        SIGNAL_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        dst.write_text(json.dumps(sig, ensure_ascii=False, indent=2), encoding="utf-8")
        archived += 1
    if archived:
        logger.info(f"信号归档: {d} g0/g1 ×{archived}")


# ── 数据装载 ─────────────────────────────────────────────────────
def load_bear_clear_days() -> set:
    """从信号归档判定 regime 清仓日(bear_clear)。

    bear_clear 是 regime 集中清仓事件(如 9/7 32只全清), 与 MA10 逐笔
    触发结构不同: 单日样本 + 当日市场漂移混入滑点——入 100 笔池会拉偏
    18bp 裁决线(且偏向切C)。单独归档, 不计入裁决(2026-09-07 用户定)。
    """
    days = set()
    if not SIGNAL_ARCHIVE_DIR.exists():
        return days
    for f in SIGNAL_ARCHIVE_DIR.glob("*_g0.json"):
        try:
            sig = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(sig.get("regime", "")).lower() == "bear":
            d = str(sig.get("signal_date", "")).replace("-", "")
            if d:
                days.add(d)
    return days


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
    # 000001/000688/000905/000906 的 parquet 被指数数据占用(INDEX_CODES 约定),
    # 个股单读到的是指数点位——宁可无参考价剔除, 不可算出垃圾滑点
    if code in INDEX_SHADOW_CODES:
        return None
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
    bear_days = load_bear_clear_days()
    t0_legs = load_t0_legs()
    fills = load_order_fills()

    rows = []
    for fl in fills:
        tag = "其他"
        if _is_stock(fl["code"]):
            tag = "调仓" if fl["date"] in exec_days else "MA10"
            if fl["date"] in bear_days and tag == "调仓":
                tag = "bear_clear"  # regime集中清仓, 独立归档不入裁决池
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
        # 状态串不含计数: 否则每天+几笔也算"变更", 样本期天天告警
        state = "样本不足"
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
def _archive_snapshot(df, measured) -> Path | None:
    """成本明细存档——样本期盲盒纪律(§6.8): 报表不显示中间值, 数据不丢。"""
    if not len(df):
        return None
    rows = df[["date", "code", "side", "price", "shares", "tag", "ref",
               "slip_bp", "fees_bp", "cost_bp", "notional"]].copy()
    for c in ("slip_bp", "fees_bp", "cost_bp"):
        rows[c] = rows[c].round(2)
    cuts = {}
    for label, sub in [("buy", df[df["side"] == "buy"]),
                       ("sell", df[df["side"] == "sell"]),
                       ("rebalance", df[df["tag"] == "调仓"]),
                       ("ma10", df[df["tag"] == "MA10"]),
                       ("bear_clear", df[df["tag"] == "bear_clear"]),
                       ("t0", df[df["tag"] == "做T"])]:
        m = sub[sub["slip_bp"].notna()]
        if len(m):
            cuts[label] = {"n": int(len(m)), "side_eq_bp": round(side_eq_of(m), 2)}
    payload = {"generated_at": datetime.now().isoformat(),
               "n_strategy_measured": int(len(measured)),
               "min_fills": MIN_FILLS,
               "cuts": cuts,
               "fills": rows.to_dict(orient="records")}
    out = LOGS_DIR / f"cost_snapshot_{datetime.now().strftime('%Y%m%d')}.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def _dir_stats(sub):
    """卖出侧成交价 vs 收盘价的方向分布: (中位滑点bp, 低于收盘成交占比%)。

    卖出滑点>0 = 成交价低于收盘价 = 比回测(收盘价成交)卖得差。系统性为正
    说明回测收盘价假设偏乐观, 直接影响 A vs C 裁决的证据权重。
    """
    m = sub[(sub["side"] == "sell") & sub["slip_bp"].notna()]
    if not len(m):
        return None
    return float(m["slip_bp"].median()), float((m["slip_bp"] > 0).mean() * 100)


def _t0_anomaly_alert(df):
    """做T标签=异常检测器: t0 开关关闭期间(9/23判定前)匹配到非零,
    说明有非预期成交——不只分类, 即告警。"""
    from monitoring.alerts import send_alert
    t0 = df[df["tag"] == "做T"]
    if not len(t0):
        return
    days = sorted(t0["date"].unique())
    send_alert(f"做T标签非零: {len(t0)}笔疑似做T成交(日:{','.join(days)})"
               f" — t0开关应为关(9/23判定前), 查是否有非预期成交", level="warning")


def print_report(exec_days, df, measured):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    blind = len(measured) < MIN_FILLS  # 样本期盲盒: 中途瞄18bp线=锚定偏差(§6.8 纪律)
    snap = _archive_snapshot(df, measured) if len(df) else None
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

    # 逐日明细(计数永远可见; bp 列样本期盲盒)
    print(f"\n[逐日明细]")
    print(f"{'日期':<10}{'调仓买':>7}{'调仓卖':>7}{'MA10卖':>7}{'BC卖':>6}{'做T':>6}{'其他':>6}{'策略单边bp':>11}")
    for d, g in df.groupby("date"):
        def cnt(tag, side=None):
            q = g[g["tag"] == tag]
            return len(q) if side is None else len(q[q["side"] == side])
        eq_s = f"{'  盲盒':>11}" if blind else (
            f"{side_eq_of(g[g['tag'].isin(['调仓', 'MA10']) & g['slip_bp'].notna()]):>10.1f}"
            if len(g[g["tag"].isin(["调仓", "MA10"]) & g["slip_bp"].notna()]) else f"{'  -':>11}")
        print(f"{d:<10}{cnt('调仓','buy'):>7}{cnt('调仓','sell'):>7}{cnt('MA10','sell'):>7}"
              f"{cnt('bear_clear','sell'):>6}{cnt('做T'):>6}{cnt('其他'):>6}{eq_s}")

    # 分侧/分型
    print(f"\n[成本分解(单边等效 bp, 成交额加权)]")
    if blind:
        print(f"  样本期盲盒: 明细已存档 {snap.name if snap else '无'},"
              f" 累够 {MIN_FILLS} 笔后统一打开(§6.8 纪律: 中途瞄线=锚定偏差)")
        print(f"  当前进度: 策略单 {len(measured)}/{MIN_FILLS} 笔")
    else:
        cuts = [("买入侧", df[df["side"] == "buy"]),
                ("卖出侧", df[df["side"] == "sell"]),
                ("  调仓单", df[df["tag"] == "调仓"]),
                ("  MA10单", df[df["tag"] == "MA10"]),
                ("  bear_clear单(不计入裁决)", df[df["tag"] == "bear_clear"]),
                ("  做T单(不计入裁决)", df[df["tag"] == "做T"]),
                ("  CB/其他(不计入裁决)", df[~df["tag"].isin(["调仓", "MA10", "bear_clear", "做T"])])]
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
        # 卖出侧方向分布: 回测收盘价假设的偏差探测器
        print(f"\n[卖出侧方向分布(成交价 vs 收盘价; 滑点>0=卖得比收盘低)]")
        for label, sub in [("调仓卖", df[df["tag"] == "调仓"]),
                           ("MA10卖", df[df["tag"] == "MA10"]),
                           ("清仓卖", df[df["tag"] == "bear_clear"])]:
            st = _dir_stats(sub)
            if st is None:
                print(f"  {label}: 无卖出样本")
                continue
            med, below = st
            print(f"  {label}: 中位{med:+.1f}bp, 低于收盘成交占比 {below:.0f}%"
                  f"  {'⚠️系统性偏负, 回测收盘价假设偏乐观' if med > 3 else ''}")

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
            _t0_anomaly_alert(df)


if __name__ == "__main__":
    main()
