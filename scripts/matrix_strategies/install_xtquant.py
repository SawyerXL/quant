import sys, os, subprocess

mpython = sys.path[0]
log_file = os.path.join(mpython, "pip_log.txt")

print("Python:", sys.executable)
print("mpython:", mpython)

# 用文件接收输出（pythonw.exe 没有控制台，不能用 PIPE）
with open(log_file, 'w') as f:
    proc = subprocess.Popen(
        [sys.executable, '-m', 'pip', 'install', 'xtquant',
         '--target', mpython, '--no-warn-script-location'],
        stdout=f, stderr=f
    )
    proc.wait(timeout=120)
    print("pip returncode:", proc.returncode)

# 打印日志
with open(log_file, 'r', errors='ignore') as f:
    content = f.read()
print("pip output:", content[:800])

# 检查结果
xt_path = os.path.join(mpython, 'xtquant')
if os.path.exists(xt_path):
    print("SUCCESS: xtquant installed to", xt_path)
    import xtquant
    print("import OK:", dir(xtquant)[:8])
else:
    print("FAILED: xtquant not in mpython")
    print("mpython contents:", [f for f in os.listdir(mpython) if not f.endswith('.py')][:10])
