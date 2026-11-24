# -*- coding: utf-8 -*-
# 自动发现 Hermes 主目录，把本目录安装为 Agent Plugin：
#   1. 复制到 <HermesHome>\plugins\stalker-translate
#   2. 在 <HermesHome>\config.yaml 的 plugins.enabled 中加入 stalker-translate
#   3. 让翻译工具在 Hermes 中直接可见（关闭工具折叠）
#   4. 自动重启 Hermes 内核，让插件立即生效
# 只依赖 Windows 自带 PowerShell 5.1；不需要 Python、pip、管理员权限。
$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
trap { Write-Host "安装失败：$($_.Exception.Message)" -ForegroundColor Red; exit 1 }

function Fail($m) { Write-Host "失败：$m" -ForegroundColor Red; exit 1 }

function Add-Candidate($list, $value) {
    if ($value) { [void]$list.Add([string]$value) }
}

# 配置文件统一按 UTF-8 读写，避免 PowerShell 5.1 默认按 ANSI 读坏中文注释
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Read-ConfigLines([string]$path) {
    return [System.IO.File]::ReadAllLines($path, [System.Text.Encoding]::UTF8)
}

function Write-ConfigLines([string]$path, $lines) {
    [System.IO.File]::WriteAllLines($path, [string[]]$lines, $Utf8NoBom)
}

$candidates = New-Object System.Collections.Generic.List[string]
Add-Candidate $candidates $env:HERMES_HOME
foreach ($base in @($env:LOCALAPPDATA, $env:APPDATA)) {
    if (-not $base) { continue }
    Add-Candidate $candidates (Join-Path $base "hermes")
    Add-Candidate $candidates (Join-Path $base "Hermes")
}
if ($HOME) { Add-Candidate $candidates (Join-Path $HOME ".hermes") }

# 活动 profile：active-profile.json 的 connectionProfiles.local
foreach ($base in @($env:APPDATA, $env:LOCALAPPDATA)) {
    if (-not $base) { continue }
    foreach ($rootName in @("Hermes", "hermes")) {
        $ap = Join-Path (Join-Path $base $rootName) "active-profile.json"
        if (-not (Test-Path $ap)) { continue }
        try {
            $obj = ([System.IO.File]::ReadAllText($ap, [System.Text.Encoding]::UTF8)) | ConvertFrom-Json
            $profile = $obj.connectionProfiles.local
            if ($profile -and $profile -ne "default") {
                Add-Candidate $candidates (Join-Path (Join-Path (Join-Path $base $rootName) "profiles") $profile)
            }
        } catch { }
    }
}

$seen = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::OrdinalIgnoreCase)
$homes = New-Object System.Collections.Generic.List[string]
foreach ($c in $candidates) {
    try { $full = [System.IO.Path]::GetFullPath($c) } catch { continue }
    if (Test-Path (Join-Path $full "config.yaml")) {
        if ($seen.Add($full)) { [void]$homes.Add($full) }
    }
}
if ($homes.Count -eq 0) {
    Fail "未找到 Hermes 的 config.yaml。可能原因：1) Hermes 还没有启动过，首次安装后需要先打开一次；2) Hermes 安装在非默认目录，且没有设置 HERMES_HOME；3) Hermes 刚被卸载，尚未重新安装。请先启动一次 Hermes，再双击本安装器。"
}

