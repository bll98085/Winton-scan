# Windows wrapper for the bundled winton-scan Python script.
# Spot re-OCR of a small region at high DPI (Windows replacement for ocrbin):
#
#   ocr_region.ps1 <pdf> <page> --rot 270 --region 0.5,0.6,1.0,0.75 --scale 14
$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$script = Join-Path $PSScriptRoot "ocr_page.py"
$python = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
if (-not (Test-Path $python)) { $python = "python" }
& $python $script @args
exit $LASTEXITCODE
