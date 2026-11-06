# Environment probe; no installation or bill processing.
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$env:PYTHONDONTWRITEBYTECODE = '1'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$wintonFailed = $false
Write-Host 'Winton-scan 移機環境檢查' -ForegroundColor Cyan
try {
    $wintonPython = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
    if (-not (Test-Path -LiteralPath $wintonPython -PathType Leaf)) {
        $wintonPython = (Get-Command python.exe -CommandType Application -ErrorAction Stop).Source
    }
    & $wintonPython (Join-Path $PSScriptRoot 'check_environment.py')
    if ($LASTEXITCODE -ne 0) { $wintonFailed = $true }
} catch {
    Write-Host ('[FAIL] Python: ' + $_.Exception.Message)
    $wintonFailed = $true
}
if ($null -eq [Type]::GetTypeFromProgID('Excel.Application')) {
    Write-Host '[FAIL] 找不到 Microsoft Excel 桌面版註冊。'
    $wintonFailed = $true
} else { Write-Host '[OK] Microsoft Excel 已註冊（尚未實際開啟或匯出）' }
$wintonCodex = Get-Command codex.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $wintonCodex) { $wintonCodex = Get-Command codex.cmd -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1 }
if (-not $wintonCodex) {
    Write-Host '[FAIL] 找不到 Codex CLI，請依移機使用說明安裝並登入。'
    $wintonFailed = $true
} else {
    & $wintonCodex.Source --version
    if ($LASTEXITCODE -ne 0) { $wintonFailed = $true }
    try {
        $ErrorActionPreference = 'Continue'
        & $wintonCodex.Source login status *> $null
        $wintonLoginExit = $LASTEXITCODE
    } finally { $ErrorActionPreference = 'Stop' }
    if ($wintonLoginExit -eq 0) { Write-Host '[OK] Codex 本地登入狀態存在' }
    else { Write-Host '[FAIL] 請先執行 codex.cmd login 完成登入。'; $wintonFailed = $true }
}
Write-Host '此檢查不驗證網路、帳號額度、模型權限、Codex 沙箱或 Excel 實際匯出。'
if ($wintonFailed) {
    Write-Host '環境尚未齊全，請依移機使用說明補齊後重試。' -ForegroundColor Yellow
    exit 1
}
Write-Host '本機基本環境檢查通過。' -ForegroundColor Green
exit 0