function Enable-Plugin($lines) {
    $pIdx = -1
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match '^plugins:\s*$') { $pIdx = $i; break }
    }
    if ($pIdx -lt 0) {
        $out = @($lines)
        if ($out.Count -gt 0 -and $out[-1].Trim() -ne "") { $out += "" }
        $out += "plugins:"
        $out += "  enabled:"
        $out += "    - stalker-translate"
        return $out
    }
    $end = $lines.Count
    for ($i = $pIdx + 1; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match '^\S') { $end = $i; break }
    }
    $eIdx = -1
    for ($i = $pIdx + 1; $i -lt $end; $i++) {
        if ($lines[$i] -match '^\s{2}enabled\s*:') { $eIdx = $i; break }
    }
    if ($eIdx -lt 0) {
        $new = @()
        $new += $lines[0..$pIdx]
        $new += "  enabled:"
        $new += "    - stalker-translate"
        if ($pIdx + 1 -lt $lines.Count) { $new += $lines[($pIdx + 1)..($lines.Count - 1)] }
        return $new
    }
    $line = $lines[$eIdx]
    if ($line -match '^\s{2}enabled\s*:\s*\[(.*)\]\s*$') {
        $items = @()
        if ($Matches[1].Trim()) {
            $items = @($Matches[1].Split(',') |
                ForEach-Object { $_.Trim().Trim("'").Trim('"') } |
                Where-Object { $_ })
        }
        if ($items -notcontains "stalker-translate") { $items += "stalker-translate" }
        $repl = @("  enabled:") + ($items | ForEach-Object { "    - $_" })
        $new = @()
        if ($eIdx -gt 0) { $new += $lines[0..($eIdx - 1)] }
        $new += $repl
        if ($eIdx + 1 -lt $lines.Count) { $new += $lines[($eIdx + 1)..($lines.Count - 1)] }
        return $new
    }
    if ($line -match '^\s{2}enabled\s*:\s*$') {
        $has = $false
        for ($i = $eIdx + 1; $i -lt $end; $i++) {
            if ($lines[$i] -match '^\s{2,4}-\s*stalker-translate\s*$') { $has = $true; break }
            if ($lines[$i] -match '^\s{2}\S') { break }
        }
        if ($has) { return $lines }
        $new = @()
        $new += $lines[0..$eIdx]
        $new += "    - stalker-translate"
        if ($eIdx + 1 -lt $lines.Count) { $new += $lines[($eIdx + 1)..($lines.Count - 1)] }
        return $new
    }
    Write-Host "提示：config.yaml 里的 plugins.enabled 不是 YAML 列表格式，无法自动启用。请在 plugins.enabled 下手动加入一行：- stalker-translate" -ForegroundColor Yellow
    return $lines
}

# Hermes 默认会把 MCP / 插件工具折叠成“延迟工具”，小模型往往不会先搜索再调用。
# 关闭折叠后，check_env / translate_mod / translate_text 会直接出现在工具列表中。
function Set-ToolSearchOff($lines) {
    $tIdx = -1
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match '^tools:\s*$') { $tIdx = $i; break }
    }
    if ($tIdx -lt 0) {
        $out = @($lines)
        if ($out.Count -gt 0 -and $out[-1].Trim() -ne "") { $out += "" }
        $out += "tools:"
        $out += "  tool_search:"
        $out += "    enabled: `"off`""
        return $out
    }
    $end = $lines.Count
    for ($i = $tIdx + 1; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match '^\S') { $end = $i; break }
    }
    $tsIdx = -1
    for ($i = $tIdx + 1; $i -lt $end; $i++) {
        if ($lines[$i] -match '^\s{2}tool_search\s*:\s*$') { $tsIdx = $i; break }
    }
    if ($tsIdx -lt 0) {
        $new = @()
        $new += $lines[0..$tIdx]
        $new += "  tool_search:"
        $new += "    enabled: `"off`""
        if ($tIdx + 1 -lt $lines.Count) { $new += $lines[($tIdx + 1)..($lines.Count - 1)] }
        return $new
    }
    $tsEnd = $end
    for ($i = $tsIdx + 1; $i -lt $end; $i++) {
        if ($lines[$i] -match '^\s{0,2}\S') { $tsEnd = $i; break }
    }
    for ($i = $tsIdx + 1; $i -lt $tsEnd; $i++) {
        if ($lines[$i] -match '^(\s{4})enabled\s*:') {
            $lines[$i] = "    enabled: `"off`""
            return $lines
        }
    }
    $new = @()
    $new += $lines[0..$tsIdx]
    $new += "    enabled: `"off`""
    if ($tsIdx + 1 -lt $lines.Count) { $new += $lines[($tsIdx + 1)..($lines.Count - 1)] }
    return $new
}

# 小模型面对 27 个工具时会乱选（实测去开终端/浏览器/代码执行）。
# 汉化只需要 MCP 工具，因此在本 profile 关闭这些容易带偏的工具集。
$WantedToolsets = @("terminal", "file", "browser", "code_execution", "computer_use")

function Add-DisabledToolsets($lines, $items) {
    $aIdx = -1
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match '^agent:\s*$') { $aIdx = $i; break }
    }
    if ($aIdx -lt 0) {
        $out = @($lines)
        if ($out.Count -gt 0 -and $out[-1].Trim() -ne "") { $out += "" }
        $out += "agent:"
        $out += "  disabled_toolsets:"
        foreach ($it in $items) { $out += "    - $it" }
        return $out
    }
    $end = $lines.Count
    for ($i = $aIdx + 1; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match '^\S') { $end = $i; break }
    }
    $dIdx = -1
    for ($i = $aIdx + 1; $i -lt $end; $i++) {
        if ($lines[$i] -match '^\s{2}disabled_toolsets\s*:') { $dIdx = $i; break }
    }
    if ($dIdx -lt 0) {
        $new = @()
        $new += $lines[0..$aIdx]
        $new += "  disabled_toolsets:"
        foreach ($it in $items) { $new += "    - $it" }
        if ($aIdx + 1 -lt $lines.Count) { $new += $lines[($aIdx + 1)..($lines.Count - 1)] }
        return $new
    }
    $line = $lines[$dIdx]
    if ($line -match '^\s{2}disabled_toolsets\s*:\s*\[(.*)\]\s*$') {
        $existing = @()
        if ($Matches[1].Trim()) {
            $existing = @($Matches[1].Split(',') |
                ForEach-Object { $_.Trim().Trim("'").Trim('"') } |
                Where-Object { $_ })
        }
        foreach ($it in $items) { if ($existing -notcontains $it) { $existing += $it } }
        $repl = @("  disabled_toolsets:") + ($existing | ForEach-Object { "    - $_" })
        $new = @()
        if ($dIdx -gt 0) { $new += $lines[0..($dIdx - 1)] }
        $new += $repl
        if ($dIdx + 1 -lt $lines.Count) { $new += $lines[($dIdx + 1)..($lines.Count - 1)] }
        return $new
    }
    if ($line -match '^\s{2}disabled_toolsets\s*:\s*$') {
        $existing = @()
        $insertAt = $dIdx + 1
        for ($i = $dIdx + 1; $i -lt $end; $i++) {
            if ($lines[$i] -match '^\s{4}-\s*(.+?)\s*$') {
                $existing += $Matches[1].Trim("'").Trim('"')
                $insertAt = $i + 1
                continue
            }
            if ($lines[$i] -match '^\s{2}\S') { $insertAt = $i; break }
        }
        $add = @()
        foreach ($it in $items) { if ($existing -notcontains $it) { $add += "    - $it" } }
        if ($add.Count -eq 0) { return $lines }
        $new = @()
        if ($insertAt -gt 0) { $new += $lines[0..($insertAt - 1)] }
        $new += $add
        if ($insertAt -lt $lines.Count) { $new += $lines[$insertAt..($lines.Count - 1)] }
        return $new
    }
    return $lines
}

