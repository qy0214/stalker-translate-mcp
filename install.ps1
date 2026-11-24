# -*- coding: utf-8 -*-
# STALKER 汉化技能（Hermes Agent）一键安装。
# 做三件事：复制技能到 Hermes 技能目录、生成 config.json、把 MCP 服务写进
# Hermes 的 config.yaml（先备份）。可重复运行；已有 config.json 不会被覆盖。

$ErrorActionPreference = "Stop"

$SkillName = "stalker-translate"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogTag = "[stalker-translate]"

function Write-Step($msg) { Write-Host "$LogTag $msg" }
function Fail($msg) { Write-Host "$LogTag 失败：$msg" -ForegroundColor Red; exit 1 }

# ── 1. 定位 Hermes 配置与技能目录 ──────────────────────────────────
$HermesHome = $null
if ($env:LOCALAPPDATA -and (Test-Path (Join-Path $env:LOCALAPPDATA "Hermes"))) {
    $HermesHome = Join-Path $env:LOCALAPPDATA "Hermes"
} elseif ($env:APPDATA -and (Test-Path (Join-Path $env:APPDATA "Hermes"))) {
    $HermesHome = Join-Path $env:APPDATA "Hermes"
} elseif (Test-Path (Join-Path $HOME ".hermes")) {
    $HermesHome = Join-Path $HOME ".hermes"
} else {
    Fail "未找到 Hermes 配置目录（LOCALAPPDATA\Hermes / APPDATA\Hermes / ~/.hermes）。请确认 Hermes Agent 已安装。"
}
$HermesConfig = Join-Path $HermesHome "config.yaml"
$SkillsDir = Join-Path (Join-Path $HermesHome "skills") $SkillName
Write-Step "Hermes 目录：$HermesHome"

# ── 2. 找 Python（MCP 服务只用标准库，不需要 pip 安装任何东西）────
$PyExe = $null
$PyArgs = @()
$PyVer = ""
function Test-Python($exe, $extraArgs) {
    try {
        $out = & $exe @extraArgs -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) {
            $parts = "$out".Trim().Split(".")
            if ([int]$parts[0] -ge 3 -and [int]$parts[1] -ge 8) { return "$out".Trim() }
        }
    } catch { }
    return $null
}
$cand = Get-Command python -ErrorAction SilentlyContinue
if ($cand -and $cand.Source) {
    $ver = Test-Python $cand.Source @()
    if ($ver) { $PyExe = $cand.Source; $PyVer = $ver }
}
if (-not $PyExe) {
    $cand = Get-Command py -ErrorAction SilentlyContinue
    if ($cand -and $cand.Source) {
        $ver = Test-Python $cand.Source @("-3")
        if ($ver) { $PyExe = $cand.Source; $PyArgs = @("-3"); $PyVer = $ver }
    }
}
if (-not $PyExe) {
    Fail "未找到 Python 3.8+。请先安装 Python（https://www.python.org/downloads/），勾选 Add to PATH 后重跑本脚本。"
}
Write-Step "Python：$PyExe（$PyVer）"

# ── 3. 复制技能文件 ────────────────────────────────────────────────
New-Item -ItemType Directory -Force -Path $SkillsDir | Out-Null
$items = @("SKILL.md", "server.py", "config.example.json", "README.md",
           "VERSION", "LICENSE")
foreach ($it in $items) {
    $src = Join-Path $Here $it
    if (Test-Path $src) { Copy-Item $src $SkillsDir -Force }
}
$kernelSrc = Join-Path $Here "kernel"
if (-not (Test-Path $kernelSrc)) { Fail "安装包缺少 kernel 目录。" }
$kernelDst = Join-Path $SkillsDir "kernel"
New-Item -ItemType Directory -Force -Path $kernelDst | Out-Null
Copy-Item (Join-Path $kernelSrc "*") $kernelDst -Recurse -Force

# 清掉旧字节码；日志与用户词条保留
Get-ChildItem $SkillsDir -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Write-Step "技能已复制到：$SkillsDir"

