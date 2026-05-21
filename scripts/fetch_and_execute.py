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
from datetime import date
from pathlib import Path

# 确保项目根目录在 sys.path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from loguru import logger
from monitoring.alerts import send_alert

logger.add("logs/execute_{time:YYYY-MM-DD}.log", rotation="1 day", retention="30 days")

LINUX_SERVER  = os.getenv("LINUX_SERVER", "47.116.166.139")
LINUX_USER    = os.getenv("LINUX_USER",   "root")
SSH_KEY       = os.getenv("SSH_KEY", "")
SIGNAL_DIR    = "data_store/meta"


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

    # 1. 拉取信号
    signal = fetch_signal_from_linux(track)
    if signal is None:
        send_alert(f"[执行失败] Track {track.upper()} 信号拉取失败", level="error")
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
    try:
        from execution.trader import Trader
        trader = Trader()

        if setup:
            # 建仓模式：用信号预计算好的股数下单，不用 rebalance（避免按账户总资产重算）
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
            sig_file = ROOT / f"data_store/meta/signal_{track}_latest.json"
            result   = trader.execute_signal(sig_file, strategy_id=f"track_{track}")

        logger.info(f"执行完成: {result}")
        send_alert(
            f"[Track {track.upper()} {'建仓' if setup else '调仓'}完成] {date.today()}\n"
            f"买入: {len(buy_list)} 只  卖出: {len(sell_list)} 只\n"
            f"信号日期: {signal.get('signal_date')}"
        )
    except Exception as e:
        logger.error(f"执行失败: {e}")
        send_alert(f"[执行失败] Track {track.upper()}: {e}", level="error")


def main():
    parser = argparse.ArgumentParser(description="从 Linux 拉取信号并通过 QMT 执行")
    parser.add_argument("--track",   default="a", choices=["a", "b"], help="执行哪个策略")
    parser.add_argument("--dry-run", action="store_true", help="仅打印，不真正下单")
    parser.add_argument("--setup",   action="store_true",
                        help="建仓初始化：跳过日期检查，全量买入holdings（首次使用）")
    args = parser.parse_args()

    execute(track=args.track, dry_run=args.dry_run, setup=args.setup)


if __name__ == "__main__":
    main()
