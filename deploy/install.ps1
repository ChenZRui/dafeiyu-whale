<#
  大肥鱼 B站分身 —— Windows 一键安装

  用法（PowerShell 里跑）：
    cd 到你解压/克隆出来的仓库目录
    powershell -NoProfile -ExecutionPolicy Bypass -File deploy\install.ps1 -AstrBotDir "D:\qqbot\localbot\src\AstrBot-4.29.3"

  参数：
    -AstrBotDir     AstrBot 安装目录（有 main.py 的那层），不给会自动找
    -OwnerQQ        你的 QQ
    -GroupIds       "111,222" 要分享的群，留空不发群
    -OwnerMid       你的 B站 mid
    -SelfMid        大肥鱼自己的 B站 mid
    -LlmProvider    AstrBot 里的 provider id，默认 deepseek/deepseek-chat
    -SkipDeps       不装 python 依赖
    -WhatIfOnly     只看要做什么，不落盘
#>
param(
  [string]$AstrBotDir = "",
  [string]$OwnerQQ = "",
  [string]$GroupIds = "",
  [string]$OwnerMid = "",
  [string]$SelfMid = "",
  [string]$LlmProvider = "deepseek/deepseek-chat",
  [switch]$SkipDeps,
  [switch]$WhatIfOnly
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [Text.Encoding]::UTF8

function Say([string]$m, [string]$c = "Cyan") { Write-Host "[大肥鱼] $m" -ForegroundColor $c }
function Ok([string]$m) { Write-Host "[  OK  ] $m" -ForegroundColor Green }
function Warn([string]$m) { Write-Host "[注意!] $m" -ForegroundColor Yellow }
function Die([string]$m) { Write-Host "[出错!] $m" -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "  大肥鱼 · B站分身  一键安装（Windows）" -ForegroundColor Cyan
Write-Host "  刷B站 / 评论@你 / 私信 / 日记 / 打分台" -ForegroundColor DarkGray
Write-Host ""

$RepoRoot = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $RepoRoot "plugins"))) {
  Die "看起来这个脚本不是从仓库的 deploy\ 目录里跑的（找不到 plugins\）"
}

# ---------- 1. 找 AstrBot ----------
if ($AstrBotDir -eq "") {
  $cands = @(
    "D:\qqbot\localbot\src\AstrBot-4.29.3",
    "D:\AstrBot", "C:\AstrBot",
    "$env:USERPROFILE\AstrBot", "$env:USERPROFILE\astrbot",
    "D:\qqbot\AstrBot"
  )
  foreach ($c in $cands) { if (Test-Path (Join-Path $c "main.py")) { $AstrBotDir = $c; break } }
  if ($AstrBotDir -eq "") {
    if (Test-Path "$env:USERPROFILE\Desktop") { }
    Die "没找到 AstrBot 目录，用 -AstrBotDir '你的路径'（该目录下要有 main.py）"
  }
}
if (-not (Test-Path (Join-Path $AstrBotDir "main.py"))) { Die "$AstrBotDir 下面没有 main.py" }
Ok "AstrBot：$AstrBotDir"

# ---------- 2. 找 python ----------
$Py = ""
foreach ($p in @("venv\Scripts\python.exe", ".venv\Scripts\python.exe")) {
  $full = Join-Path $AstrBotDir $p
  if (Test-Path $full) { $Py = $full; break }
}
if ($Py -eq "") {
  $cmd = Get-Command python -ErrorAction SilentlyContinue
  if ($cmd) { $Py = $cmd.Source; Warn "没找到 AstrBot 的 venv，用系统 python：$Py" }
  else { Die "找不到 python（AstrBot 的 venv\Scripts\python.exe 也没有）" }
}
Ok "Python：$Py"

# ---------- 3. 装插件 ----------
$PluginDir = Join-Path $AstrBotDir "data\plugins"
$ts = Get-Date -Format "yyyyMMddHHmmss"
if (-not (Test-Path $PluginDir)) { if (-not $WhatIfOnly) { New-Item -ItemType Directory -Path $PluginDir -Force | Out-Null } }

foreach ($name in @("astrbot_plugin_bili_whale", "astrbot_plugin_whale_journal")) {
  $dst = Join-Path $PluginDir $name
  if (Test-Path $dst) {
    $bak = "$dst.bak-$ts"
    Warn "$name 已存在 → 备份到 $bak"
    if (-not $WhatIfOnly) { Move-Item $dst $bak }
  }
  if ($WhatIfOnly) { Say "[dry] 复制 $name → $dst" }
  else { Copy-Item (Join-Path $RepoRoot "plugins\$name") $dst -Recurse -Force }
  Ok "已安装 $name"
}

