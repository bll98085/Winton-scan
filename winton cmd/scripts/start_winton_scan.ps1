# Double-click entry point. Keep this file UTF-8 with BOM for Windows PowerShell 5.1.
[CmdletBinding()]
param(
    [string]$SubmissionMonth,
    [string]$UserCode,
    [ValidateNotNullOrEmpty()][string]$Model = 'gpt-6-luna',
    [string]$ScanDir,
    # Kept for existing shortcuts; all PDFs are now selected by default.
    [switch]$AllPdfs,
    [string]$Template,
    [string]$CacheDir = (Join-Path $env:TEMP 'winton_ocr_cache'),
    [string]$ResumeWorkDir,
    [switch]$NewBatch,
    [ValidateRange(1, 10)][int]$BatchSize = 5,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$wintonUtf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $wintonUtf8
[Console]::OutputEncoding = $wintonUtf8
$OutputEncoding = $wintonUtf8
$env:PYTHONUTF8 = '1'
$wintonSkillRoot = Split-Path -Parent $PSScriptRoot
if (-not $PSBoundParameters.ContainsKey('ScanDir')) { $ScanDir = Split-Path -Parent $wintonSkillRoot }
if (-not $PSBoundParameters.ContainsKey('Template')) { $Template = Join-Path $wintonSkillRoot 'assets\進項憑證明細檔.xls' }
$wintonWorkDir = $null
$wintonLock = $null

function Read-WintonInput([string]$Prompt) {
    if ([Console]::IsInputRedirected) {
        Write-Host ($Prompt + ': ') -NoNewline
        $wintonLine = [Console]::ReadLine()
        if ($null -eq $wintonLine) { throw '輸入已結束，作業取消。' }
        return $wintonLine
    }
    return (Read-Host $Prompt)
}

function Move-WintonCompletedFile([string]$Source, [string]$DestinationDirectory) {
    if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) { return $Source }
    if (-not (Test-Path -LiteralPath $DestinationDirectory -PathType Container)) {
        New-Item -ItemType Directory -Path $DestinationDirectory | Out-Null
    }
    $wintonDestination = Join-Path $DestinationDirectory ([IO.Path]::GetFileName($Source))
    if (Test-Path -LiteralPath $wintonDestination) {
        $wintonStem = [IO.Path]::GetFileNameWithoutExtension($Source)
        $wintonExtension = [IO.Path]::GetExtension($Source)
        do {
            $wintonUniqueName = $wintonStem + '_' + [guid]::NewGuid().ToString('N').Substring(0, 8) + $wintonExtension
            $wintonDestination = Join-Path $DestinationDirectory $wintonUniqueName
        } while (Test-Path -LiteralPath $wintonDestination)
    }
    [IO.File]::Move($Source, $wintonDestination)
    return $wintonDestination
}

function Assert-WintonFileMovable([string]$Path) {
    $wintonProbe = $null
    try {
        $wintonProbe = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    } catch {
        throw ('檔案正在使用中或無法移動，請先關閉後再執行：' + $Path)
    } finally {
        if ($wintonProbe) { $wintonProbe.Dispose() }
    }
}

