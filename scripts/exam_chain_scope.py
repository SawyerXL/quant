"""考卷链范围清单(2026-10-12 用户裁定: 范围由程序算, 不由人判断)。

范围 = 入口 ∪ 入口递归导入的本仓库模块 ∪ 它们读取的配置文件
      ∪ cron/计划任务里写出考卷链读取文件的脚本。
入口: cb_signal / build_cb_data / fetch_and_execute / trader / 客户端 / run_execute.bat
用法: python scripts/exam_chain_scope.py [--json logs/exam_chain_scope.json]
冻结规则(用户裁定): 10-19 收盘前范围内文件不得提交 main, 改走 feature 分支;
10-19 开盘前 git diff 基线..HEAD 与范围清单取交集, 非空即停升级。
"""
import ast
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent
REPO_DIRS = {"scripts", "execution", "data", "factors", "monitoring", "strategies", "web"}

ENTRIES = [
    "scripts/cb_signal.py",
    "scripts/build_cb_data.py",
    "scripts/fetch_and_execute.py",
    "execution/trader.py",
    "execution/qmt_client.py",
    "scripts/run_execute.bat",
]
# 考卷链读取的数据路径(谁写这些路径=数据写手, 纳入范围)
CHAIN_READ_DIRS = [
    "data_store/meta",
    "data_store/convertible_bonds",
]
# 链读取的信号文件(cron 写手判定用)
CHAIN_READ_FILES = [
    "data_store/meta/signal_cb_latest.json",
    "data_store/meta/signal_a_latest.json",
]


def module_to_file(mod: str) -> Path | None:
    parts = mod.split(".")
    # 裸模块(scripts/ 下平铺脚本互 import 的形态): scripts/{mod}.py
    bare = REPO / "scripts" / (mod + ".py")
    if bare.exists():
        return bare
    if not parts or parts[0] not in REPO_DIRS:
        return None
    f = REPO.joinpath(*parts).with_suffix(".py")
    if f.exists():
        return f
    f2 = REPO.joinpath(*parts, "__init__.py")
    return f2 if f2.exists() else None


def imports_of(src: str) -> set[str]:
    """ast 提取 import 模块名(含 from a.b import c 的 a.b 与 a)。"""
    out = set()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return out
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                out.add(a.name)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.add(node.module)
    return out


def closure(entries: list[str]) -> set[Path]:
    seen: set[Path] = set()
    todo = [REPO / e for e in entries if (REPO / e).exists()]
    while todo:
        f = todo.pop()
        if f in seen:
            continue
        seen.add(f)
        for mod in imports_of(f.read_text(errors="ignore")):
            mf = module_to_file(mod)
            if mf and mf not in seen:
                todo.append(mf)
    return seen


def main():
    mods = closure(ENTRIES)
    # 配置文件: 闭包内代码引用 config/settings 与 .env
    configs: set[str] = set()
    for f in mods:
        src = f.read_text(errors="ignore")
        if re.search(r"config\.settings|from config|import config", src):
            configs.add("config/settings.py")
        if re.search(r"load_dotenv|\.env|os\.getenv|environ", src):
            configs.add(".env(不入库, 内容在服务器)")
    # cron 写手: 活 crontab 里向链读路径写数据的脚本
    crontab_txt = ""
    spool = Path("/var/spool/cron/crontabs/root")
    if spool.exists():
        crontab_txt = spool.read_text(errors="ignore")
    writers: set[str] = set()
    for line in crontab_txt.splitlines():
        m = re.search(r"python(?:3)?\s+(scripts/[\w/]+\.py|[\w./-]+\.py)", line)
        if not m:
            continue
        script = m.group(1)
        p = REPO / script if script.startswith("scripts/") else REPO / script
        if not p.exists():
            continue
        src = p.read_text(errors="ignore")
        # 写手判定: 有写调用 且 写入路径属于链读取的数据目录/信号文件
        writes = bool(re.search(r"save_meta|save_daily|to_parquet|write_text", src))
        touches_chain = bool(re.search(
            r"data_store/meta|data_store/convertible_bonds|signal_cb_latest|signal_a_latest",
            src))
        if writes and touches_chain:
            writers.add(str(p.relative_to(REPO)))
    scope = {
        "entries": ENTRIES,
        "modules": sorted(str(p.relative_to(REPO)) for p in mods),
        "configs": sorted(configs),
        "cron_data_writers": sorted(writers),
    }
    print(json.dumps(scope, ensure_ascii=False, indent=1))
    out = REPO / "logs/exam_chain_scope.json"
    out.write_text(json.dumps(scope, ensure_ascii=False, indent=1))
    print(f"\n已保存 {out}")
    # 命中检查接口: --check <file> 输出该文件是否在范围
    if "--check" in sys.argv:
        i = sys.argv.index("--check")
        target = sys.argv[i + 1]
        in_scope = (target in scope["modules"] or target in scope["entries"]
                    or target in scope["cron_data_writers"]
                    or target in scope["configs"])
        print(f"{target}: {'在范围内→停升级' if in_scope else '范围外→不触发, 记账即可'}")
    # 窗口批量分类(10-16 定稿用): --window <base> → git diff base..HEAD 逐文件分类
    if "--window" in sys.argv:
        import subprocess
        i = sys.argv.index("--window")
        base = sys.argv[i + 1]
        r = subprocess.run(["git", "diff", "--name-only", f"{base}..HEAD"],
                           capture_output=True, text=True, cwd=REPO)
        if r.returncode != 0:
            print(f"git diff 失败: {r.stderr}")
            return
        in_scope, out_scope = [], []
        for f in r.stdout.splitlines():
            if not f:
                continue
            hit = (f in scope["modules"] or f in scope["entries"]
                   or f in scope["cron_data_writers"] or f in scope["configs"])
            (in_scope if hit else out_scope).append(f)
        print(f"窗口 {base}..HEAD: 共 {len(in_scope) + len(out_scope)} 个文件变更")
        print(f"\n在范围内({len(in_scope)})——交集非空即停升级:")
        for f in in_scope:
            print(f"  ⚠ {f}")
        print(f"\n范围外({len(out_scope)})——不触发, 记账即可:")
        for f in out_scope:
            print(f"  ✓ {f}")


if __name__ == "__main__":
    main()
