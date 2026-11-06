---
name: winton-scan
description: Recognize scanned Taipower, Taiwan Water, and FET bills; review extracted fields and create one consolidated CSV plus a template-preserving Winton XLS import. Use for utility bills, telecom bills, payment receipts, and Winton invoice imports.
---

# Winton 帳單辨識與匯入

處理台電、台水及遠傳 PDF，每張帳單一列，輸出一份 14 欄合併 CSV 與原生文中進項憑證 `.xls`。一頁可能有多張帳單。結果、欄位內容與待確認事項使用繁體中文；中文欄名與識別碼不得翻譯。

## 本批輸入與邊界

- 先取得 **keyin 月份／送件年月**（五碼民國年月，如 `11510`），再取得 **客戶代號／用戶代號**。用 `build_winton_import.py --list-users` 顯示既有客戶選單。接受本批已提供、已驗證的值，不再詢問；不以當月或前批選擇補值。
- 可攜版預設來源為本技能資料夾的上一層（例如 Winton-scan/winton cmd 的上一層 Winton-scan）；啟動器傳入 -ScanDir 時以該路徑為準，選取直接位於資料夾內的全部 PDF，不依掃描／建立／修改日期篩選，不遞迴子資料夾。使用者指定的路徑、清單及範圍優先；無 PDF 就說明並停止。整批歸屬已選定的客戶。
- 使用此技能自身 `scripts/` 中的工具。保留 explicit-only invocation 設定。Windows 環境與手動命令見 [references/operations.md](references/operations.md)。不自行安裝套件或替換環境。
- CSV、XLS 放來源資料夾，保留已有輸出；啟動器提供的精確路徑優先。暫存資料與快取放來源資料夾之外。原 PDF 與 XLS 範本不修改。

## 流程

**收到啟動器指定的 `batch_NNN.input.json` 時**，使用 [references/review-batches.md](references/review-batches.md) 的小批模式：只複核指定 records，以 `review_checkpoint.py commit` 存檔後結束。OCR、合併及 Excel 由外層處理，不重做下列完整流程，也不讀取其他批次圖片。

1. **建立明確批次**：把選定 PDF 的完整路徑寫入本批 `sources.json` 陣列，建立新暫存資料夾。啟動器已提供時直接使用。首次／直接執行先跑 `run_batch.ps1 -Preflight`，檢查 PDF、Python 套件、繁中 OCR 及範本；桌面啟動器另檢查 Excel 註冊。
2. **整批辨識與解析**：執行一次 `run_batch.ps1 -SourcesJson <sources.json> -WorkDir <workdir> -UserCode <code> -SubmissionMonth <month> [-CacheDir <cache>] [-Template <xls>]`。同一 Python 程序依序處理所有頁面及局部重辨識，共用 OCR 模型；只用一個 worker，不再按 15 頁重啟。每頁直接輸出座標 OCR，含文字層的 PDF 也可解析。等待程序結束並檢查返回碼。
3. **分批複核 CSV 與匯入資料**：工具同步產生 `draft.csv`、`details.draft.json`、`flags.json`、`review.json`、`manifest.json`。先讀 [references/bill-fields.md](references/bill-fields.md) 與 [references/winton-import.md](references/winton-import.md)，依統一清單產生裁圖，用 `view_image` 複核。每 5 筆同時核對 CSV 與匯入資料並存檔，使用 [分批存檔工具](references/review-batches.md)；不要等整批看完才保存，也不要一次輸出全部 JSON／圖片。全部完成後合併為 `details.json` 與最終 CSV，勿重新抄整批欄位。缺值或無法確認者留白並註明原因。
4. **保留來源證據**：推算／修正的稅額及 5% 檢核異常必須核對原單。未確認前，不把它填入 XLS 稅額或扣抵否。確認後在該筆 `details.json` 填 `tax_status: reviewed`、`tax_amount`、`evidence`（來源／頁碼／欄位），並更新 CSV、移除已解決的警示。其他無法解決的警示保留。
5. **確認完整性與重複**：只自動略過發票號碼、號碼、期間、金額及其他關鍵欄位／證據都一致的重掃。缺少識別資料或欄位衝突一律保留待確認，不能為了號碼唯一而任意刪帳單。正式 CSV 號碼仍須唯一；同號碼不同期若都有效，保留草稿、說明需分批，不任意選一筆。複核導致增減列時，在 `manifest.json` 的 `resolutions` 記錄來源頁面、最後筆數與原單判斷原因，格式見 operations。匯出程式會核對全部來源頁面、來源雜湊、解析筆數與最終 CSV。
6. **匯出**：使用 `export_winton_import.ps1`，傳入 CSV、客戶、月份、`details.json`、`-Manifest <manifest.json>`、`-SourcesJson <sources.json>`、範本及輸出路徑。保留 CSV 14 欄與 XLS 33 欄標題，完整欄位對應只以 winton-import 參考文件為準。預設 W 欄使用本期載具流水號，帳面發票號碼留備註；僅明確要求才用 `printed` 模式。
7. **回報**：列出客戶、送件年月、檔案連結、各類帳單筆數、略過重掃及全部剩餘 CSV／匯入警示。另述是否使用快取、失敗或未完成頁面。需要一般帳單彙整時才另做 `.xlsx`，識別碼與前導零保留文字。

## 啟動器交接與續跑

雙擊入口為 `winton-scan.cmd`，預設以 `gpt-6-luna`（Luna）啟動 Codex，可用 `-Model` 明確覆寫；推理強度沿用 Codex 設定。在既有對話直接呼叫技能時仍使用該對話模型。啟動器先做整批 OCR，再循序以新對話處理每 5 筆複核，全部存檔後自行合併、驗證並以 `-PrepareOnly` 產生完成標記，最後由桌面 Excel 匯出。小批對話只交付複核存檔。直接執行完整流程且收到完成標記路徑時，步驟 6 加 `-PrepareOnly -ReadyFile <指定路徑>`，回報「匯入資料已備妥，啟動程式接續產生 XLS」。不自行建立標記，不在沙箱重試 Excel或變更全域設定。

標記綁定客戶、月份、CSV、補充資料、manifest、來源清單與範本。建立後若修改相關資料，先移除本批標記，完成複核後重新準備。Excel／範本失敗時保留 CSV，回報原因，不以改名 HTML／XLSX 代替 XLS。

中斷時保留資料。再次雙擊並提供相同月份、客戶與相同來源／範本，會自動接續匹配的未完成批次，跳過已存檔複核；也可用 `-ResumeWorkDir` 指定。暫時性串流／服務錯誤每小批最多嘗試 3 次，未知錯誤、登入／額度問題停止。快取依 PDF 雜湊、頁碼、引擎與程式版本及裁圖參數驗證；只重新辨識缺少、損壞或失效結果。來源／參數／程式版本改變時開新 WorkDir（啟動器 `-NewBatch`），共用相同快取。已完成且人工修改的草稿不由批次工具覆寫。細節及故障處理見 operations。