# SOUL.md 会被 Hermes 自动加载进系统提示词；把汉化规则写成常驻规则，
# 比让小模型自己去 skill_view 更可靠。
function Install-SoulRule([string]$homeDir) {
    $soulPath = Join-Path $homeDir "SOUL.md"
    $rule = @'
<!-- stalker-translate:begin -->
## STALKER 汉化（最高优先级）

- 用户要求“翻译这个MOD / 汉化这个目录”，或消息里带 STALKER/X-Ray MOD 目录时，必须直接调用工具 `mcp__stalker_translate_plugin__translate_mod`，`input_dir` 用用户给的目录。
- 禁止用 terminal、execute_code、browser、read_file、search_files 等工具扫描 MOD；翻译工具会自己处理。
- 不要启动、下载或切换模型。只有翻译报错时才调用 `check_env`。
- 本机是 Windows，路径形如 `C:\Users\...`；禁止使用 `/Users/...` 或 `/home/...`。
- 不要要求用户上传文件，也不要编造路径。
<!-- stalker-translate:end -->
'@
    $text = ""
    if (Test-Path -LiteralPath $soulPath) {
        $text = [System.IO.File]::ReadAllText($soulPath, [System.Text.Encoding]::UTF8)
    }
    $pattern = '(?s)<!-- stalker-translate:begin -->.*?<!-- stalker-translate:end -->'
    if ($text -match $pattern) {
        $text = [regex]::Replace($text, $pattern, $rule)
    } else {
        if ($text.Trim().Length -gt 0) { $text = $text.TrimEnd() + "`r`n`r`n" }
        $text += $rule
    }
    [System.IO.File]::WriteAllText($soulPath, $text, $Utf8NoBom)
}

