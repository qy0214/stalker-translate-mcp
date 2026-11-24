param(
    [string]$OutDir = "dist"
)

$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Source = Join-Path $Here "hermes-plugin"
$Utf8 = New-Object System.Text.UTF8Encoding($false)
$Version = ([System.IO.File]::ReadAllText((Join-Path $Source "plugin.json"), $Utf8) | ConvertFrom-Json).version
$RootVersion = ([System.IO.File]::ReadAllText((Join-Path $Here "VERSION"), $Utf8)).Trim()

if ($Version -ne $RootVersion) {
    throw "版本不一致：hermes-plugin/plugin.json=$Version，VERSION=$RootVersion"
}

$required = @(
    "plugin.json", "mcp.json", "config.json", "server.py",
    "install-plugin.cmd", "install-plugin.ps1", "README-MCP.md",
    "runtime/python.exe", "kernel/plugin.py",
    "kernel/assets/glossary_builtin.json",
    "kernel/assets/stalker_termbase.json",
    "kernel/assets/xray_pkg.zip",
    "skills/stalker-translate/SKILL.md"
)
foreach ($rel in $required) {
    if (-not (Test-Path -LiteralPath (Join-Path $Source $rel))) {
        throw "缺少打包文件：$rel"
    }
}

$Stage = Join-Path $env:TEMP ("stalker-translate-mcp-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $Stage | Out-Null
try {
    Get-ChildItem -LiteralPath $Source | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $Stage -Recurse -Force
    }
    Copy-Item -LiteralPath (Join-Path $Here "LICENSE") -Destination $Stage -Force
    Copy-Item -LiteralPath (Join-Path $Here "THIRD_PARTY_NOTICES.md") -Destination $Stage -Force

    Get-ChildItem -LiteralPath $Stage -Recurse -Directory -Filter "__pycache__" |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    $excluded = @(
        "logs", "kernel/assets/glossary_user.json", "kernel/assets/session_terms",
        "kernel/assets/models", "kernel/assets/bin", "kernel/assets/downloads",
        "kernel/assets/logs"
    )
    foreach ($rel in $excluded) {
        $path = Join-Path $Stage $rel
        if (Test-Path -LiteralPath $path) {
            Remove-Item -LiteralPath $path -Recurse -Force
        }
    }

    $out = if ([System.IO.Path]::IsPathRooted($OutDir)) {
        $OutDir
    } else {
        Join-Path $Here $OutDir
    }
    New-Item -ItemType Directory -Path $out -Force | Out-Null
    $zip = Join-Path $out ("stalker-translate-MCP-" + $Version + ".zip")
    if (Test-Path -LiteralPath $zip) {
        Remove-Item -LiteralPath $zip -Force
    }
    Compress-Archive -Path (Join-Path $Stage "*") -DestinationPath $zip -CompressionLevel Optimal
    $mb = [math]::Round((Get-Item -LiteralPath $zip).Length / 1MB, 1)
    Write-Host "已生成：$zip（$mb MB）"
} finally {
    if (Test-Path -LiteralPath $Stage) {
        Remove-Item -LiteralPath $Stage -Recurse -Force -ErrorAction SilentlyContinue
    }
}