# ── 4. 生成 config.json（首次安装才写；读 Hermes 的本地模型端点）──
$CfgPath = Join-Path $SkillsDir "config.json"
if (-not (Test-Path $CfgPath)) {
    $BaseUrl = "http://127.0.0.1:18434/v1"
    $ModelName = "local-model"
    if (Test-Path $HermesConfig) {
        $lines = @(Get-Content -LiteralPath $HermesConfig)
        $modelStart = -1
        for ($i = 0; $i -lt $lines.Count; $i++) {
            if ($lines[$i] -match '^model:\s*$') { $modelStart = $i; break }
        }
        if ($modelStart -ge 0) {
            for ($i = $modelStart + 1; $i -lt $lines.Count; $i++) {
                if ($lines[$i] -match '^\S') { break }
                if ($lines[$i] -match '^\s+base_url:\s*[''"]?([^''"\s#]+)') {
                    $BaseUrl = $Matches[1]
                }
                if ($lines[$i] -match '^\s+default:\s*[''"]?([^''"\s#]+)') {
                    $ModelName = $Matches[1]
                }
            }
        }
    }
    $cfg = [ordered]@{
        base_url = $BaseUrl
        api_key = ""
        model = $ModelName
        ctx_size = 8192
        parallel = 1
        timeout_sec = 300
        prompt_extra = ""
        official_prompts = $false
        source_lang = "auto"
        target_lang = "chs"
    }
    $json = $cfg | ConvertTo-Json
    [System.IO.File]::WriteAllText($CfgPath, $json, (New-Object System.Text.UTF8Encoding($false)))
    Write-Step "已生成 config.json：$BaseUrl（模型：$ModelName）"
} else {
    Write-Step "保留已有 config.json：$CfgPath"
}

# ── 5. 注册 MCP 服务到 Hermes config.yaml ─────────────────────────
function Format-YamlValue($s) { return "'" + ($s -replace "'", "''") + "'" }
$ServerScript = Join-Path $SkillsDir "server.py"
$argv = @($PyArgs) + @($ServerScript)
$argsInner = (@($argv) | ForEach-Object { Format-YamlValue $_ }) -join ", "
$blockBody = @(
    "${SkillName}:",
    "  command: $(Format-YamlValue $PyExe)",
    "  args: [$argsInner]",
    "  timeout: 86400",
    "  connect_timeout: 120"
)

if (-not (Test-Path $HermesConfig)) {
    Write-Step "未找到 $HermesConfig；请手动把下面内容加入 config.yaml："
    Write-Host "mcp_servers:"
    $blockBody | ForEach-Object { Write-Host ("  " + $_) }
    Write-Step "完成。重启 Hermes 后即可让 Agent 汉化 MOD。"
    exit 0
}

$lines = @(Get-Content -LiteralPath $HermesConfig)
$backup = "$HermesConfig.bak-stalker-" + (Get-Date -Format "yyyyMMddHHmmss")
Copy-Item $HermesConfig $backup -Force
Write-Step "已备份：$backup"

$activeIdx = -1
for ($i = 0; $i -lt $lines.Count; $i++) {
    if ($lines[$i] -match '^mcp_servers:\s*$') { $activeIdx = $i; break }
}
if ($activeIdx -ge 0) {
    $childEnd = $lines.Count
    for ($i = $activeIdx + 1; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match '^\S') { $childEnd = $i; break }
    }
    $indent = "  "
    for ($i = $activeIdx + 1; $i -lt $childEnd; $i++) {
        if ($lines[$i] -match '^(\s+)\S') { $indent = $Matches[1]; break }
    }
    $already = $false
    $namePat = '^' + [regex]::Escape($indent) + [regex]::Escape($SkillName) + ':\s*$'
    for ($i = $activeIdx + 1; $i -lt $childEnd; $i++) {
        if ($lines[$i] -match $namePat) { $already = $true; break }
    }
    if ($already) {
        Write-Step "config.yaml 里已存在 $SkillName，跳过注册"
    } else {
        $out = @()
        $out += $lines[0..$activeIdx]
        $out += ($blockBody | ForEach-Object { $indent + $_ })
        if ($activeIdx + 1 -lt $lines.Count) {
            $out += $lines[($activeIdx + 1)..($lines.Count - 1)]
        }
        [System.IO.File]::WriteAllLines($HermesConfig, $out)
        Write-Step "已把 MCP 服务写入 config.yaml 的 mcp_servers 段"
    }
} else {
    $out = @($lines)
    if ($out.Count -gt 0 -and $out[-1].Trim() -ne "") { $out += "" }
    $out += "mcp_servers:"
    $out += ($blockBody | ForEach-Object { "  " + $_ })
    [System.IO.File]::WriteAllLines($HermesConfig, $out)
    Write-Step "已在 config.yaml 末尾追加 mcp_servers 段"
}

Write-Host ""
Write-Host "$LogTag 安装完成。下一步：" -ForegroundColor Green
Write-Host "  1) 重启 Hermes Agent（让 MCP 服务生效）"
Write-Host "  2) 按你的教程启动本地模型"
Write-Host "  3) 对 Agent 说：汉化 D:\mods\你的MOD"
Write-Host "  配置文件：$CfgPath"
