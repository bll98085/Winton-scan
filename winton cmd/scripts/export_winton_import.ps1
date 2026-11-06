# Preserve the native XLS template and write reviewed rows with hidden Excel.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Csv,
    [string]$UserCode,
    [string]$SubmissionMonth,
    [string]$DetailsJson,
    [string]$Manifest,
    [string]$SourcesJson,
    [ValidateSet('carrier', 'printed')][string]$InvoiceMode = 'carrier',
    [string]$Template,
    [string]$Output,
    [switch]$Overwrite,
    [switch]$PrepareOnly,
    [string]$ReadyFile
)
$ErrorActionPreference = 'Stop'
if (-not $PSBoundParameters.ContainsKey('Template')) { $Template = Join-Path (Split-Path -Parent $PSScriptRoot) 'assets\進項憑證明細檔.xls' }
$env:PYTHONUTF8 = '1'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$wintonPython = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
if (-not (Test-Path -LiteralPath $wintonPython)) { $wintonPython = 'python' }
$wintonBuilder = Join-Path $PSScriptRoot 'build_winton_import.py'
if (-not $UserCode) {
    $wintonUserJson = & $wintonPython $wintonBuilder --list-users
    if ($LASTEXITCODE -ne 0) { throw '無法讀取用戶選單' }
    $wintonUsers = $wintonUserJson | ConvertFrom-Json
    foreach ($wintonUser in $wintonUsers.PSObject.Properties) {
        Write-Host ($wintonUser.Name + '  ' + $wintonUser.Value)
    }
    $UserCode = Read-Host '請選擇本批用戶代號'
}
$UserCode = $UserCode.Trim().ToUpperInvariant()
if (-not $SubmissionMonth) {
    $SubmissionMonth = Read-Host '請輸入送件年月（民國年三碼＋月份兩碼，例如 11510）'
}
$SubmissionMonth = $SubmissionMonth.Trim()
if (-not $SubmissionMonth) { throw '送件年月須由使用者輸入' }
if (-not $Manifest) { throw '匯出須指定 -Manifest，以驗證來源 PDF 完整性' }
$wintonManifestPath = (Resolve-Path -LiteralPath $Manifest).Path
$wintonSourcesPath = if ($SourcesJson) { (Resolve-Path -LiteralPath $SourcesJson).Path } else { '' }
$wintonCsvPath = (Resolve-Path -LiteralPath $Csv).Path
$wintonTemplatePath = (Resolve-Path -LiteralPath $Template).Path
if ([IO.Path]::GetExtension($wintonTemplatePath) -ine '.xls') { throw '範本須為原生 .xls' }
if (-not $Output) {
    $Output = Join-Path (Split-Path -Parent $wintonCsvPath) ('進項憑證明細檔_' + (Get-Date -Format 'yyyyMMdd') + '_' + $UserCode + '.xls')
}
$wintonOutputPath = [IO.Path]::GetFullPath($Output)
if ([IO.Path]::GetExtension($wintonOutputPath) -ine '.xls') { throw '輸出副檔名須為 .xls' }
if ($wintonOutputPath -ieq $wintonTemplatePath -or $wintonOutputPath -ieq $wintonCsvPath) { throw '輸出不可覆蓋範本或 CSV' }
if ((Test-Path -LiteralPath $wintonOutputPath) -and -not $Overwrite) { throw '輸出已存在；請改檔名或在授權覆寫後使用 -Overwrite' }
if (-not (Test-Path -LiteralPath (Split-Path -Parent $wintonOutputPath))) { throw '輸出資料夾不存在' }
$wintonDetailsPath = if ($DetailsJson) { (Resolve-Path -LiteralPath $DetailsJson).Path } else { '' }
$wintonInputState = [ordered]@{
    schema_version = 2
    user_code = $UserCode
    submission_month = $SubmissionMonth
    invoice_mode = $InvoiceMode
    manifest_path = $wintonManifestPath
    manifest_sha256 = (Get-FileHash -LiteralPath $wintonManifestPath -Algorithm SHA256).Hash
    sources_path = $wintonSourcesPath
    sources_sha256 = $(if ($wintonSourcesPath) { (Get-FileHash -LiteralPath $wintonSourcesPath -Algorithm SHA256).Hash } else { '' })
    csv_path = $wintonCsvPath
    csv_sha256 = (Get-FileHash -LiteralPath $wintonCsvPath -Algorithm SHA256).Hash
    details_path = $wintonDetailsPath
    details_sha256 = $(if ($wintonDetailsPath) { (Get-FileHash -LiteralPath $wintonDetailsPath -Algorithm SHA256).Hash } else { '' })
    template_path = $wintonTemplatePath
    template_sha256 = (Get-FileHash -LiteralPath $wintonTemplatePath -Algorithm SHA256).Hash
    output_path = $wintonOutputPath
}
if ($PrepareOnly -and -not $ReadyFile) { throw '準備模式須指定 -ReadyFile，供啟動程式確認本批資料已完成' }
if ($ReadyFile) {
    $ReadyFile = [IO.Path]::GetFullPath($ReadyFile)
    if ([IO.Path]::GetExtension($ReadyFile) -ine '.json' -or
        $ReadyFile -in @($wintonCsvPath, $wintonDetailsPath, $wintonTemplatePath, $wintonOutputPath, $wintonManifestPath, $wintonSourcesPath)) {
        throw '完成標記須為獨立的 JSON 檔案，不可覆蓋輸入或輸出'
    }
    if ($PrepareOnly) {
        if (Test-Path -LiteralPath $ReadyFile) { throw '完成標記已存在，請使用新的批次暫存路徑' }
    } else {
        if (-not (Test-Path -LiteralPath $ReadyFile -PathType Leaf)) { throw '缺少本批資料完成標記，尚未建立 XLS' }
        $wintonReady = Get-Content -LiteralPath $ReadyFile -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($wintonKey in $wintonInputState.Keys) {
            if ([string]$wintonReady.$wintonKey -cne [string]$wintonInputState[$wintonKey]) {
                throw ('本批資料與完成標記不一致：' + $wintonKey + '；請重新複核，尚未建立 XLS')
            }
        }
    }
}
$wintonWorkDir = Join-Path $env:TEMP ('winton_import_' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $wintonWorkDir | Out-Null
$wintonPayloadPath = Join-Path $wintonWorkDir 'payload.json'
$wintonArguments = @($wintonBuilder, '--csv', $wintonCsvPath, '--user-code', $UserCode,
    '--submission-month', $SubmissionMonth, '--invoice-mode', $InvoiceMode, '--out', $wintonPayloadPath,
    '--manifest', $wintonManifestPath)
if ($wintonSourcesPath) { $wintonArguments += @('--sources-json', $wintonSourcesPath) }
if ($wintonDetailsPath) { $wintonArguments += @('--details-json', $wintonDetailsPath) }
& $wintonPython @wintonArguments
if ($LASTEXITCODE -ne 0) { throw '匯入資料準備失敗；尚未建立 XLS' }
$wintonPayload = Get-Content -LiteralPath $wintonPayloadPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($PrepareOnly) {
    $wintonInputState.row_count = $wintonPayload.rows.Count
    $wintonReadyBytes = [Text.Encoding]::UTF8.GetBytes(($wintonInputState | ConvertTo-Json -Depth 5))
    $wintonReadyStream = [IO.File]::Open($ReadyFile, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try { $wintonReadyStream.Write($wintonReadyBytes, 0, $wintonReadyBytes.Length) } finally { $wintonReadyStream.Dispose() }
    Write-Output ('CSV 與匯入資料已準備完成；啟動程式將接續產生 XLS。完成標記：' + $ReadyFile)
    foreach ($wintonReview in $wintonPayload.reviews) {
        Write-Output ('⚠️ 第' + $wintonReview.列號 + '列 [' + $wintonReview.號碼 + ']：' + ($wintonReview.待確認 -join '；'))
    }
    Write-Output ('複核資料：' + $wintonPayloadPath)
    return
}
$wintonTemplateCopy = Join-Path $wintonWorkDir 'template.xls'
Copy-Item -LiteralPath $wintonTemplatePath -Destination $wintonTemplateCopy
if ((Get-FileHash -LiteralPath $wintonTemplateCopy -Algorithm SHA256).Hash -cne $wintonInputState.template_sha256) {
    throw '範本在匯出前發生變更，尚未建立 XLS'
}
$wintonExcel = $null
$wintonBook = $null
$wintonSheet = $null
$wintonBooks = $null
$wintonStagedPath = Join-Path $wintonWorkDir 'verified.xls'

function Release-WintonCom($ComObject) {
    if ($null -ne $ComObject -and [Runtime.InteropServices.Marshal]::IsComObject($ComObject)) {
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($ComObject)
    }
}

try {
    $wintonExcel = New-Object -ComObject Excel.Application
    $wintonExcel.Visible = $false
    $wintonExcel.DisplayAlerts = $false
    $wintonExcel.EnableEvents = $false
    $wintonExcel.AutomationSecurity = 3
    $wintonBooks = $wintonExcel.Workbooks
    $wintonBook = $wintonBooks.Open($wintonTemplateCopy, 0, $true)
    if ($wintonBook.FileFormat -ne 56) { throw '範本不是 Excel 97–2003 XLS 格式' }
    $wintonSheet = $wintonBook.Worksheets.Item(1)
    $wintonColumnCount = $wintonPayload.headers.Count
    $wintonHeaderRange = $wintonSheet.Range('A1:AG1')
    $wintonHeadersBefore = $wintonHeaderRange.Value2
    $wintonHeaderFormats = @()
    $wintonHeaderComments = @()
    for ($wintonColumn = 1; $wintonColumn -le $wintonColumnCount; $wintonColumn++) {
        $wintonCell = $wintonSheet.Cells.Item(1, $wintonColumn)
        $wintonHeaderFormats += [string]$wintonCell.NumberFormat
        $wintonComment = $wintonCell.Comment
        $wintonHeaderComments += $(if ($null -ne $wintonComment) { [string]$wintonComment.Text() } else { $null })
        Release-WintonCom $wintonComment
        if ([string]$wintonCell.Value2 -cne [string]$wintonPayload.headers[$wintonColumn - 1]) {
            throw ('範本欄名不符：第 ' + $wintonColumn + ' 欄')
        }
        Release-WintonCom $wintonCell
    }
    Release-WintonCom $wintonHeaderRange
    $wintonLastRow = $wintonPayload.rows.Count + 1
    $wintonUsedRange = $wintonSheet.UsedRange
    $wintonUsedRows = $wintonUsedRange.Row + $wintonUsedRange.Rows.Count - 1
    Release-WintonCom $wintonUsedRange
    $wintonClearRange = $wintonSheet.Range('A2:AG' + [Math]::Max($wintonLastRow, $wintonUsedRows))
    [void]$wintonClearRange.UnMerge()
    [void]$wintonClearRange.ClearContents()
    Release-WintonCom $wintonClearRange
    # Keep row 2's template formatting for every imported row.
    if ($wintonLastRow -gt 2) {
        $wintonFormatSource = $wintonSheet.Range('A2:AG2')
        $wintonFormatTarget = $wintonSheet.Range('A3:AG' + $wintonLastRow)
        [void]$wintonFormatSource.Copy($wintonFormatTarget)
        Release-WintonCom $wintonFormatSource
        Release-WintonCom $wintonFormatTarget
    }
    foreach ($wintonColumn in $wintonPayload.text_columns) {
        $wintonRange = $wintonSheet.Range($wintonSheet.Cells.Item(2, [int]$wintonColumn), $wintonSheet.Cells.Item($wintonLastRow, [int]$wintonColumn))
        $wintonRange.NumberFormat = '@'
        Release-WintonCom $wintonRange
    }
    for ($wintonIndex = 0; $wintonIndex -lt $wintonPayload.rows.Count; $wintonIndex++) {
        for ($wintonColumn = 1; $wintonColumn -le $wintonColumnCount; $wintonColumn++) {
            $wintonValue = $wintonPayload.rows[$wintonIndex][$wintonColumn - 1]
            if ($null -eq $wintonValue -or ($wintonValue -is [string] -and $wintonValue -ceq '')) { continue }
            $wintonCell = $wintonSheet.Cells.Item($wintonIndex + 2, $wintonColumn)
            if ($wintonColumn -in $wintonPayload.text_columns) {
                $wintonCell.Value2 = [string]$wintonValue
            } else {
                $wintonCell.Value2 = [double]$wintonValue
            }
            Release-WintonCom $wintonCell
        }
    }
    $wintonBook.SaveAs($wintonStagedPath, 56)
    $wintonBook.Close($false)
    Release-WintonCom $wintonSheet
    $wintonSheet = $null
    Release-WintonCom $wintonBook
    $wintonBook = $wintonBooks.Open($wintonStagedPath, 0, $true)
    $wintonSheet = $wintonBook.Worksheets.Item(1)
    if ($wintonBook.FileFormat -ne 56) { throw '輸出不是原生 XLS' }
    for ($wintonColumn = 1; $wintonColumn -le $wintonColumnCount; $wintonColumn++) {
        $wintonCell = $wintonSheet.Cells.Item(1, $wintonColumn)
        if ([string]$wintonCell.Value2 -cne [string]$wintonHeadersBefore[1, $wintonColumn] -or
            [string]$wintonCell.NumberFormat -cne $wintonHeaderFormats[$wintonColumn - 1]) { throw '第一列驗證失敗' }
        $wintonComment = $wintonCell.Comment
        $wintonCommentText = if ($null -ne $wintonComment) { [string]$wintonComment.Text() } else { $null }
        if ($wintonCommentText -cne $wintonHeaderComments[$wintonColumn - 1]) { throw '第一列註解驗證失敗' }
        Release-WintonCom $wintonComment
        Release-WintonCom $wintonCell
    }
    for ($wintonIndex = 0; $wintonIndex -lt $wintonPayload.rows.Count; $wintonIndex++) {
        for ($wintonColumn = 1; $wintonColumn -le $wintonColumnCount; $wintonColumn++) {
            $wintonCell = $wintonSheet.Cells.Item($wintonIndex + 2, $wintonColumn)
            $wintonExpected = $wintonPayload.rows[$wintonIndex][$wintonColumn - 1]
            if ($null -eq $wintonExpected -or ($wintonExpected -is [string] -and $wintonExpected -ceq '')) {
                if ($null -ne $wintonCell.Value2 -and [string]$wintonCell.Value2 -ne '') { throw '應留白的欄位有殘留內容' }
            } elseif ($wintonColumn -in $wintonPayload.text_columns) {
                if ($wintonCell.Value2 -isnot [string] -or $wintonCell.Value2 -cne [string]$wintonExpected) { throw '文字／前導零驗證失敗' }
            } elseif ([double]$wintonCell.Value2 -ne [double]$wintonExpected) { throw '數值驗證失敗' }
            Release-WintonCom $wintonCell
        }
    }
    $wintonBook.Close($false)
    if ((Get-FileHash -LiteralPath $wintonTemplatePath -Algorithm SHA256).Hash -cne $wintonInputState.template_sha256) {
        throw '原範本在匯出期間發生變更，已保留暫存 XLS，未發布正式輸出'
    }
    [IO.File]::Copy($wintonStagedPath, $wintonOutputPath, [bool]$Overwrite)
    Write-Output ('已輸出：' + $wintonOutputPath)
    Write-Output ('用戶：' + $wintonPayload.user_code + ' ' + $wintonPayload.user_name + '；送件年月：' + $wintonPayload.submission_month + '；筆數：' + $wintonPayload.rows.Count)
    foreach ($wintonReview in $wintonPayload.reviews) {
        Write-Output ('⚠️ 第' + $wintonReview.列號 + '列 ' + $wintonReview.來源檔案 + ' [' + $wintonReview.號碼 + ']：' + ($wintonReview.待確認 -join '；'))
    }
    Write-Output ('複核資料：' + $wintonPayloadPath)
} finally {
    if ($null -ne $wintonBook) { try { $wintonBook.Close($false) } catch {} }
    if ($null -ne $wintonExcel) { try { $wintonExcel.Quit() } catch {} }
    Release-WintonCom $wintonSheet
    Release-WintonCom $wintonBook
    Release-WintonCom $wintonBooks
    Release-WintonCom $wintonExcel
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
}
