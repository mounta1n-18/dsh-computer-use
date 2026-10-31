# 打一个可独立安装的 tarball（发布模式：装进 profile 后本文件夹可删可移）。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts\pack.ps1
# 产物：local-dsh-computer-use-<version>.tgz（在插件根目录）
#
# 装法（在 DSH 里）：
#   plugin_manager { action: "install_bundle", target: "<这个 tgz 的绝对路径>" }
#
# 注意：开发模式（install_bundle 指向文件夹）改代码即时生效；发布模式要重新 pack + 安装。
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Push-Location $root
try {
    $pkg = Get-Content package.json -Raw -Encoding UTF8 | ConvertFrom-Json
    Write-Host "[pack] $($pkg.name)@$($pkg.version)  目录：$root"
    # npm pack 会按 package.json 的 files 白名单打包，省得手写清单漏文件
    $out = & npm pack --silent 2>&1
    if ($LASTEXITCODE -ne 0) { throw "npm pack 失败：$out" }
    $tgz = ($out | Select-Object -Last 1).ToString().Trim()
    $full = Join-Path $root $tgz
    if (-not (Test-Path $full)) { throw "没找到产物 $full" }
    $size = [math]::Round((Get-Item $full).Length / 1KB, 1)
    Write-Host "[pack] 完成：$tgz  ($size KB)"
    Write-Host "[pack] 安装：plugin_manager { action: ""install_bundle"", target: ""$full"" }"
} finally {
    Pop-Location
}