# ---------- 4. 依赖 ----------
if (-not $SkipDeps) {
  $req = Join-Path $PluginDir "astrbot_plugin_bili_whale\requirements.txt"
  if (Test-Path $req) {
    Say "安装 Python 依赖（bilibili-api-python 等）…"
    if ($WhatIfOnly) { Say "[dry] $Py -m pip install -r $req" }
    else {
      & $Py -m pip install -q -i https://pypi.tuna.tsinghua.edu.cn/simple -r $req
      if ($LASTEXITCODE -ne 0) { Warn "清华源失败，试官方源"; & $Py -m pip install -q -r $req }
      Ok "依赖装好了"
    }
  }
} else { Warn "SkipDeps：跳过依赖安装" }

# ---------- 5. 配置 ----------
$CfgDir = Join-Path $AstrBotDir "data\config"
if (-not (Test-Path $CfgDir)) { if (-not $WhatIfOnly) { New-Item -ItemType Directory -Path $CfgDir -Force | Out-Null } }

function Write-Cfg([string]$example, [string]$target, [hashtable]$over) {
  if (Test-Path $target) { Warn "配置已存在，保留不动：$target"; return }
  $cfg = Get-Content $example -Raw -Encoding UTF8 | ConvertFrom-Json
  foreach ($k in $over.Keys) {
    if ($null -eq $over[$k] -or "$($over[$k])" -eq "") { continue }
    if ($cfg.PSObject.Properties.Name -contains $k) { $cfg.$k = $over[$k] }
    else { $cfg | Add-Member -NotePropertyName $k -NotePropertyValue $over[$k] -Force }
  }
  if ($WhatIfOnly) { Say "[dry] 写配置 $target"; return }
  ($cfg | ConvertTo-Json -Depth 10) | Set-Content $target -Encoding UTF8
  Ok "配置已写入 $target"
}

$groups = @()
if ($GroupIds.Trim() -ne "") { $groups = @($GroupIds -split "[,，]" | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" }) }
$over = @{
  owner_qq       = $OwnerQQ
  group_ids      = $(if ($groups.Count -gt 0) { $groups } else { $null })
  owner_bili_mid = $(if ($OwnerMid -match '^\d+$') { [int]$OwnerMid } else { $null })
  self_bili_mid  = $(if ($SelfMid  -match '^\d+$') { [int]$SelfMid }  else { $null })
  llm_provider_id = $LlmProvider
}
Write-Cfg (Join-Path $RepoRoot "deploy\config.example.json") `
          (Join-Path $CfgDir "astrbot_plugin_bili_whale_config.json") $over

$overJ = @{ owner_qq = $OwnerQQ; llm_provider_id = $LlmProvider; data_dir = (Join-Path $AstrBotDir "data\whale_journal") }
Write-Cfg (Join-Path $RepoRoot "deploy\config.journal.example.json") `
          (Join-Path $CfgDir "astrbot_plugin_whale_journal_config.json") $overJ

# ---------- 6. B站 凭证占位 ----------
$CredDir = Join-Path $AstrBotDir "data\bili_whale"
$Cred = Join-Path $CredDir "credentials.json"
if (Test-Path $Cred) { Warn "B站凭证已存在，保留不动" }
else {
  if (-not $WhatIfOnly) {
    New-Item -ItemType Directory -Path $CredDir -Force | Out-Null
    @'
{
  "sessdata": "在这里填你的 SESSDATA",
  "bili_jct": "在这里填你的 bili_jct（CSRF）",
  "buvid3": "在这里填你的 buvid3"
}
'@ | Set-Content $Cred -Encoding UTF8
  }
  Warn "B站凭证占位文件：$Cred —— 必须填真值"
}

# ---------- 7. 收尾 ----------
Write-Host ""
Write-Host "────────────────────────────────────────────"
Write-Host "安装完成。接下来三件事（缺一不可）："
Write-Host ""
Write-Host "1. 填 B站 凭证"
Write-Host "   $Cred"
Write-Host "   浏览器登录 B站 → F12 → Application → Cookies，抄 sessdata / bili_jct / buvid3"
Write-Host ""
Write-Host "2. 核对配置"
Write-Host "   $(Join-Path $CfgDir 'astrbot_plugin_bili_whale_config.json')"
Write-Host "   owner_qq / group_ids / owner_bili_mid / self_bili_mid 必须是你的"
Write-Host ""
Write-Host "3. 重启 AstrBot（关掉再启动，或 AstrBot Launcher 里重载），日志里出现这行就活了："
Write-Host "   [bili-whale] 已就绪｜运行 0 轮｜今天已分享 0 条｜记着 0 个看过的视频"
Write-Host "────────────────────────────────────────────"
Ok "全部完成"
