# Windows wrapper for the bundled winton-scan Python script.
#
#   make_review_crops.ps1 flags.json <pdf_dir> <outdir>
$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$script = Join-Path $PSScriptRoot "make_review_crops.py"
$python = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
if (-not (Test-Path $python)) { $python = "python" }
& $python $script @args
exit $LASTEXITCODE
