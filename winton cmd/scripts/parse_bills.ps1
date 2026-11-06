# Windows wrapper for the bundled winton-scan Python script.
#
#   parse_bills.ps1 --refine <pdf_dir> --flags-json flags.json -o draft.csv <workdir>\*.txt
#
# All arguments are passed straight to parse_bills.py; wildcards in file
# arguments are expanded here (python on Windows does not glob).
$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$script = Join-Path $PSScriptRoot "parse_bills.py"
$python = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
if (-not (Test-Path $python)) { $python = "python" }

$expanded = @()
foreach ($a in $args) {
    if ($a -match '[*?]' -and (Resolve-Path $a -ErrorAction SilentlyContinue)) {
        $expanded += (Resolve-Path $a | ForEach-Object { $_.Path })
    } else {
        $expanded += $a
    }
}
& $python $script @expanded
exit $LASTEXITCODE