function Test-IsElevated {
    try {
        $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
        $principal = New-Object System.Security.Principal.WindowsPrincipal($identity)
        return $principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch { }
    return $false
}

function Find-HermesCli {
    $cands = New-Object System.Collections.Generic.List[string]
    Add-Candidate $cands $env:HERMES_CLI
    if ($env:LOCALAPPDATA) {
        Add-Candidate $cands (Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps\hermes.exe")
        Add-Candidate $cands (Join-Path $env:LOCALAPPDATA "Programs\Hermes\hermes.exe")
    }
    if ($env:ProgramFiles) {
        Add-Candidate $cands (Join-Path $env:ProgramFiles "Hermes\hermes.exe")
    }
    $cmd = Get-Command hermes.exe -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source) { Add-Candidate $cands $cmd.Source }
    foreach ($c in $cands) {
        if ($c -and (Test-Path -LiteralPath $c)) { return $c }
    }
    return $null
}

# 安装器如果在 Hermes 会话里被执行，重启内核会中断当前会话，因此改为提示用户重开
function Test-InsideHermes {
    try {
        $current = Get-CimInstance Win32_Process -Filter "ProcessId=$PID" -ErrorAction SilentlyContinue
        for ($i = 0; $i -lt 8 -and $current; $i++) {
            $parentId = $current.ParentProcessId
            if (-not $parentId) { break }
            $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$parentId" -ErrorAction SilentlyContinue
            if (-not $parent) { break }
            if ($parent.Name -match '^Hermes' -or ($parent.CommandLine -and $parent.CommandLine -match 'hermes_cli\.main|hermes-agent')) {
                return $true
            }
            $current = $parent
        }
    } catch { }
    return $false
}

function Restart-HermesGateway([string]$homeDir) {
    $cli = Find-HermesCli
    if (-not $cli) { return "no-cli" }
    $oldHome = $env:HERMES_HOME
    $outFile = Join-Path $env:TEMP ("hermes-restart-" + [guid]::NewGuid().ToString("N") + ".out")
    $errFile = "$outFile.err"
    try {
        $env:HERMES_HOME = $homeDir
        # --yes：安装时必须重启内核；即使还有对话在跑也强制重启
        $proc = Start-Process -FilePath $cli -ArgumentList @("gateway", "restart", "--yes") `
            -NoNewWindow -PassThru -RedirectStandardOutput $outFile -RedirectStandardError $errFile
        if (-not $proc.WaitForExit(180000)) {
            try { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue } catch { }
            return "timeout"
        }
        # Store 版的 hermes.exe 是启动别名，退出码可能为空；空值按成功处理，
        # 最终由日志里的 MCP 注册结果确认。
        $proc.Refresh()
        if ($null -ne $proc.ExitCode -and $proc.ExitCode -ne 0) { return "failed" }
        return "ok"
    } catch {
        return "failed"
    } finally {
        if ($oldHome) { $env:HERMES_HOME = $oldHome }
        else { Remove-Item Env:\HERMES_HOME -ErrorAction SilentlyContinue }
        Remove-Item $outFile, $errFile -Force -ErrorAction SilentlyContinue
    }
}

function Stop-GatewayProcess {
    $found = $false
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match 'hermes_cli\.main' -and $_.CommandLine -match 'gateway run' } |
        ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            $found = $true
        }
    return $found
}

function Test-GatewayRunning {
    $proc = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match 'hermes_cli\.main' -and $_.CommandLine -match 'gateway run' } |
        Select-Object -First 1
    return [bool]$proc
}

function Start-GatewayProcess([string]$homeDir) {
    $cli = Find-HermesCli
    if (-not $cli) { return $false }
    $oldHome = $env:HERMES_HOME
    try {
        $env:HERMES_HOME = $homeDir
        Start-Process -FilePath $cli -ArgumentList @("gateway", "run", "--quiet") -WindowStyle Hidden | Out-Null
        return $true
    } catch {
        return $false
    } finally {
        if ($oldHome) { $env:HERMES_HOME = $oldHome }
        else { Remove-Item Env:\HERMES_HOME -ErrorAction SilentlyContinue }
    }
}

function Confirm-McpLoaded([string]$homeDir, [datetime]$since) {
    $log = Join-Path $homeDir "logs\agent.log"
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 1000
        if (-not (Test-Path -LiteralPath $log)) { continue }
        try { $tail = @(Get-Content -LiteralPath $log -Tail 300 -ErrorAction Stop) } catch { continue }
        foreach ($line in $tail) {
            if ($line -notmatch "stalker-translate-plugin" -or $line -notmatch "registered") { continue }
            if ($line -match '^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})') {
                try {
                    $ts = [datetime]::ParseExact($Matches[1], 'yyyy-MM-dd HH:mm:ss', $null)
                    if ($ts -ge $since.AddSeconds(-5)) { return $true }
                } catch { }
            }
        }
    }
    return $false
}

$installed = New-Object System.Collections.Generic.List[string]
foreach ($h in $homes) {
    $plugins = Join-Path $h "plugins"
    $target = Join-Path $plugins "stalker-translate"
    New-Item -ItemType Directory -Force -Path $plugins | Out-Null
    New-Item -ItemType Directory -Force -Path $target | Out-Null
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine.ToLower().Contains($target.ToLower()) } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Milliseconds 800
    try {
        Get-ChildItem -LiteralPath $Here |
            Where-Object { $_.Name -notmatch '^install-plugin\.' -and $_.Name -ne 'skills' } |
            ForEach-Object { Copy-Item $_.FullName $target -Recurse -Force }
    } catch {
        Fail "复制插件文件失败。原因：Hermes 可能正在运行并占用 runtime 文件，或当前用户没有写入权限。请完全退出 Hermes 后重试。详细信息：$($_.Exception.Message)"
    }
    Get-ChildItem $target -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

    # 旧版本把技能放在插件目录里，会和本地技能重名；升级时清掉。
    $staleSkills = Join-Path $target "skills"
    if (Test-Path -LiteralPath $staleSkills) {
        Remove-Item $staleSkills -Recurse -Force -ErrorAction SilentlyContinue
    }

    $cfg = Join-Path $h "config.yaml"
    Copy-Item $cfg "$cfg.bak-stalker-plugin-$(Get-Date -Format yyyyMMddHHmmss)" -Force
    $lines = @(Read-ConfigLines $cfg)
    $lines = @(Enable-Plugin $lines)
    $lines = @(Set-ToolSearchOff $lines)
    $lines = @(Add-DisabledToolsets $lines $WantedToolsets)
    Write-ConfigLines $cfg $lines
    Write-Host "已安装并启用：$target"

    # 常驻规则写进 SOUL.md，避免小模型自由发挥
    Install-SoulRule $h
    Write-Host "已写入汉化常驻规则：SOUL.md"

    # 插件技能在 Hermes 里的正式名是 "插件名:技能名"，小模型通常只会按裸名调用。
    # 这里再把技能安装到 <HermesHome>\skills，裸名 stalker-translate 也能直接加载。
    $skillSrc = Join-Path $Here "skills\stalker-translate\SKILL.md"
    if (Test-Path -LiteralPath $skillSrc) {
        $skillDst = Join-Path (Join-Path $h "skills") "stalker-translate"
        New-Item -ItemType Directory -Force -Path $skillDst | Out-Null
        Copy-Item $skillSrc (Join-Path $skillDst "SKILL.md") -Force
        Write-Host "已安装技能：stalker-translate"
    }
    [void]$installed.Add($h)
}

Write-Host ""
$inside = Test-InsideHermes
$elevated = Test-IsElevated
foreach ($h in $installed) {
    if ($elevated) {
        # 管理员权限启动的内核，普通权限的 Hermes 桌面端连不上（gateway_elevated）。
        [void](Stop-GatewayProcess)
        Write-Host "安装成功：插件、技能和汉化规则已安装并启用。" -ForegroundColor Green
        Write-Host "安装器以管理员身份运行，不能直接启动 Hermes 内核（否则桌面端会提示连接失败）。"
        Write-Host "请用普通方式重新打开 Hermes，插件会在启动时自动加载。"
        break
    }
    if ($inside) {
        Write-Host "安装成功：插件和技能已安装并启用。" -ForegroundColor Green
        Write-Host "请关闭 Hermes 窗口（包括系统托盘里的 Hermes）后重新打开，插件会自动加载。"
        break
    }
    Write-Host "正在重启 Hermes 内核（会中断当前对话），让翻译工具立即生效..." -ForegroundColor Cyan
    $started = Get-Date
    $result = Restart-HermesGateway $h
    if ($result -eq "ok") {
        if (Confirm-McpLoaded $h $started) {
            Write-Host "安装成功：Hermes 已自动加载 stalker-translate 翻译工具和技能。" -ForegroundColor Green
            Write-Host "现在可以直接在 Hermes 对话框里拖入 MOD 目录，输入“翻译这个MOD”开始。"
        } else {
            Write-Host "安装成功：插件已安装并启用，Hermes 已重启。" -ForegroundColor Green
            Write-Host "如果对话框提示未连接，请关闭 Hermes 窗口后重新打开。"
        }
    } else {
        Write-Host "正在用兜底方式重启 Hermes 内核..." -ForegroundColor Cyan
        [void](Stop-GatewayProcess)
        Start-Sleep -Seconds 2
        $started2 = Get-Date
        [void](Start-GatewayProcess $h)
        if (Confirm-McpLoaded $h $started2) {
            Write-Host "安装成功：Hermes 已自动加载 stalker-translate 翻译工具和技能。" -ForegroundColor Green
            Write-Host "现在可以直接在 Hermes 对话框里拖入 MOD 目录，输入“翻译这个MOD”开始。"
        } else {
            Write-Host "安装成功：插件、技能和汉化规则已安装。" -ForegroundColor Green
            Write-Host "请打开 Hermes（如果窗口已打开，请关闭后重新打开），插件会在启动时自动加载。"
        }
    }
    break
}
