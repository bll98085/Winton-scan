# Sequential, fresh Codex sessions keep image context bounded. UTF-8 BOM required.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$WorkDir,
    [Parameter(Mandatory = $true)][string]$ScanDir,
    [Parameter(Mandatory = $true)][string]$CodexPath,
    [Parameter(Mandatory = $true)][string]$PythonPath,
    [string]$Model = 'gpt-6-luna',
    [ValidateRange(1, 10)][int]$BatchSize = 5,
    [ValidateRange(1, 3)][int]$MaxAttempts = 3,
    [ValidateRange(0, 60)][int]$RetryDelaySeconds = 10
)
$ErrorActionPreference = 'Stop'
$wintonUtf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $wintonUtf8
[Console]::OutputEncoding = $wintonUtf8
$OutputEncoding = $wintonUtf8
$env:PYTHONUTF8 = '1'
$wintonHelper = Join-Path $PSScriptRoot 'review_checkpoint.py'
$wintonSkillRoot = Split-Path -Parent $PSScriptRoot

function Read-WintonProgress {
    $wintonOutput = & $PythonPath $wintonHelper status --workdir $WorkDir
    if ($LASTEXITCODE -ne 0) { throw '複核進度驗證失敗，已保留所有檔案。' }
    return (($wintonOutput -join "`n") | ConvertFrom-Json)
}

