# Windows 操作、續跑與故障處理

## 環境與工具

此可攜版位於 winton cmd 資料夾，啟動器會指定本地 SKILL.md，不必另裝到 .codex；以已讀取的 SKILL.md 所在目錄為準。移機步驟見 [移機使用說明](../移機使用說明.md)。PowerShell wrapper 優先使用 `%LOCALAPPDATA%\Programs\Python\Python312\python.exe`，否則使用 PATH 的 python。必要套件：`pypdfium2`、`Pillow`、`winocr`，及 Windows `zh-Hant-TW` OCR 語言。優先使用 `rapidocr-onnxruntime`；載入失敗會明確報告改用 Windows OCR。不要自行安裝套件。

XLS 需要 Microsoft Excel 及範本 本技能資料夾內的 assets/進項憑證明細檔.xls（可用 -Template 覆寫）。啟動器會在模型作業前檢查 Excel 註冊、PDF、OCR 語言及可讀取的 XLS 範本；實際 COM 能否啟動由桌面匯出階段確認。一般 `.xlsx` 彙整才需要 openpyxl，pandas 為選用。

## 整批命令

新建暫存資料夾，把本批已選定 PDF 的絕對路徑存成 UTF-8 `sources.json` 陣列；不加入其他批次。啟動器已有來源清單時不要重建。

```powershell
$wintonRoot = '<完整路徑>\winton cmd'
powershell -NoProfile -ExecutionPolicy Bypass -File "$wintonRoot\scripts\run_batch.ps1" -SourcesJson '<workdir>\sources.json' -Template '<template.xls>' -Preflight
powershell -NoProfile -ExecutionPolicy Bypass -File "$wintonRoot\scripts\run_batch.ps1" -SourcesJson '<workdir>\sources.json' -WorkDir '<workdir>' -UserCode '<已選客戶>' -SubmissionMonth '<已輸入年月>' -CacheDir '<cache-dir>'
```

`ExecutionPolicy Bypass` 只影響此 PowerShell 程序，不改 Codex 沙箱。全批一次呼叫、單一 worker；工具已逐頁保存結果與輸出進度，不必為 15 頁分段啟動模型。shell 傳回 running session 時持續 poll，完成前不啟動第二個 OCR。

| 產物 | 用途 |
|---|---|
| `ocr/<來源檔名>_pN.txt` | 當批座標 OCR；不混入舊批次頁面 |
| `draft.csv` | 合併 14 欄草稿，UTF-8 BOM |
| `details.draft.json` | 匯入專用欄位候選值、稅額來源及文字證據；衝突號碼不自動合併 |
| `flags.json` | CSV 與匯入問題的統一裁圖清單 |
| `review.json` | 所有頁面筆數、已確認重掃及待複核資料 |
| `manifest.json` | 來源雜湊、頁數、OCR 完成狀態、解析結果及人工增減列紀錄 |

複核後從草稿另存最終 CSV 與 `details.json`。啟動器的小批存檔合併使用 `reviewed_manifest.json` 記錄最終 resolutions，原始 manifest 保留以驗證草稿；匯出必須傳入 reviewed manifest。最終 CSV 的來源欄保持 `<原檔名>.pdf p<頁碼>`，供完整性檢查使用。預設最終 CSV 名稱 `帳單_YYYYMMDD.csv`，XLS 名稱 `進項憑證明細檔_YYYYMMDD_<客戶>.xls`；啟動器若遇同名會指定唯一名稱。不可自行覆寫其他批次。

