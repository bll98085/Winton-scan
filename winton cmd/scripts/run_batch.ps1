# One process owns all OCR models for the batch; keep this wrapper UTF-8 with BOM.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$SourcesJson,
    [string]$WorkDir,
    [string]$CacheDir = (Join-Path $env:TEMP 'winton_ocr_cache'),
    [string]$UserCode,
    [string]$SubmissionMonth,
    [string]$Template,
    [switch]$Preflight
)
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$wintonPython = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
if (-not (Test-Path -LiteralPath $wintonPython)) { $wintonPython = 'python' }
$wintonArgs = @((Join-Path $PSScriptRoot 'run_batch.py'), '--sources-json', $SourcesJson, '--cache-dir', $CacheDir)
if ($WorkDir) { $wintonArgs += @('--workdir', $WorkDir) }
if ($UserCode) { $wintonArgs += @('--user-code', $UserCode) }
if ($SubmissionMonth) { $wintonArgs += @('--submission-month', $SubmissionMonth) }
if ($Template) { $wintonArgs += @('--template', $Template) }
if ($Preflight) { $wintonArgs += '--preflight' }
& $wintonPython @wintonArgs
if ($LASTEXITCODE -ne 0) { throw ('批次處理失敗，結束代碼：' + $LASTEXITCODE) }
