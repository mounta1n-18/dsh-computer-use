# 回滚：把本插件在机器上留下的东西清干净，并给出恢复旧技能目录的命令。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts\rollback.ps1 [-OldSkillsBackup <备份目录>]
#
# 为什么不能全自动：**卸载插件只能走 DSH 的 plugin_manager**（官方唯一支持路径，
# 手改 profile 的 package.json / pnpm-lock.yaml 会留下不一致）。所以这里做两件事：
#   1. 把不依赖 DSH 的部分清理掉（残留进程、运行目录、Chrome 调试 profile）；
#   2. 打印出剩下要你在 DSH 里敲的那一条命令，以及旧技能的恢复命令。
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

Write-Host "== 1. 回收常驻服务进程 =="
$killed = 0
Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*cu_server.py*' } |
    ForEach-Object {
        Write-Host "  kill pid=$($_.ProcessId)"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        $killed++
    }
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*uia_sidecar.ps1*' } |
    ForEach-Object {
        Write-Host "  kill uia sidecar pid=$($_.ProcessId)"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        $killed++
    }
Write-Host "  已回收 $killed 个进程"

Write-Host "== 2. 释放可能被 latch 的修饰键 =="
$py = Join-Path $root 'python\cu.py'
if (Test-Path $py) {
    & python $py release 2>&1 | ForEach-Object { "  $_" }
} else {
    Write-Host "  （找不到 python\cu.py，跳过）"
}

Write-Host "== 3. 运行目录（审计日志、截图缓存、Chrome 调试 profile）=="
$dirs = @(
    (Join-Path $env:LOCALAPPDATA 'dsh-computer-use')
)
foreach ($d in $dirs) {
    if (Test-Path $d) {
        Write-Host "  将删除 $d"
        Remove-Item -Recurse -Force $d -ErrorAction SilentlyContinue
    }
}
$shotDir = Join-Path $env:TEMP 'cu'
if (Test-Path $shotDir) {
    Write-Host "  将删除截图缓存 $shotDir"
    Remove-Item -Recurse -Force $shotDir -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "== 4. 还需要你在 DSH 里做（本脚本不能替你做）==" -ForegroundColor Yellow
Write-Host '  卸载插件：plugin_manager { action: "remove_bundle", target: "@local/dsh-computer-use" }'
if ($args.Count -ge 1 -and $args[0] -eq '-OldSkillsBackup' -and $args.Count -ge 2) {
    $bak = $args[1]
} else {
    $bak = $null
}
if ($bak -and (Test-Path $bak)) {
    $dst = Join-Path $env:USERPROFILE '.dsh\skills\computer-use'
    Write-Host "  恢复旧技能：robocopy `"$bak`" `"$dst`" /E"
} else {
    Write-Host "  恢复旧技能：把 ~/.dsh/backups/iter6-computer-use-* 拷回 ~/.dsh/skills/computer-use"
}
Write-Host "  然后重启 DSH（或刷新页面）让条目树重新加载。"
