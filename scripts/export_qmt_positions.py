"""
Windows 端运行：导出 QMT 实际持仓，推送到 Linux 供对账使用。

用法（在 Windows QMT 服务器上运行）：
    python scripts/export_qmt_positions.py
    python scripts/export_qmt_positions.py --no-push   # 只导出不推送

结果文件：
    本地：logs/qmt_positions_latest.json
    推送：Linux:/root/quant/logs/qmt_positions_latest.json
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from loguru import logger

LINUX_SERVER = os.getenv("LINUX_SERVER", "")
LINUX_USER   = os.getenv("LINUX_USER", "root")
SSH_KEY      = os.getenv("SSH_KEY", "")
OUT_FILE     = ROOT / "logs" / "qmt_positions_latest.json"


def export_positions() -> dict:
    """连接 QMT 导出当前持仓和账户信息。"""
    from execution.qmt_client import get_client

    os.environ.setdefault("ENV", "simulation")   # 强制走真实QMT
    client = get_client()

    positions = client.get_positions()
    account   = client.get_account_info()
    orders    = client.get_today_orders()

    result = {
        "exported_at":  datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "account": {
            "total_assets": account.get("total_assets", 0),
            "cash":         account.get("cash", 0),
            "market_value": account.get("market_value", 0),
        },
        "positions": {
            # 去掉交易所后缀，只保留6位代码
            code.split(".")[0]: {
                "volume":       pos["volume"],
                "cost_price":   pos["cost_price"],
                "market_value": pos["market_value"],
            }
            for code, pos in positions.items()
            if pos.get("volume", 0) > 0
        },
        "today_orders": orders,
    }

    logger.info(f"持仓: {len(result['positions'])} 只  "
                f"总资产: {account.get('total_assets', 0):,.0f}  "
                f"今日委托: {len(orders)} 笔")
    return result


def push_to_linux(local_file: Path) -> bool:
    if not LINUX_SERVER:
        logger.warning("LINUX_SERVER 未配置，跳过推送")
        return False

    ssh_opts = ["-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10"]
    if SSH_KEY:
        ssh_opts += ["-i", SSH_KEY]

    remote = f"{LINUX_USER}@{LINUX_SERVER}:/root/quant/logs/{local_file.name}"
    cmd = ["scp"] + ssh_opts + [str(local_file), remote]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            logger.info(f"✅ 推送成功 → {remote}")
            return True
        logger.error(f"推送失败: {r.stderr.strip()}")
        return False
    except Exception as e:
        logger.error(f"推送出错: {e}")
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-push", action="store_true", help="只导出，不推送到Linux")
    args = parser.parse_args()

    logger.info("=== QMT 持仓导出 ===")
    data = export_positions()

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"已保存: {OUT_FILE}")

    if not args.no_push:
        push_to_linux(OUT_FILE)
    else:
        logger.info("--no-push 模式，跳过推送。请手动复制文件到 Linux:logs/qmt_positions_latest.json")

    # 打印摘要
    print("\n" + "=" * 50)
    print(f"导出时间: {data['exported_at']}")
    print(f"总资产:   {data['account']['total_assets']:,.0f} 元")
    print(f"现金:     {data['account']['cash']:,.0f} 元")
    print(f"持仓市值: {data['account']['market_value']:,.0f} 元")
    print(f"持仓数:   {len(data['positions'])} 只")
    for code, pos in sorted(data["positions"].items()):
        print(f"  {code}  {pos['volume']}股  成本{pos['cost_price']:.2f}  "
              f"市值{pos['market_value']:,.0f}")
    print("=" * 50)


if __name__ == "__main__":
    main()
