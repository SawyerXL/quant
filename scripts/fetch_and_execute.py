"""
Windows 端执行脚本：从 Linux 服务器拉取信号，经风控后通过 QMT 执行。

使用方式（每个调仓日 14:30 手动运行，或配置 Windows 任务计划）：
    python scripts/fetch_and_execute.py            # 执行 Track A 信号
    python scripts/fetch_and_execute.py --dry-run  # 仅打印，不真正下单
    python scripts/fetch_and_execute.py --track b  # 执行 Track B 信号

前提：
  1. QMT 已启动，独立交易已勾选
  2. .env 已正确配置（QMT_PATH、QMT_ACCOUNT_ID、LINUX_SERVER）
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

# 确保项目根目录在 sys.path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from loguru import logger
from monitoring.alerts import send_alert

logger.add("logs/execute_{time:YYYY-MM-DD}.log", rotation="1 day", retention="30 days")

LINUX_SERVER  = os.getenv("LINUX_SERVER", "106.15.61.81")  # 2026-09-02: 旧IP47.116.166.139已弃用
LINUX_USER    = os.getenv("LINUX_USER",   "root")
SSH_KEY       = os.getenv("SSH_KEY", "")
SIGNAL_DIR    = "data_store/meta"
EXEC_RESULT_DIR = ROOT / "logs"
FILL_WAIT_SECS  = 300  # 委托后等待成交确认的秒数 (原45s太短, 填单还没撮合就取数→fill_rate=0%)


def push_result_to_linux(result_file: Path) -> bool:
    """将执行结果 JSON 推回 Linux，供健康检查使用。"""
    ssh_opts = ["-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10"]
    if SSH_KEY:
        ssh_opts += ["-i", SSH_KEY]
    remote_path = f"{LINUX_USER}@{LINUX_SERVER}:/root/quant/logs/{result_file.name}"
    cmd = ["scp"] + ssh_opts + [str(result_file), remote_path]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            logger.info(f"执行结果已推送至 Linux: {result_file.name}")
            return True
        logger.warning(f"推送失败（不影响本次执行）: {r.stderr.strip()}")
        return False
    except Exception as e:
        logger.warning(f"推送结果出错（不影响本次执行）: {e}")
        return False


def fetch_group_signals() -> dict | None:
    """摊平模式(2026-09-02): 拉取两组信号并合并为账户级目标(100万)。

    合并语义: holdings=并集, shares=两组之和, sell/buy=并集——执行端仍按
    账户级幂等diff下单, 组的独立性只存在于信号生成端(各自日历+选股)。
    任一文件缺失/日期不新鲜 → 返回None(回退单信号路径)。
    """
    merged = {"holdings": [], "shares": {}, "prices": {},
              "sell": [], "buy": [], "position_ratio": 1.0}
    for g in ("g0", "g1"):
        remote_file = f"{SIGNAL_DIR}/signal_a_{g}.json"
        local_file = ROOT / f"data_store/meta/signal_a_{g}.json"
        local_file.parent.mkdir(parents=True, exist_ok=True)
        ssh_opts = ["-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10"]
        if SSH_KEY:
            ssh_opts += ["-i", SSH_KEY]
        cmd = ["scp"] + ssh_opts + [
            f"{LINUX_USER}@{LINUX_SERVER}:/root/quant/{remote_file}",
            str(local_file)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                logger.warning(f"拉取组信号 {g} 失败: {r.stderr.strip()[:150]}")
                return None
            sig = json.loads(local_file.read_text(encoding="utf-8"))
            if sig.get("signal_date") != merged.get("signal_date"):
                if "signal_date" not in merged:
                    merged["signal_date"] = sig.get("signal_date")
                    merged["regime"] = sig.get("regime", "bull")
                    merged["position_ratio"] = sig.get("position_ratio", 1.0)
                else:
                    logger.error(f"两组信号日期不一致: {merged['signal_date']} vs {sig.get('signal_date')}")
                    return None
            def _n(c):
                return str(c).split(".")[0]
            merged["holdings"] = sorted(
                set(merged["holdings"]) | {_n(c) for c in sig.get("holdings", [])})
            for c, s in sig.get("shares", {}).items():
                merged["shares"][_n(c)] = merged["shares"].get(_n(c), 0) + int(s)
            merged["prices"].update(
                {_n(k): v for k, v in sig.get("prices", {}).items()})
            merged["sell"] = sorted(set(merged["sell"]) | {_n(c) for c in sig.get("sell", [])})
            merged["buy"] = sorted(set(merged["buy"]) | {_n(c) for c in sig.get("buy", [])})
            merged["effective_capital"] = float(
                merged.get("effective_capital", 0)
                + float(sig.get("effective_capital") or sig.get("capital") or 0))
        except Exception as e:
            logger.warning(f"拉取组信号 {g} 异常: {e}")
            return None
    # 2026-09-02 组间卖出语义修正: 一组卖出但另一组仍持有的票, 合并后该票
    # 仍有目标shares → 必须从sell清单移除(执行器按sell清单会全额清仓,
    # 会误卖另一组的份额); 差量减仓由target_shares diff自然处理
    merged["sell"] = [c for c in merged["sell"]
                      if merged["shares"].get(c, 0) <= 0]
    logger.info(f"[摊平合并] {merged['signal_date']}: 持仓{len(merged['holdings'])}只 "
                f"资金{merged.get('effective_capital', 0):,.0f}")
    return merged


def fetch_signal_from_linux(track: str = "a") -> dict | None:
    """通过 SSH 从 Linux 服务器拉取最新信号文件。"""
    remote_file = f"{SIGNAL_DIR}/signal_{track}_latest.json"
    local_file  = ROOT / f"data_store/meta/signal_{track}_latest.json"
    local_file.parent.mkdir(parents=True, exist_ok=True)

    ssh_opts = ["-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10"]
    if SSH_KEY:
        ssh_opts += ["-i", SSH_KEY]

    cmd = ["scp"] + ssh_opts + [
        f"{LINUX_USER}@{LINUX_SERVER}:/root/quant/{remote_file}",
        str(local_file)
    ]

    logger.info(f"从 Linux 拉取 Track {track.upper()} 信号...")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            logger.error(f"SCP 失败: {result.stderr}")
            return None
        signal = json.loads(local_file.read_text(encoding="utf-8"))
        logger.info(f"信号拉取成功: {signal['signal_date']}，"
                    f"持仓 {len(signal.get('holdings', []))} 只，"
                    f"买入 {len(signal.get('buy', []))} 只，"
                    f"卖出 {len(signal.get('sell', []))} 只")
        return signal
    except Exception as e:
        logger.error(f"拉取信号失败: {e}")
        return None


def preflight(signal: dict, track: str) -> list[str]:
    """执行前检查(2026-09-01 用户要求: 执行链反复出问题, 必须预检再下单)。

    三查:
      ① 持仓域: CB信号必须全是转债代码(11/12/127), 股票信号必须无转债
         —— 防跨域误卖(9/1事故: CB执行器卖了600276)
      ② 价格新鲜度: 信号价格 vs 信号日期, 过期>3交易日告警
         —— 防月快照价事故(9/1 CB首日5/25成交)
      ③ 资金口径: 目标市值总额 vs 信号capital, 偏离>15%告警
    """
    issues = []
    cb_prefix = ("110", "111", "113", "118", "123", "127", "128")
    holdings = [str(c).split(".")[0] for c in signal.get("holdings", [])]
    for c in holdings:
        is_cb = c[:3] in cb_prefix
        if track == "cb" and not is_cb:
            issues.append(f"域违规: CB信号含股票 {c}")
        if track in ("a", "b") and is_cb:
            issues.append(f"域违规: 股票信号含转债 {c}")
    # 价格新鲜度(文档承诺的"过期>3交易日告警"实装: generated_at才是数据生成时点,
    # signal_date只是执行日标签; 2026-09-02 修复——8/28生成价在9/1被执行的漏洞)
    generated_at = signal.get("generated_at", "")
    if generated_at:
        try:
            gen_d = datetime.fromisoformat(generated_at).date()
            age = (date.today() - gen_d).days
            if age > 3:
                issues.append(f"价格过期: generated_at={gen_d} ({age}天前, >3交易日)")
        except Exception:
            pass
    # 资金口径(信号写的是effective_capital, 不是capital; 2026-09-02 修复)
    # 2026-09-02 摊平口径修正: 超配>20%才拦截(防错单/口径混用); 低配只告警
    # ——floor-to-lot跳票的现金拖累在摊平口径下天然有20~30%低配, 不是错误
    shares, prices = signal.get("shares", {}), signal.get("prices", {})
    total = sum(shares.get(c, 0) * prices.get(c, 0) for c in holdings)
    capital = float(signal.get("capital") or signal.get("effective_capital") or 0)
    if capital > 0:
        dev = total / capital - 1
        if dev > 0.20:
            issues.append(f"资金超配: 目标市值{total:,.0f} vs capital{capital:,.0f} ({dev*100:+.0f}%)")
        elif dev < -0.35:
            issues.append(f"资金低配异常: 目标市值{total:,.0f} vs capital{capital:,.0f} ({dev*100:+.0f}%, 超出lot跳票正常范围)")
    return issues


def check_signal_fresh(signal: dict) -> bool:
    """确认信号是今天生成的（防止误执行旧信号）。"""
    sig_date = signal.get("signal_date", "")
    today    = date.today().strftime("%Y-%m-%d")
    if sig_date != today:
        logger.warning(f"信号日期 {sig_date} ≠ 今天 {today}，跳过执行")
        return False
    return True


def execute(track: str = "a", dry_run: bool = False, setup: bool = False):
    """
    拉取信号并执行调仓。
    setup=True：建仓模式，跳过新鲜度检查，买入全部 holdings（适合首次初始化）。
    """
    mode = "建仓初始化" if setup else ("DRY-RUN预览" if dry_run else "正式执行")
    logger.info("=" * 60)
    logger.info(f"Track {track.upper()} 信号执行  模式={mode}")
    logger.info("=" * 60)

    # 1. 拉取信号（必须先判 None 再预检——SCP失败时preflight会AttributeError,
    #    原顺序让"信号拉取失败"告警变成死代码, 2026-09-02 修复）
    #    摊平模式(2026-09-02): track=a 优先拉两组信号合并, 缺失回退单信号
    signal = None
    merged_path = None
    if track == "a" and not setup:
        signal = fetch_group_signals()
        if signal is not None:
            merged_path = ROOT / "data_store/meta/signal_a_merged_tmp.json"
            merged_path.write_text(json.dumps(signal, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
    if signal is None:
        signal = fetch_signal_from_linux(track)
    if signal is None:
        send_alert(f"[执行失败] Track {track.upper()} 信号拉取失败", level="error")
        return
    issues = preflight(signal, track)
    if issues:
        for i in issues:
            logger.error(f"[预检失败] {i}")
        send_alert(f"[{track}] 执行前预检失败, 拒绝下单: {'; '.join(issues)}", level="error")
        return

    # 2. 检查信号新鲜度（--setup 模式跳过，用于初始建仓）
    if not setup and not check_signal_fresh(signal):
        return

    # 3. 大势过滤检查
    regime = signal.get("regime", "bull")
    if regime == "bear":
        logger.warning("大势过滤：熊市信号，仅执行清仓操作")

    # 4. 交易计划：setup模式=全量买入holdings，正常模式=买卖差量
    holdings  = signal.get("holdings", [])
    shares    = signal.get("shares",  {})
    prices    = signal.get("prices",  {})

    if setup:
        # 建仓模式：买入所有目标持仓（QMT账户从零开始）
        buy_list  = [c for c in holdings if shares.get(c, 0) > 0]
        sell_list = []
        logger.info(f"[建仓模式] 全量买入 {len(buy_list)} 只（跳过新鲜度检查）")
        logger.info(f"  信号日期: {signal.get('signal_date')}  仓位: {signal.get('position_ratio', 1.0):.0%}")
    else:
        buy_list  = signal.get("buy",  [])
        sell_list = signal.get("sell", [])

    logger.info(f"交易计划：买入 {len(buy_list)} 只，卖出 {len(sell_list)} 只")
    if sell_list:
        logger.info(f"  卖出：{sell_list}")
    if buy_list:
        logger.info(f"  买入：{buy_list}")

    if dry_run:
        logger.info("DRY RUN 模式：仅打印，不执行")
        for code in buy_list:
            p = prices.get(code, 0)
            s = shares.get(code, 0)
            logger.info(f"  [模拟] BUY  {code}  {s}股 @ {p:.2f}元")
        for code in sell_list:
            logger.info(f"  [模拟] SELL {code}  全部卖出")
        return

    # 5. 通过 Trader 执行（会走风控检查）
    exec_started_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    try:
        from execution.trader import Trader
        from execution.qmt_client import get_client

        trader = Trader()
        client = get_client()

        if setup:
            import json as _json
            setup_sig = dict(signal)
            setup_sig["buy"]  = [c for c in holdings if shares.get(c, 0) > 0]
            setup_sig["sell"] = []
            temp_path = ROOT / f"data_store/meta/signal_{track}_setup_tmp.json"
            temp_path.write_text(
                _json.dumps(setup_sig, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            result = trader.execute_signal(temp_path, strategy_id=f"track_{track}")
            temp_path.unlink(missing_ok=True)
        else:
            # 摊平模式: 执行合并信号(账户级目标100万); 否则单信号
            sig_file = (merged_path if merged_path is not None
                        else ROOT / f"data_store/meta/signal_{track}_latest.json")
            result   = trader.execute_signal(sig_file, strategy_id=f"track_{track}")

        logger.info(f"委托提交完成，等待 {FILL_WAIT_SECS}s 后采集成交价...")
        time.sleep(FILL_WAIT_SECS)

        # ── 采集实际成交数据 ──────────────────────────────────────
        exec_confirmed_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        actual_positions  = {}
        try:
            actual_positions = client.get_positions()
        except Exception as qe:
            logger.warning(f"获取持仓失败，跳过成交价记录: {qe}")

        # 废单检测(2026-09-03教训固化): status=57=柜台废单, 历史上多次
        # 静默发生(8/14×8、9/3×32), 现在执行后立即告警不再沉默
        try:
            rejected = [o for o in client.get_today_orders()
                        if o.get("status") == 57]
            if rejected:
                n_rej = sum(o.get("shares", 0) for o in rejected)
                codes = sorted({str(o.get("code", "")).split(".")[0]
                                for o in rejected})
                send_alert(
                    f"🔴 [{track}] {len(rejected)}笔委托被柜台废单(共{n_rej}股): "
                    f"{codes[:8]}{'...' if len(codes) > 8 else ''} —— 请查QMT"
                    f"废单原因(常见: 委托价不符合价位/可卖不足/资金不足)",
                    level="error")
                logger.warning(f"废单 {len(rejected)}笔: {codes[:10]}")
        except Exception as e:
            logger.warning(f"废单检测失败: {e}")

        assumed_prices = signal.get("prices", {})
        slippage_data  = []
        # key归一化: QMT positions带后缀('600176.SH'), 信号无后缀——
        # 旧版直接取交集恒空 → fill_rate恒0%, 掩盖真实成交失败(2026-09-02修复)
        actual_positions = {str(k).split(".")[0]: v
                            for k, v in actual_positions.items()}
        filled_codes   = set(actual_positions.keys())
        target_buys    = {str(c).split(".")[0] for c in buy_list}

        for code in target_buys:
            assumed = assumed_prices.get(code)
            actual  = actual_positions.get(code, {}).get("cost_price")
            if assumed and actual and assumed > 0:
                slip_pct = (actual - assumed) / assumed * 100
                slippage_data.append({
                    "code": code, "assumed": round(assumed, 4),
                    "actual": round(actual, 4), "slippage_pct": round(slip_pct, 4),
                })

        fill_count = len(target_buys & filled_codes)
        fill_rate  = fill_count / len(target_buys) * 100 if target_buys else 100.0
        avg_slip   = (sum(d["slippage_pct"] for d in slippage_data) / len(slippage_data)
                      if slippage_data else 0.0)
        max_slip   = (max(abs(d["slippage_pct"]) for d in slippage_data)
                      if slippage_data else 0.0)

        # 计算全链路时延（信号生成→成交确认）
        gen_at = signal.get("generated_at", "")
        try:
            gen_dt  = datetime.fromisoformat(gen_at)
            conf_dt = datetime.fromisoformat(exec_confirmed_at)
            latency_min = (conf_dt - gen_dt).total_seconds() / 60
        except Exception:
            latency_min = None

        exec_record = {
            "signal_date":        signal.get("signal_date"),
            "track":              track,
            "generated_at":       gen_at,
            "exec_started_at":    exec_started_at,
            "exec_confirmed_at":  exec_confirmed_at,
            "latency_min":        round(latency_min, 1) if latency_min is not None else None,
            "target_buy_count":   len(target_buys),
            "fill_count":         fill_count,
            "fill_rate_pct":      round(fill_rate, 1),
            "avg_slippage_pct":   round(avg_slip, 4),
            "max_slippage_pct":   round(max_slip, 4),
            "slippage_detail":    slippage_data,
            "blocked":            result.get("blocked", []),
        }

        # 保存到本地并推回 Linux（按track分文件: 旧版同日期名被cb覆盖, Track A
        # 执行记录丢失, 2026-09-02 修复）
        today_str   = date.today().strftime("%Y%m%d")
        result_file = EXEC_RESULT_DIR / f"execution_result_{today_str}_{track}.json"
        EXEC_RESULT_DIR.mkdir(parents=True, exist_ok=True)
        result_file.write_text(
            json.dumps(exec_record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info(f"执行结果已保存: {result_file}")
        logger.info(f"成交率: {fill_rate:.0f}%  平均滑点: {avg_slip:+.3f}%  时延: {latency_min:.1f}min" if latency_min else f"成交率: {fill_rate:.0f}%  平均滑点: {avg_slip:+.3f}%")
        push_result_to_linux(result_file)

        send_alert(
            f"[Track {track.upper()} {'建仓' if setup else '调仓'}完成] {date.today()}\n"
            f"买入: {len(buy_list)} 只  卖出: {len(sell_list)} 只\n"
            f"成交率: {fill_rate:.0f}%  平均滑点: {avg_slip:+.3f}%"
            + (f"  时延: {latency_min:.1f}min" if latency_min else "")
            + f"\n信号日期: {signal.get('signal_date')}"
        )
    except Exception as e:
        logger.error(f"执行失败: {e}")
        send_alert(f"[执行失败] Track {track.upper()}: {e}", level="error")


def backfill_fills():
    """
    收盘后补拉成交数据, 覆盖执行记录——调仓日14:30执行时成交未出(FILL_WAIT_SECS太短或
    执行崩溃导致fill_rate=0%), 15:45再跑一次取真实成交率/滑点。
    独立模式: python scripts/fetch_and_execute.py --backfill
    """
    import json as _json
    from pathlib import Path as _Path
    today_str = date.today().strftime("%Y%m%d")
    result_file = EXEC_RESULT_DIR / f"execution_result_{today_str}_a.json"
    logger.info(f"补拉今日成交: {today_str}")
    try:
        from execution.qmt_client import get_client as _gc
        c = _gc()
        orders = c.get_today_orders()
        if not orders:
            logger.warning("今日无委托记录, 跳过补拉"); return
        buys = [o for o in orders if o.get("direction") == "buy"]
        sells = [o for o in orders if o.get("direction") == "sell"]
        filled_buy = [o for o in buys if o.get("filled", 0) > 0]
        filled_sell = [o for o in sells if o.get("filled", 0) > 0]
        total_buy = len(buys)
        fill_rate = len(filled_buy) / total_buy * 100 if total_buy else 100
        # 读信号取参考价算滑点
        sig_path = _Path("data_store/meta/signal_a_latest.json")
        ref_prices = {}
        if sig_path.exists():
            sig = _json.loads(sig_path.read_text(encoding="utf-8"))
            ref_prices = sig.get("prices", {})
        slippages = []
        for o in filled_buy + filled_sell:
            code = o.get("code", "").split(".")[0]
            ref = ref_prices.get(code, 0)
            actual = float(o.get("price", 0))
            if ref > 0 and actual > 0:
                slippages.append({"code": code, "ref": round(ref, 2),
                                  "actual": round(actual, 2),
                                  "slip_pct": round((actual / ref - 1) * 100, 2)})
        avg_slip = round(sum(s["slip_pct"] for s in slippages) / len(slippages), 2) if slippages else 0
        max_slip = round(max(abs(s["slip_pct"]) for s in slippages), 2) if slippages else 0
        record = {"signal_date": today_str, "track": "a",
                  "exec_confirmed_at": datetime.now().isoformat(), "backfilled": True,
                  "target_buy": total_buy, "filled_buy": len(filled_buy),
                  "target_sell": len(sells), "filled_sell": len(filled_sell),
                  "fill_rate_pct": round(fill_rate, 1),
                  "avg_slippage_pct": avg_slip, "max_slippage_pct": max_slip,
                  "slippage_detail": slippages}
        result_file.parent.mkdir(parents=True, exist_ok=True)
        result_file.write_text(_json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"补拉完成: fill_rate={fill_rate:.0f}% avg_slip={avg_slip:+.2f}% → {result_file}")
        push_result_to_linux(result_file)
    except Exception as e:
        logger.error(f"补拉失败: {e}")


def main():
    parser = argparse.ArgumentParser(description="从 Linux 拉取信号并通过 QMT 执行")
    parser.add_argument("--track",   default="a", choices=["a", "b", "cb"], help="执行哪个策略")
    parser.add_argument("--dry-run", action="store_true", help="仅打印，不真正下单")
    parser.add_argument("--setup",   action="store_true",
                        help="建仓初始化：跳过日期检查，全量买入holdings（首次使用）")
    parser.add_argument("--backfill", action="store_true",
                        help="收盘后补拉成交数据，覆盖执行记录(fill_rate/滑点)")
    args = parser.parse_args()

    if args.backfill:
        backfill_fills()
    else:
        execute(track=args.track, dry_run=args.dry_run, setup=args.setup)


if __name__ == "__main__":
    main()