## 複核與裁圖

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File '<skill-root>\scripts\make_review_crops.ps1' '<workdir>\flags.json' '<scan-dir>' '<workdir>\crops'
```

用 `view_image` 讀裁圖。地址／用量問題可用局部圖；發票、稅額與匯入日期問題會產生整頁圖，避免只看到地址卻漏掉原單日期。多個候選值須核對該張帳單範圍，不借用同頁另一張帳單的日期或稅額。

需要單欄再辨識時可用 `ocr_region.ps1 <pdf> <page> --rot <R> --region x0,y0,x1,y1 --scale 14 --save-png <png>`。座標原點左下，依全頁 OCR 的旋轉方向。工具預設強制座標 OCR；舊 `--force-ocr` 仍接受。`--embedded-text` 僅供診斷，其結果不能交給解析器。

舊單檔入口 `run_ocr.ps1 <pdf> <outdir> [-FirstPage N] [-LastPage M]` 與 `-CountOnly` 仍可使用；正式批次請用 run_batch，取得可驗證的 manifest。`parse_bills.ps1` 保留相容，新增 `--details-json`／`--review-json`；缺座標、空 OCR 或重複傳入頁面會返回失敗，不產生部分成功 CSV。

## 增減列的來源對帳

每頁最終列數預設等於解析保留筆數。僅在有來源證據、已目視確認的情況下調整，例如確認重掃、把人工辨識的未知頁拆為兩筆，或確認某頁是無帳單內容的附頁。於 manifest 的 `resolutions` 記錄該頁最終筆數與具體理由：

```json
{
  "resolutions": {
    "台電.pdf p2": {
      "row_count": 0,
      "reason": "已目視確認與台電.pdf p1 同一張發票；發票、期間及金額均相同"
    }
  }
}
```

只修改 `resolutions`，保留來源、OCR 與解析紀錄。這不是略過未知頁的方式；尚未辨識／無法判讀的頁面仍需處理。號碼有衝突時先保留全部候選；無法判定不能任意刪除。有效的不同期帳單若違反每批號碼唯一規則，保留草稿並報告需分批。

## 快取與失敗續跑

預設快取 `%TEMP%\winton_ocr_cache`，也可由 `-CacheDir` 指定來源資料夾之外的路徑。只快取原始 OCR，不快取客戶、送件年月或人工核對結論。每筆包含 PDF SHA256、頁碼、引擎／套件版本、OCR 程式版本、裁圖參數和內容雜湊；損壞、版本變更或來源變更不沿用。

- OCR 中斷：保留 WorkDir，重跑原命令；完成頁面從快取取得，失敗頁再處理。
- 複核中斷：`review_batches/batch_NNN.done.json` 是已驗證存檔。重新雙擊、輸入相同月份與客戶，啟動器以來源／範本雜湊找出匹配批次並續跑，只開未完成小批的新對話。每批預設 5 筆，`-BatchSize` 可設 1～10（既有計畫沿用原大小）。
- 暫時性串流、連線或服務端錯誤，每小批含第一次最多嘗試 3 次，等待 10／20 秒。登入、額度、內容限制、未知錯誤、成功退出但未存檔都停止，不將失敗当作成功。若存檔後才斷線，先驗證存檔並接續下一批。
- `-ResumeWorkDir '<workdir>'` 明確指定有 `launcher.json` 的批次；`-NewBatch` 略過自動續跑。XLS 已存在的批次不再次匯出；輸入不同或草稿被修改不沿用舊複核。
- 來源、參數或程式改變：建立新 WorkDir，仍使用相同快取。未變的 OCR 可重用，但解析與複核重新執行。
- 已完成草稿被人工修改：工具拒絕重寫；繼續複核或新開 WorkDir，不清空使用者修改。
- PDF 無文字可辨識、語言／套件缺少或辨識失敗：明確停止並保存已完成頁面，不將空文字當成功。空白頁需先目視判定並依使用者範圍另行處理，不能默默跳過。
- 同時啟動多批會競爭 CPU；請一次執行一批。快取不改變來源檔案選取範圍。

## 雙擊啟動器

桌面或技能目錄的 `winton-scan.cmd` 呼叫 `start_winton_scan.ps1`。先詢問月份、再詢問客戶；`-SubmissionMonth`、`-UserCode` 可直接提供。`-ScanDir`、`-Template`、`-CacheDir` 可覆寫預設值。模型預設 `gpt-6-luna`（Luna），啟動時透過 `codex exec --model` 指定，也可用 `-Model` 覆寫；啟動摘要與 DryRun 會顯示所選模型。`-AllPdfs` 僅保留舊捷徑相容性，現在預設已選取全部 PDF。

`-DryRun` 顯示批次參數、輸出路徑及是否接續既有批次，不啟動 OCR／模型／Excel，也不寫帳單或暫存批次檔案：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File '<skill-root>\scripts\start_winton_scan.ps1' -SubmissionMonth '<已輸入年月>' -UserCode '<已選客戶>' -DryRun
```

啟動器將模型預設為 `gpt-6-luna`，其餘（包含推理強度）沿用已登入的 Codex CLI 配置，使用 `workspace-write`、approval `never`；各小批工作目錄為本批暫存資料夾，來源 PDF 僅讀取。不修改全域模型設定。資料完成後以 `-PrepareOnly` 產生綁定輸入的標記，外層桌面身份核對後匯出；不在沙箱內啟動 Excel。同一 WorkDir 使用獨占鎖避免同時執行。每批 `%TEMP%\winton_scan_*` 留存 `launcher.json`、來源清單、結果摘要與 Excel log；`review_batches/` 留存每次 prompt、JSONL 事件、stderr、退出碼、錯誤訊息（含服務提供的 request ID）及完成存檔。詳細複核格式見 [review-batches.md](review-batches.md)。維護 `.ps1` 時保存 UTF-8 BOM，`.cmd` 保存 ASCII CRLF。

## 維護驗證

`python scripts/test_workflow.py` 在臨時目錄驗證稅額、去重、文字層／漏頁、來源完整性、匯入欄位與快取。`python scripts/test_review_checkpoint.py` 以假 Codex 驗證分批存檔、斷線重試、上限、續跑及不重做；不呼叫 API。另以技能建立工具的 quick_validate 檢查 SKILL.md。涉及引擎或 COM 的修改應再用臨時測試 PDF 與原生 XLS 匯出驗證，不拿正式帳單做自動重跑測試。
