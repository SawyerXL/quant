# Tunnel keepalive v2.3 (2026-09-14): 连续失败闸门 + SYSTEM 身份支持 + 探针超时放宽。
# v2 缺陷(2026-09-13~14 结案): 重工作日 Linux IO 饱和 → 探针超时 → 检查误判
# "隧道死" → 杀健康隧道 → 重启 → 5 分钟后再误判, 自伤振荡循环(09-06 576条/
# 09-07 216条/09-09 150条, io_monitor 实测 vda3 %util 227%)。
# v2.3: 连续 2 次检查失败才杀隧道(状态文件计数), 瞬时探针超时只计数不杀;
#       探针超时 12s→20s 进一步压假阳。
# v2.3 SYSTEM 支持(09-10/11、09-13晚~14晨 两次长静默根因=任务"只使用交互
# 方式"无人登录即停摆): 任务以 SYSTEM + 无论是否登录运行时, 密钥改读
# C:\ProgramData\quant-tunnel\id_ed25519(ACL 仅 SYSTEM, 避开 Administrator
# 私钥 ACL 限制与 OpenSSH 严格权限检查)。
# v2.2 (2026-09-07): Linux SSH 端口 2026-09-05 23:51 从 22 挪到 12201。
# v1 缺陷: 只查 ssh.exe 进程存在即 exit 0 —— 半开隧道永远不自愈。
$ErrorActionPreference = "Continue"
$log  = "H:\quant\logs\tunnel_keepalive.log"
$SSH  = "C:\Windows\System32\OpenSSH\ssh.exe"
$LINUX = "root@106.15.61.81"   # Linux服务器公网IP(变更时改这一处)
$LPORT = "12201"               # Linux SSH 端口(2026-09-05 从 22 迁移)
$FAIL_THRESHOLD = 2            # 连续失败闸门(v2.3): 防负载期误杀健康隧道
$state = "H:\quant\logs\tunnel_fail_count.txt"

# 密钥按运行身份选择: SYSTEM 用 ProgramData 专用副本
if ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name -eq "NT AUTHORITY\SYSTEM") {
    $KEY = "C:\ProgramData\quant-tunnel\id_ed25519"
} else {
    $KEY = "C:\Users\Administrator\.ssh\id_ed25519"
}

function Test-Tunnel {
    # 经主SSH到Linux, 由Linux侧探针脚本经2222反向隧道连回本机 —— 验证完整链路。
    # 探针独立成脚本: PowerShell→ssh 嵌套引号传参会被搅碎(v2.1修复)
    $out = & $SSH -i $KEY -p $LPORT -o BatchMode=yes -o ConnectTimeout=20 `
        -o StrictHostKeyChecking=no $LINUX "/root/quant/scripts/tunnel_probe.sh" 2>$null
    return ($out -join " ") -match "TUNNEL_OK"
}

function Start-Tunnel {
    Start-Process -WindowStyle Hidden -FilePath $SSH `
        -ArgumentList '-R','2222:localhost:22','-N',$LINUX,
            '-p',$LPORT,
            '-o','ServerAliveInterval=15','-o','ServerAliveCountMax=4',
            '-o','TCPKeepAlive=yes','-o','ExitOnForwardFailure=yes',
            '-o','StrictHostKeyChecking=no','-i',$KEY
}

function Get-FailCount {
    if (Test-Path $state) {
        try { return [int](Get-Content $state -Raw) } catch { return 0 }
    }
    return 0
}
function Set-FailCount($n) { Set-Content $state $n -ErrorAction SilentlyContinue }

if (Test-Tunnel) { Set-FailCount 0; exit 0 }

# v2.3 连续失败闸门: 未达阈值只计数不杀——瞬时探针超时不牺牲健康隧道
$fails = (Get-FailCount) + 1
if ($fails -lt $FAIL_THRESHOLD) {
    Set-FailCount $fails
    Add-Content $log "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss'): check failed ($fails/$FAIL_THRESHOLD), grace (transient probe timeout?), no restart"
    exit 0
}
Set-FailCount 0
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
