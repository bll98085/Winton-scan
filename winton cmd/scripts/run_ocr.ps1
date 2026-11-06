# OCR every page of a scanned bill PDF into per-page text files.
#
#   run_ocr.ps1 <pdf> <outdir> [-FirstPage N] [-LastPage M]
#   run_ocr.ps1 <pdf> -CountOnly          # print the page count and exit
#
# Produces <outdir>\<pdf-basename>_pN.txt (UTF-8) for pages FirstPage..LastPage
# (defaults: the whole PDF). The whole range runs in ONE python process so the
# OCR models load only once; ocr_page.py prints "  page N done" to stderr
# after each page. Windows port of run_ocr.sh; uses ocr_page.py (pypdfium2 +
# Windows.Media.Ocr + RapidOCR). NOTE: OCR is CPU-bound and onnxruntime
# already uses every core — run ONE worker at a time on this machine
# (measured 2026-07-15 on the 4-core i3: two parallel workers were slower
# in total than one).
param(
    [Parameter(Mandatory = $true)][string]$Pdf,
    [Parameter(Mandatory = $false)][string]$OutDir,
    [int]$FirstPage = 1,
    [int]$LastPage = 0,
    [switch]$CountOnly
)
$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$script = Join-Path $PSScriptRoot "ocr_page.py"
$python = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
if (-not (Test-Path $python)) { $python = "python" }

$pages = & $python $script $Pdf --count
if ($LASTEXITCODE -ne 0) { throw "ocr_page.py --count failed for $Pdf" }
if ($CountOnly) { Write-Output $pages; exit 0 }
if (-not $OutDir) { throw "OutDir is required unless -CountOnly is given" }

New-Item -ItemType Directory -Force $OutDir | Out-Null
if ($FirstPage -lt 1) { $FirstPage = 1 }
if ($LastPage -le 0 -or $LastPage -gt [int]$pages) { $LastPage = [int]$pages }
[Console]::Error.WriteLine("${Pdf}: pages $FirstPage..$LastPage of $pages")
& $python $script $Pdf "$FirstPage-$LastPage" --out-dir $OutDir
if ($LASTEXITCODE -ne 0) { throw "ocr_page.py failed in pages $FirstPage-$LastPage of $Pdf" }