& $PythonPath $wintonHelper init --workdir $WorkDir --size $BatchSize
if ($LASTEXITCODE -ne 0) { throw '無法建立或接續複核計畫。' }
$wintonProgress = Read-WintonProgress
while ($null -ne $wintonProgress.next_batch) {
    $wintonBatch = [int]$wintonProgress.next_batch
    $wintonInputOutput = & $PythonPath $wintonHelper input --workdir $WorkDir --batch $wintonBatch
    if ($LASTEXITCODE -ne 0) { throw '無法產生本批複核資料。' }
    $wintonInput = (($wintonInputOutput -join "`n") | ConvertFrom-Json).input
    $wintonCandidate = Join-Path $WorkDir ('review_batches\batch_{0:000}.candidate.json' -f $wintonBatch)
    $wintonPrompt = @"
請使用 `$winton-scan：$wintonSkillRoot\SKILL.md。
這是已完成 OCR 的分批複核工作；只處理本批 input JSON 的 records：
$wintonInput
只需讀取 references/bill-fields.md、references/winton-import.md 與 references/review-batches.md。
客戶與送件年月已由使用者確認，見 input；不再詢問。
先讀 input，每次最多查看兩張所列裁圖；只有看不清楚時才用 original。
只讀本批相關 OCR 頁面，不載入整批 manifest、review、flags 或其他批次圖片。
每筆同時核對 CSV 與匯入欄位。未知欄位留白，保留待確認原因，不猜測。
不要執行 OCR、啟動 Excel、匯出、建立完成標記、修改來源／草稿／其他批次或 skill，也不要啟動子代理。
依 review-batches.md 格式把本批全部 records 的修正與證據寫至：
$wintonCandidate
完成後必須用以下命令驗證並存檔（PowerShell 以 & 呼叫；所有檔案用 UTF-8）：
& '$PythonPath' '$wintonHelper' commit --workdir '$WorkDir' --batch $wintonBatch --candidate '$wintonCandidate'
確認返回碼為 0 才回報「本小批複核已存檔」。不繼續其他批次。
若無法完成安全的原單判定，停止並說明原因，不將未完成工作標記為已完成。
"@
    $wintonBatchSaved = $false
    for ($wintonAttempt = 1; $wintonAttempt -le $MaxAttempts; $wintonAttempt++) {
        $wintonAttemptTag = 'batch_{0:000}_attempt_{1}_{2}' -f $wintonBatch, $wintonAttempt, ([guid]::NewGuid().ToString('N').Substring(0, 8))
        $wintonLog = Join-Path $WorkDir ('review_batches\' + $wintonAttemptTag + '.jsonl')
        $wintonErrorLog = Join-Path $WorkDir ('review_batches\' + $wintonAttemptTag + '.stderr.log')
        $wintonMessage = Join-Path $WorkDir ('review_batches\' + $wintonAttemptTag + '.result.txt')
        [IO.File]::WriteAllText((Join-Path $WorkDir ('review_batches\' + $wintonAttemptTag + '.prompt.txt')), $wintonPrompt, $wintonUtf8)
        Write-Host ('複核第 {0}/{1} 批，已完成 {2} 批；本批第 {3}/{4} 次嘗試。' -f $wintonBatch, $wintonProgress.total_batches, $wintonProgress.completed_batches, $wintonAttempt, $MaxAttempts) -ForegroundColor Cyan
        $wintonArguments = @('-a', 'never', 'exec', '--model', $Model, '--sandbox', 'workspace-write',
            '--skip-git-repo-check', '--cd', $WorkDir, '--json', '--output-last-message', $wintonMessage, '-')
        $wintonLogWriter = New-Object IO.StreamWriter($wintonLog, $false, $wintonUtf8)
        $wintonErrorWriter = New-Object IO.StreamWriter($wintonErrorLog, $false, $wintonUtf8)
        try {
            # PS 5.1 wraps native stderr as ErrorRecord; preserve it without aborting the pipeline.
            $ErrorActionPreference = 'Continue'
            $wintonPrompt | & $CodexPath @wintonArguments 2>&1 | ForEach-Object {
                if ($_ -is [System.Management.Automation.ErrorRecord]) {
                    $wintonErrorWriter.WriteLine($_.ToString())
                    $wintonErrorWriter.Flush()
                } else {
                    $wintonLine = [string]$_
                    $wintonLogWriter.WriteLine($wintonLine)
                    $wintonLogWriter.Flush()
                    try {
                        $wintonEvent = $wintonLine | ConvertFrom-Json -ErrorAction Stop
                        if ($wintonEvent.type -eq 'error' -or $wintonEvent.type -eq 'turn.failed') {
                            Write-Host $wintonLine -ForegroundColor Yellow
                        } elseif ($wintonEvent.type -eq 'item.completed' -and $wintonEvent.item.type -eq 'agent_message') {
                            Write-Host $wintonEvent.item.text
                        }
                    } catch { Write-Host $wintonLine }
                }
            }
            $wintonExitCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = 'Stop'
            $wintonLogWriter.Dispose()
            $wintonErrorWriter.Dispose()
        }
        $wintonAfter = Read-WintonProgress
        $wintonRetryOutput = & $PythonPath $wintonHelper retryable --logs $wintonLog $wintonErrorLog
        if ($LASTEXITCODE -ne 0) { throw '無法辨別 Codex 錯誤，已停止自動重試。' }
        $wintonRetry = (($wintonRetryOutput -join "`n") | ConvertFrom-Json)
        $wintonAttemptInfo = [ordered]@{ batch = $wintonBatch; attempt = $wintonAttempt; exit_code = $wintonExitCode;
            time = (Get-Date).ToString('o'); log = $wintonLog; stderr = $wintonErrorLog;
            retryable = $wintonRetry.retryable; error = $wintonRetry.error; completed_batches = $wintonAfter.completed_batches }
        [IO.File]::WriteAllText((Join-Path $WorkDir ('review_batches\' + $wintonAttemptTag + '.status.json')), ($wintonAttemptInfo | ConvertTo-Json -Depth 6), $wintonUtf8)
        # A committed checkpoint survives a later disconnect while sending the final message.
        if ($wintonAfter.completed_batches -gt $wintonProgress.completed_batches) {
            if ($wintonExitCode -ne 0 -and -not $wintonRetry.retryable) {
                throw ('本小批已存檔，但 Codex 回報不可自動重試的錯誤，停止後續批次。記錄：' + $wintonLog)
            }
            $wintonProgress = $wintonAfter
            $wintonBatchSaved = $true
            break
        }
        if ($wintonExitCode -eq 0) { throw ('Codex 已結束但本批未存檔，停止匯出。詳見：' + $wintonMessage) }
        if (-not $wintonRetry.retryable -or $wintonAttempt -eq $MaxAttempts) {
            throw ('第 ' + $wintonBatch + ' 批未完成，Codex 結束代碼：' + $wintonExitCode + '。已保存先前進度；錯誤記錄：' + $wintonLog)
        }
        $wintonDelay = [Math]::Min(60, $RetryDelaySeconds * $wintonAttempt)
        Write-Host ('暫時性連線／服務錯誤，' + $wintonDelay + ' 秒後以新對話接續本小批。') -ForegroundColor Yellow
        Start-Sleep -Seconds $wintonDelay
    }
    if (-not $wintonBatchSaved) { throw '本批未完成，已停止。' }
}
Write-Host ('全部 ' + $wintonProgress.total_rows + ' 筆複核已存檔。') -ForegroundColor Green
