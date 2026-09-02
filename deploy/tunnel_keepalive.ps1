# Tunnel keepalive v2 (2026-09-02): 端到端验证 + 半开自愈。
# v1 缺陷: 只查ssh.exe进程存在即exit 0 —— 进程活着但Linux侧会话已死的
# "半开隧道"永远不自愈, 这是"隧道一直不稳定"的根因。
# v2: 每次先走完整链路验证(Windows→Linux→经2222反向隧道回Windows),
#     链路不通才杀旧进程重启; 杀掉的是半开/僵尸进程而非正常隧道。
$ErrorActionPreference = "Continue"
$log  = "H:\quant\logs\tunnel_keepalive.log"
$SSH  = "C:\Windows\System32\OpenSSH\ssh.exe"
$KEY  = "C:\Users\Administrator\.ssh\id_ed25519"
$LINUX = "root@106.15.61.81"   # Linux服务器公网IP(变更时改这一处)
$LINUX_KEY = "~/.ssh/id_rsa"   # Linux侧连接Windows用的key

function Test-Tunnel {
    # 经主SSH到Linux, 由Linux侧探针脚本经2222反向隧道连回本机 —— 验证完整链路。
    # 探针独立成脚本: PowerShell→ssh 嵌套引号传参会被搅碎(v2.1修复)
    $out = & $SSH -i $KEY -o BatchMode=yes -o ConnectTimeout=12 `
        -o StrictHostKeyChecking=no $LINUX "/root/quant/scripts/tunnel_probe.sh" 2>$null
    return ($out -join " ") -match "TUNNEL_OK"
}

function Start-Tunnel {
    Start-Process -WindowStyle Hidden -FilePath $SSH `
        -ArgumentList '-R','2222:localhost:22','-N',$LINUX,
            '-o','ServerAliveInterval=15','-o','ServerAliveCountMax=4',
            '-o','TCPKeepAlive=yes','-o','ExitOnForwardFailure=yes',
            '-o','StrictHostKeyChecking=no','-i',$KEY
}

if (Test-Tunnel) { exit 0 }

Add-Content $log "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss'): end-to-end check failed, restarting tunnel"
# 杀掉半开/僵尸隧道进程(精确匹配 -R 2222:localhost:22, 不误伤测试会话)
Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match "-R 2222:localhost:22" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 3
Start-Tunnel
Start-Sleep -Seconds 8
if (Test-Tunnel) {
    Add-Content $log "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss'): tunnel restarted OK"
} else {
    Add-Content $log "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss'): restart failed, will retry next cycle"
}