try {
    if ($ResumeWorkDir -and $NewBatch) { throw '-ResumeWorkDir 與 -NewBatch 不能同時使用。' }
    if ($ResumeWorkDir) {
        $wintonResume = Get-Content -LiteralPath (Join-Path $ResumeWorkDir 'launcher.json') -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($wintonPair in @(@('UserCode', 'user_code'), @('SubmissionMonth', 'submission_month'), @('ScanDir', 'scan_dir'), @('Template', 'template'))) {
            if ($PSBoundParameters.ContainsKey($wintonPair[0]) -and (Get-Variable -Name $wintonPair[0] -ValueOnly) -ine $wintonResume.($wintonPair[1])) {
                throw ('續跑參數與原批次不一致：' + $wintonPair[0])
            }
        }
        $UserCode = $wintonResume.user_code
        $SubmissionMonth = $wintonResume.submission_month
        $ScanDir = $wintonResume.scan_dir
        $Template = $wintonResume.template
        $CacheDir = $wintonResume.cache_dir
        if (-not $PSBoundParameters.ContainsKey('Model')) { $Model = $wintonResume.model }
    }
    Write-Host 'Winton-scan 帳單辨識與文中匯入' -ForegroundColor Cyan
    Write-Host ''

    # Ask for the keyin month first; it is the import submission month, not a PDF date filter.
    $wintonMonthSupplied = $PSBoundParameters.ContainsKey('SubmissionMonth') -or [bool]$ResumeWorkDir
    while ($true) {
        if (-not $wintonMonthSupplied) {
            $SubmissionMonth = Read-WintonInput '請輸入 keyin 月份／送件年月（民國年三碼＋月份兩碼，例如 11510）'
        }
        $SubmissionMonth = $SubmissionMonth.Trim()
        if ($SubmissionMonth -cmatch '^(?!000)[0-9]{3}(0[1-9]|1[0-2])$') { break }
        if ($wintonMonthSupplied) { throw '月份格式錯誤：請輸入五碼民國年月，例如 11510。' }
        Write-Host '月份格式錯誤，請輸入五碼民國年月，月份須為 01～12。' -ForegroundColor Yellow
    }

    # Reuse the import helper's customer list so the menu and exporter cannot diverge.
    $wintonPython = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
    if (-not (Test-Path -LiteralPath $wintonPython -PathType Leaf)) {
        $wintonPython = (Get-Command python.exe -CommandType Application -ErrorAction Stop).Source
    }
    $wintonUserJson = & $wintonPython (Join-Path $PSScriptRoot 'build_winton_import.py') --list-users
    if ($LASTEXITCODE -ne 0) { throw '無法讀取客戶清單。' }
    $wintonUsers = ($wintonUserJson -join "`n") | ConvertFrom-Json
    $wintonUserSupplied = $PSBoundParameters.ContainsKey('UserCode') -or [bool]$ResumeWorkDir
    if (-not $wintonUserSupplied) {
        Write-Host ''
        foreach ($wintonUser in $wintonUsers.PSObject.Properties) {
            Write-Host ('  {0,-8} {1}' -f $wintonUser.Name, $wintonUser.Value)
        }
        Write-Host ''
    }
    while ($true) {
        if (-not $wintonUserSupplied) { $UserCode = Read-WintonInput '請輸入客戶代號' }
        $UserCode = $UserCode.Trim().ToUpperInvariant()
        if ($UserCode -in $wintonUsers.PSObject.Properties.Name) { break }
        if ($wintonUserSupplied) { throw '客戶代號不在清單中。' }
        Write-Host '查無此客戶代號，請依上方清單重新輸入。' -ForegroundColor Yellow
    }
    $wintonUserName = $wintonUsers.$UserCode

    if (-not (Test-Path -LiteralPath $ScanDir -PathType Container)) {
        throw ('找不到掃描資料夾：' + $ScanDir)
    }
    $ScanDir = (Resolve-Path -LiteralPath $ScanDir).Path
    $wintonToday = (Get-Date).Date
    $wintonScannedPdfDir = Join-Path $ScanDir '已掃描PDF'
    $wintonPdfs = @(Get-ChildItem -LiteralPath $ScanDir -File -Filter '*.pdf' | Sort-Object Name)
    if ($wintonPdfs.Count -eq 0) {
        Write-Host ('資料夾內沒有尚未處理的 PDF，未執行辨識：' + $ScanDir) -ForegroundColor Yellow
        exit 0
    }
    foreach ($wintonPdf in $wintonPdfs) { Assert-WintonFileMovable $wintonPdf.FullName }


    $Template = (Resolve-Path -LiteralPath $Template).Path
    $wintonTemplateHash = (Get-FileHash -LiteralPath $Template -Algorithm SHA256).Hash
    $wintonSourceSignature = ConvertTo-Json -InputObject @($wintonPdfs | ForEach-Object {
        [ordered]@{ path = $_.FullName; sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash }
    }) -Compress
    if (-not $NewBatch -and -not $ResumeWorkDir) {
        $wintonCandidates = @(Get-ChildItem -LiteralPath $env:TEMP -Directory -Filter 'winton_scan_*' |
            Sort-Object LastWriteTime -Descending)
        foreach ($wintonDirectory in $wintonCandidates) {
            $wintonMetadataPath = Join-Path $wintonDirectory.FullName 'launcher.json'
            if (-not (Test-Path -LiteralPath $wintonMetadataPath)) { continue }
            try { $wintonCandidateMeta = Get-Content -LiteralPath $wintonMetadataPath -Raw -Encoding UTF8 | ConvertFrom-Json } catch { continue }
            if ($wintonCandidateMeta.schema_version -eq 1 -and
                $wintonCandidateMeta.user_code -eq $UserCode -and $wintonCandidateMeta.submission_month -eq $SubmissionMonth -and
                $wintonCandidateMeta.scan_dir -ieq $ScanDir -and $wintonCandidateMeta.template -ieq $Template -and
                $wintonCandidateMeta.template_sha256 -eq $wintonTemplateHash -and
                $wintonCandidateMeta.source_signature -ceq $wintonSourceSignature -and
                -not (Test-Path -LiteralPath (Join-Path $wintonDirectory.FullName 'completed.json')) -and
                -not (Test-Path -LiteralPath $wintonCandidateMeta.xls)) {
                $ResumeWorkDir = $wintonDirectory.FullName
                $wintonResume = $wintonCandidateMeta
                break
            }
        }
    }
    if ($ResumeWorkDir) {
        if ($wintonResume.schema_version -ne 1 -or $wintonResume.source_signature -cne $wintonSourceSignature -or
            $wintonResume.template_sha256 -ne $wintonTemplateHash) {
            throw '原批次來源或範本已變更，不能沿用；請用 -NewBatch 建立新批次。'
        }
        $wintonWorkDir = (Resolve-Path -LiteralPath $ResumeWorkDir).Path
        if ($wintonResume.work_dir -ine $wintonWorkDir) { throw '批次位置已變更，不能沿用。' }
        $wintonCsv = $wintonResume.csv
        $wintonXls = $wintonResume.xls
        $CacheDir = $wintonResume.cache_dir
        if (Test-Path -LiteralPath $wintonXls) { throw ('本批已有 XLS，不重複匯出：' + $wintonXls) }
    } else {
        $wintonDateTag = $wintonToday.ToString('yyyyMMdd')
        $wintonCsv = Join-Path $ScanDir ('帳單_' + $wintonDateTag + '.csv')
        $wintonXls = Join-Path $ScanDir ('進項憑證明細檔_' + $wintonDateTag + '_' + $UserCode + '.xls')
        while ((Test-Path -LiteralPath $wintonCsv) -or (Test-Path -LiteralPath $wintonXls)) {
            $wintonSuffix = $wintonDateTag + '_' + $UserCode + '_' + [guid]::NewGuid().ToString('N').Substring(0, 8)
            $wintonCsv = Join-Path $ScanDir ('帳單_' + $wintonSuffix + '.csv')
            $wintonXls = Join-Path $ScanDir ('進項憑證明細檔_' + $wintonSuffix + '.xls')
        }
        $wintonWorkDir = Join-Path $env:TEMP ('winton_scan_' + [guid]::NewGuid().ToString('N'))
    }
    $wintonSummary = Join-Path $wintonWorkDir 'result.txt'
    $wintonDetails = Join-Path $wintonWorkDir 'details.json'
    $wintonReadyFile = Join-Path $wintonWorkDir 'export_ready.json'
    $wintonSourcesFile = Join-Path $wintonWorkDir 'sources.json'
    $wintonManifest = Join-Path $wintonWorkDir 'reviewed_manifest.json'
    $wintonSourceJson = ConvertTo-Json -InputObject @($wintonPdfs.FullName) -Compress
    $wintonMetadata = [ordered]@{
        schema_version = 1; user_code = $UserCode; submission_month = $SubmissionMonth;
        scan_dir = $ScanDir; template = $Template; template_sha256 = $wintonTemplateHash;
        cache_dir = $CacheDir; model = $Model; csv = $wintonCsv; xls = $wintonXls;
        work_dir = $wintonWorkDir; source_signature = $wintonSourceSignature
    }
    Write-Host ('模型：' + $Model)
    Write-Host ('送件年月：' + $SubmissionMonth + '；客戶：' + $UserCode + ' ' + $wintonUserName)
    Write-Host '處理範圍：來源資料夾內全部 PDF（不限掃描或修改日期，不含子資料夾）'
    foreach ($wintonPdf in $wintonPdfs) { Write-Host ('  ' + $wintonPdf.Name) }
    Write-Host ('每批最多 ' + $BatchSize + ' 筆複核，完成即存檔。')
    if ($ResumeWorkDir) { Write-Host ('接續未完成批次：' + $wintonWorkDir) -ForegroundColor Cyan }
    if ($DryRun) {
        Write-Host '測試模式：不啟動 OCR／模型／Excel，也不寫入批次資料。'
        Write-Output ($wintonMetadata | ConvertTo-Json -Depth 5)
        exit 0
    }
    if (-not (Test-Path -LiteralPath $wintonWorkDir)) { New-Item -ItemType Directory -Path $wintonWorkDir | Out-Null }
    $wintonLock = [IO.File]::Open((Join-Path $wintonWorkDir 'launcher.lock'), [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    if (-not $ResumeWorkDir) {
        [IO.File]::WriteAllText((Join-Path $wintonWorkDir 'launcher.json'), ($wintonMetadata | ConvertTo-Json -Depth 5), $wintonUtf8)
        [IO.File]::WriteAllText($wintonSourcesFile, $wintonSourceJson, $wintonUtf8)
    }
    if ($null -eq [Type]::GetTypeFromProgID('Excel.Application')) { throw '找不到 Microsoft Excel，尚未開始處理。' }
    Write-Host ('執行記錄：' + $wintonWorkDir)
    if (Test-Path -LiteralPath $wintonReadyFile) {
        $wintonMissingPreparedFiles = @($wintonCsv, $wintonDetails) | Where-Object { -not (Test-Path -LiteralPath $_ -PathType Leaf) }
        if ($wintonMissingPreparedFiles.Count -gt 0) {
            Write-Host '已完成的複核資料缺少匯出檔，正在驗證並重建……' -ForegroundColor Cyan
            & $wintonPython (Join-Path $PSScriptRoot 'review_checkpoint.py') materialize --workdir $wintonWorkDir `
                --csv $wintonCsv --details $wintonDetails --recover
            if ($LASTEXITCODE -ne 0) { throw '已完成批次的 CSV／明細無法安全重建。' }
        }
    }
    if (-not (Test-Path -LiteralPath $wintonReadyFile)) {
        & (Join-Path $PSScriptRoot 'run_batch.ps1') -SourcesJson $wintonSourcesFile -WorkDir $wintonWorkDir `
            -CacheDir $CacheDir -UserCode $UserCode -SubmissionMonth $SubmissionMonth -Template $Template
        $wintonFlags = Get-Content -LiteralPath (Join-Path $wintonWorkDir 'flags.json') -Raw -Encoding UTF8 | ConvertFrom-Json
        $wintonNeedCrops = $false
        for ($wintonIndex = 0; $wintonIndex -lt $wintonFlags.Count; $wintonIndex++) {
            $wintonFlag = $wintonFlags[$wintonIndex]
            $wintonNumber = if ($wintonFlag.number) { $wintonFlag.number } elseif ($wintonFlag.type) { $wintonFlag.type } else { 'row' }
            $wintonCropName = 'crop_{0}_{1}_p{2}_{3}.png' -f ($wintonIndex + 1), ([IO.Path]::GetFileNameWithoutExtension($wintonFlag.source)), $wintonFlag.page, $wintonNumber.Replace('/', '-')
            if (-not (Test-Path -LiteralPath (Join-Path $wintonWorkDir ('crops\' + $wintonCropName)))) { $wintonNeedCrops = $true; break }
        }
        if ($wintonNeedCrops) {
            & $wintonPython (Join-Path $PSScriptRoot 'make_review_crops.py') (Join-Path $wintonWorkDir 'flags.json') $ScanDir (Join-Path $wintonWorkDir 'crops') |
                Out-File -LiteralPath (Join-Path $wintonWorkDir 'crops.log') -Encoding utf8
            if ($LASTEXITCODE -ne 0) { throw '複核裁圖失敗，尚未啟動模型；詳見 crops.log。' }
        }
        $wintonCodex = Get-Command codex.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $wintonCodex) { $wintonCodex = Get-Command codex.cmd -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1 }
        if (-not $wintonCodex) { throw '找不到 Codex CLI，請先完成安裝與登入。' }
        & (Join-Path $PSScriptRoot 'run_review.ps1') -WorkDir $wintonWorkDir -ScanDir $ScanDir `
            -CodexPath $wintonCodex.Source -PythonPath $wintonPython -Model $Model -BatchSize $BatchSize
        & $wintonPython (Join-Path $PSScriptRoot 'review_checkpoint.py') materialize --workdir $wintonWorkDir --csv $wintonCsv --details $wintonDetails
        if ($LASTEXITCODE -ne 0) { throw '複核合併或完整性檢查失敗，保留全部小批成果，未匯出。' }
        & (Join-Path $PSScriptRoot 'export_winton_import.ps1') -Csv $wintonCsv -UserCode $UserCode `
            -SubmissionMonth $SubmissionMonth -DetailsJson $wintonDetails -Manifest $wintonManifest `
            -SourcesJson $wintonSourcesFile -Template $Template -Output $wintonXls -PrepareOnly -ReadyFile $wintonReadyFile
    }
    Write-Host '資料已備妥，正在產生 Excel 匯入檔……' -ForegroundColor Cyan
    & (Join-Path $PSScriptRoot 'export_winton_import.ps1') -Csv $wintonCsv -UserCode $UserCode `
        -SubmissionMonth $SubmissionMonth -DetailsJson $wintonDetails -Manifest $wintonManifest `
        -SourcesJson $wintonSourcesFile -Template $Template -Output $wintonXls -ReadyFile $wintonReadyFile |
        Tee-Object -FilePath (Join-Path $wintonWorkDir 'excel_export.log')
    if (-not (Test-Path -LiteralPath $wintonXls)) { throw 'Excel 匯出未產生 XLS，已保留複核資料。' }
    $wintonImportDir = Join-Path $ScanDir 'Excel及匯入檔'
    foreach ($wintonFileToMove in @($wintonCsv, $wintonXls) + @($wintonPdfs.FullName)) {
        Assert-WintonFileMovable $wintonFileToMove
    }
    $wintonFinalCsv = Move-WintonCompletedFile $wintonCsv $wintonImportDir
    $wintonFinalXls = Move-WintonCompletedFile $wintonXls $wintonImportDir
    $wintonArchivedPdfs = @()
    foreach ($wintonPdf in $wintonPdfs) {
        $wintonArchivedPdfs += Move-WintonCompletedFile $wintonPdf.FullName $wintonScannedPdfDir
    }
    $wintonCompleted = [ordered]@{
        schema_version = 1; completed_at = (Get-Date).ToString('o');
        csv = $wintonFinalCsv; xls = $wintonFinalXls; archived_sources = @($wintonArchivedPdfs)
    }
    [IO.File]::WriteAllText((Join-Path $wintonWorkDir 'completed.json'), ($wintonCompleted | ConvertTo-Json -Depth 5), $wintonUtf8)
    $wintonReviewSummary = Get-Content -LiteralPath (Join-Path $wintonWorkDir 'review_summary.txt') -Raw -Encoding UTF8
    $wintonCompletion = $wintonReviewSummary + "`r`nCSV 與原生 XLS 已完成，已通過 33 欄範本與逐格資料驗證。`r`nCSV：$wintonFinalCsv`r`nXLS：$wintonFinalXls`r`n已掃描 PDF：$wintonScannedPdfDir`r`n"
    [IO.File]::WriteAllText($wintonSummary, $wintonCompletion, $wintonUtf8)
    Write-Host $wintonCompletion -ForegroundColor Green
    Write-Host ('結果摘要：' + $wintonSummary)
    exit 0
} catch {
    $wintonFailure = $_.Exception.Message
    Write-Host ('作業未完成：' + $wintonFailure) -ForegroundColor Red
    if ($wintonWorkDir -and $wintonLock -and (Test-Path -LiteralPath $wintonWorkDir)) {
        [IO.File]::AppendAllText((Join-Path $wintonWorkDir 'result.txt'), ((Get-Date).ToString('o') + ' 作業未完成：' + $wintonFailure + "`r`n"), $wintonUtf8)
        Write-Host ('進度已保留：' + $wintonWorkDir)
        Write-Host '再次雙擊並輸入相同月份、客戶，將自動接續未完成批次。'
    }
    exit 1
} finally {
    if ($wintonLock) { $wintonLock.Dispose() }
}
