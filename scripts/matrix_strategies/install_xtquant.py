import sys, os

mpython = sys.path[0]
print("mpython:", mpython)

# 直接调用 pip 内部 API，不启动子进程（避免 pythonw.exe 句柄问题）
try:
    from pip._internal import main as pip_main
    ret = pip_main(['install', 'xtquant', '--target', mpython,
                    '--no-warn-script-location', '-q'])
    print("pip returned:", ret)
except ImportError:
    try:
        from pip._internal.cli.main import main as pip_main
        ret = pip_main(['install', 'xtquant', '--target', mpython,
                        '--no-warn-script-location', '-q'])
        print("pip (new API) returned:", ret)
    except Exception as e2:
        print("pip import failed:", e2)

# 检查结果
xt_path = os.path.join(mpython, 'xtquant')
if os.path.exists(xt_path):
    print("SUCCESS: xtquant installed!")
    sys.path.insert(0, mpython)
    import xtquant
    print("import OK:", dir(xtquant)[:5])
else:
    print("FAILED: xtquant not found")
    print("mpython files:", [f for f in os.listdir(mpython)
                             if not f.endswith('.py')][:15])
