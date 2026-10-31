#requires -Version 5.1
<#
.SYNOPSIS
  兜底安装：把 @local/dsh-computer-use 挂进 DSH profile 的 package.json。
  仅在 DSH 没有暴露 plugin_manager 工具时使用；官方路径是 plugin_manager install_bundle。

.DESCRIPTION
  只做两件事（都带备份，幂等）：
    1) dependencies["@local/dsh-computer-use"] = "link:<仓库绝对路径>"
    2) dsh.profile.bundles 里追加 "@local/dsh-computer-use"（去重）
  默认 dry-run；加 -Apply 才落盘。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install-profile.ps1
  powershell -ExecutionPolicy Bypass -File scripts\install-profile.ps1 -Apply
#>
[CmdletBinding()]
param(
  [string]$Profile = 'desktop',
  [string]$DshHome = '',
  [string]$Repo = '',
  [switch]$Apply
)

$ErrorActionPreference = 'Stop'

$pkgName = '@local/dsh-computer-use'

if (-not $Repo) { $Repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path }
$Repo = (Resolve-Path $Repo).Path

if (-not $DshHome) {
  $DshHome = if ($env:DSH_HOME) { $env:DSH_HOME } else { Join-Path $env:USERPROFILE '.dsh' }
}
if (-not (Test-Path $DshHome)) { throw "找不到 DSH 家目录：$DshHome（用 -DshHome 指定）" }

$manifest = Join-Path $DshHome "profiles\$Profile\package.json"
if (-not (Test-Path $manifest)) { throw "找不到 profile 清单：$manifest（用 -Profile 指定正确的 profile）" }

$json = Get-Content $manifest -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $json.PSObject.Properties['dependencies']) {
  $json | Add-Member -NotePropertyName dependencies -NotePropertyValue ([pscustomobject]@{})
}
if (-not $json.PSObject.Properties['dsh']) {
  $json | Add-Member -NotePropertyName dsh -NotePropertyValue ([pscustomobject]@{ profile = [pscustomobject]@{ bundles = @() } })
}
if (-not $json.dsh.PSObject.Properties['profile']) {
  $json.dsh | Add-Member -NotePropertyName profile -NotePropertyValue ([pscustomobject]@{ bundles = @() })
}
if (-not $json.dsh.profile.PSObject.Properties['bundles']) {
  $json.dsh.profile | Add-Member -NotePropertyName bundles -NotePropertyValue @()
}

$linkRef = "link:$($Repo -replace '\\','/')"
$current = $json.dependencies.PSObject.Properties[$pkgName]
$bundles = @($json.dsh.profile.bundles)
$hasBundle = $bundles -contains $pkgName

Write-Host "仓库      : $Repo"
Write-Host "profile   : $Profile"
Write-Host "清单      : $manifest"
Write-Host "依赖      : 现在是 $(if ($current) { $current.Value } else { '(无)' })  ->  $linkRef"
Write-Host "bundles   : $(if ($hasBundle) { '已包含，不重复追加' } else { '追加 ' + $pkgName })"

if (-not $Apply) {
  Write-Host ''
  Write-Host '这是 dry-run。确认无误后加 -Apply 落盘。' -ForegroundColor Yellow
  return
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backup = "$manifest.bak-$stamp"
Copy-Item $manifest $backup -Force
Write-Host "已备份 -> $backup"

if ($current) { $current.Value = $linkRef }
else { $json.dependencies | Add-Member -NotePropertyName $pkgName -NotePropertyValue $linkRef }

if (-not $hasBundle) { $json.dsh.profile.bundles = @($bundles + $pkgName) }

$out = $json | ConvertTo-Json -Depth 20
# 不带 BOM：DSH 用 JSON.parse 读它，BOM 会让解析直接抛错
[System.IO.File]::WriteAllText($manifest, $out, (New-Object System.Text.UTF8Encoding($false)))

# 回读校验：能解析、且两处都在
$check = Get-Content $manifest -Raw -Encoding UTF8 | ConvertFrom-Json
$okDep = $check.dependencies.PSObject.Properties[$pkgName].Value -eq $linkRef
$okBundle = @($check.dsh.profile.bundles) -contains $pkgName
if (-not ($okDep -and $okBundle)) { throw "写入后回读校验失败，请用 $backup 还原" }

Write-Host '已写入并回读校验通过。' -ForegroundColor Green
Write-Host '下一步：重启 DSH（或刷新页面），然后按 INSTALL.md 第 2 节做四项验证。'
Write-Host "还原：Copy-Item '$backup' '$manifest' -Force"
