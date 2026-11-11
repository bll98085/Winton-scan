# Winton-scan 帳單辨識與匯入檔產生工具

Winton-scan 會辨識台電、台灣自來水及遠傳等帳單 PDF，經 Codex 分批複核後，產生帳單 CSV 與文中系統使用的原生 XLS 匯入檔。

目前 Windows 啟動器固定使用以下工作資料夾：

```text
C:\Users\Eagle\Desktop\Winton-scan
```

## 實際執行方式

### 1. 放入待處理 PDF

將要處理的 PDF 直接放在專案根目錄，不要放進子資料夾：

```text
C:\Users\Eagle\Desktop\Winton-scan\待處理帳單.pdf
```

程式會處理根目錄內的全部 PDF：

- 不限制掃描日期、建立日期或修改日期。
- 不處理子資料夾內的 PDF。
- 同一批 PDF 全部歸屬於本次選擇的客戶。
- 執行前請關閉已在 PDF 閱讀器或 Excel 中開啟的來源及輸出檔案。

### 2. 啟動程式

雙擊：

```text
C:\Users\Eagle\Desktop\Winton-scan\winton cmd\winton-scan.cmd
```

依畫面提示輸入：

1. **keyin 月份／送件年月**：五碼民國年月，例如 `11510`。
2. **客戶代號**：從畫面列出的客戶清單選擇，例如 `H11037`。

接著程式會執行 OCR、帳單欄位解析、Codex 複核及 Excel 匯出。Codex 預設使用 `gpt-6-luna`，每批最多複核 5 筆，每批完成即保存進度。

> 送件年月只用於匯入資料，不會拿來篩選 PDF。

### 3. 查看完成結果

整批成功後，程式會自動整理檔案：

| 檔案 | 完成後位置 | 處理方式 |
|---|---|---|
| 來源 PDF | `C:\Users\Eagle\Desktop\Winton-scan\已掃描PDF\` | **直接移動，不保留根目錄副本** |
| 帳單 CSV | `C:\Users\Eagle\Desktop\Winton-scan\Excel及匯入檔\` | 移動 |
| 原生 XLS 匯入檔 | `C:\Users\Eagle\Desktop\Winton-scan\Excel及匯入檔\` | 移動 |

輸出檔名通常如下：

```text
帳單_YYYYMMDD.csv
進項憑證明細檔_YYYYMMDD_客戶代號.xls
```

若遇到同名檔案，程式會產生不重複的名稱，不會直接覆寫舊檔。

完成後的資料夾範例如下：

```text
Winton-scan\
├─ winton cmd\
├─ 已掃描PDF\
│  └─ 待處理帳單.pdf
└─ Excel及匯入檔\
   ├─ 帳單_20261111.csv
   └─ 進項憑證明細檔_20261111_H11037.xls
```

## 收款日期規則

帳單 CSV 的 `收款日期` 欄位及進項憑證明細檔的日期欄位使用相同判斷：

1. 帳單有收款章且能辨識章內日期時，使用**收款章日期**。
2. 沒有收款章日期時，改用帳單上方的**繳費日期**。
3. 兩者都無法確認時保留警示，交由複核處理，不任意猜測日期。

## 第一次使用前的環境檢查

需要以下環境：

- Windows。
- Python 3.12 64 位元及 `winton cmd\requirements.txt` 內的套件。
- Windows 繁體中文（台灣）OCR 語言功能。
- Microsoft Excel 桌面版；網頁版 Excel 無法執行原生 XLS 匯出。
- 已安裝並登入的 Codex CLI，且帳號可使用設定的模型。

雙擊下列檔案進行檢查：

```text
C:\Users\Eagle\Desktop\Winton-scan\winton cmd\check-environment.cmd
```

環境檢查只檢查必要元件，不會處理正式帳單。完整移機與安裝步驟請見 [`winton cmd/移機使用說明.md`](winton%20cmd/%E7%A7%BB%E6%A9%9F%E4%BD%BF%E7%94%A8%E8%AA%AA%E6%98%8E.md)。

## 中斷、失敗與續跑

執行進度保存在：

```text
%TEMP%\winton_scan_*
```

如果 OCR、模型連線、複核或 Excel 匯出中斷：

1. 不要刪除畫面顯示的暫存工作資料夾。
2. 保持來源 PDF、送件年月、客戶代號及範本不變。
3. 再次雙擊 `winton-scan.cmd`，輸入相同的月份與客戶代號。
4. 程式會自動接續符合的未完成批次，已保存的複核批次不會重做。

若來源 PDF、參數或範本已變更，請改開新批次，不要強行沿用舊進度。

## 命令列執行

一般使用建議直接雙擊 CMD。需要測試參數但不啟動 OCR、Codex 或 Excel 時，可執行：

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass `
  -File "C:\Users\Eagle\Desktop\Winton-scan\winton cmd\scripts\start_winton_scan.ps1" `
  -ScanDir "C:\Users\Eagle\Desktop\Winton-scan" `
  -SubmissionMonth 11510 `
  -UserCode H11037 `
  -DryRun
```

需要直接指定完整參數正式執行時，移除 `-DryRun`。也可使用 `-Model`、`-BatchSize`、`-Template`、`-CacheDir`、`-ResumeWorkDir` 或 `-NewBatch`；細節請見 [`winton cmd/references/operations.md`](winton%20cmd/references/operations.md)。

## 目前路徑設定

`winton cmd\winton-scan.cmd` 內的 `WINTON_BASE` 目前固定為：

```bat
set "WINTON_BASE=C:\Users\Eagle\Desktop\Winton-scan"
```

若整個專案移到其他位置，請修改這一行，或直接用 PowerShell 執行 `start_winton_scan.ps1` 並傳入新的 `-ScanDir`。

## 開發驗證

測試不會呼叫正式模型 API：

```powershell
python ".\winton cmd\scripts\test_workflow.py"
python ".\winton cmd\scripts\test_review_checkpoint.py"
```

帳單 PDF、已掃描 PDF、CSV、XLS 及 XLSX 已由 `.gitignore` 排除，避免把客戶資料或正式輸出誤推送到 GitHub。
