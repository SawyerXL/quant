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


def execute(track: str = "a", dry_run: bool = False):
    """拉取信号并执行调仓。"""
    logger.info("=" * 60)
    logger.info(f"Track {track.upper()} 信号执行  dry_run={dry_run}")
    logger.info("=" * 60)

    # 1. 拉取信号
    signal = fetch_signal_from_linux(track)
    if signal is None:
        send_alert(f"[执行失败] Track {track.upper()} 信号拉取失败", level="error")
        return

    # 2. 检查信号新鲜度
    if not check_signal_fresh(signal):
        return

    # 3. 大势过滤检查
    regime = signal.get("regime", "bull")
    if regime == "bear":
        logger.warning("大势过滤：熊市信号，仅执行清仓操作")

    # 4. 打印交易计划
    holdings = signal.get("holdings", [])
    buy_list = signal.get("buy",      [])
    sell_list = signal.get("sell",    [])
    shares    = signal.get("shares",  {})
    prices    = signal.get("prices",  {})

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

        sig_file = ROOT / f"data_store/meta/signal_{track}_latest.json"
        result   = trader.execute_signal(sig_file, strategy_id=f"track_{track}")

        logger.info(f"执行完成: {result}")
        send_alert(
            f"[Track {track.upper()} 执行完成] {date.today()}\n"
            f"买入: {len(buy_list)} 只  卖出: {len(sell_list)} 只\n"
            f"详情: {result}"
        )
    except Exception as e:
        logger.error(f"执行失败: {e}")
        send_alert(f"[执行失败] Track {track.upper()}: {e}", level="error")


def main():
    parser = argparse.ArgumentParser(description="从 Linux 拉取信号并通过 QMT 执行")
    parser.add_argument("--track",   default="a", choices=["a", "b"], help="执行哪个策略")
    parser.add_argument("--dry-run", action="store_true", help="仅打印，不真正下单")
    args = parser.parse_args()

    execute(track=args.track, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
