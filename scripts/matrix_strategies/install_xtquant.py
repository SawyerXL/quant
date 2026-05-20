import sys, os

mpython = sys.path[0]
print("Python:", sys.version[:6])

# 1. 检查内置模块（可能包含终端注入的交易模块）
print("\n=== 内置模块 ===")
print(sys.builtin_module_names)

# 2. 检查全局变量（终端可能注入了交易函数）
print("\n=== 全局变量 ===")
g = [k for k in globals() if not k.startswith('_')]
print(g)

# 3. 用 urllib 从 PyPI 下载 xtquant wheel
print("\n=== 尝试下载 xtquant ===")
try:
    import urllib.request, json
    url = "https://pypi.org/pypi/xtquant/json"
    with urllib.request.urlopen(url, timeout=10) as r:
        data = json.loads(r.read())
    version = data['info']['version']
    print("PyPI xtquant 最新版本:", version)

    # 找 Python 3.6 Windows wheel
    files = data['releases'].get(version, [])
    wheel = None
    for f in files:
        fn = f['filename']
        if 'cp36' in fn and 'win' in fn and fn.endswith('.whl'):
            wheel = f
            break
    if not wheel:
        # 找通用 wheel
        for f in files:
            if f['filename'].endswith('.whl'):
                wheel = f
                break

    if wheel:
        print("下载:", wheel['filename'])
        wheel_path = os.path.join(mpython, wheel['filename'])
        urllib.request.urlretrieve(wheel['url'], wheel_path)
        print("下载完成，解压中...")

        # 解压 wheel（其实是 zip 文件）
        import zipfile
        with zipfile.ZipFile(wheel_path, 'r') as zf:
            zf.extractall(mpython)
        os.remove(wheel_path)
        print("解压完成")

        if os.path.exists(os.path.join(mpython, 'xtquant')):
            print("SUCCESS!")
            import xtquant
            print("xtquant OK:", dir(xtquant)[:5])
        else:
            print("FAILED: 解压后未找到 xtquant")
            print("mpython:", [f for f in os.listdir(mpython) if not f.endswith('.py')][:10])
    else:
        print("未找到合适的 wheel 文件")
        print("可用文件:", [f['filename'] for f in files[:5]])

except Exception as e:
    print("下载失败:", type(e).__name__, str(e))
