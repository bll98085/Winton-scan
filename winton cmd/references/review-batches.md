# 分批複核存檔

啟動器先完成一次 OCR，再以獨立 Codex 對話依序複核每批最多 5 筆。收到 `batch_NNN.input.json` 時，只做該小批；不執行一般流程的 OCR、合併或匯出。模型、推理強度與原有設定相同。

讀取 input 的 `plan_id`、`batch`、`records`。每筆有穩定的 `index`、14 欄 CSV、候選 `details`、警示與裁圖路徑。讀取文字使用 UTF-8；PowerShell 向 Python 傳中文程式碼時設定 `$OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new()`，Python 用 `-X utf8`。

依 bill-fields 與 winton-import 同時核對兩種輸出。每次最多開兩张本批裁圖，一般用 `detail: high`，細字無法辨識才用 `original`。需要更多證據時只讀同頁 OCR 或原 PDF。圖片上未確定的欄位保持空白並記錄原因；缺發票日期不可拿繳款期限補上。

將修正寫成 UTF-8 JSON，不重抄未變欄位：

```json
{
  "plan_id": "從 input 原樣複製",
  "batch": 1,
  "records": [
    {
      "index": 1,
      "evidence": "a.pdf p1：目視營業稅欄為50；無明確發票日期，保留待確認",
      "reason": "",
      "rows": [
        {
          "csv_updates": {"營業稅": "50"},
          "details_updates": {
            "tax_status": "reviewed",
            "tax_amount": "50",
            "evidence": "a.pdf p1 營業稅欄目視50元",
            "invoice_date": null
          }
        }
      ]
    }
  ]
}
```

`records` 必須恰好涵蓋 input 的全部 index，每個一次。CSV 修改值一律是字串，空值為 `""`，不可更改來源頁面；details 中 `null` 表示移除未確定的候選。没有變更時兩個 updates 都用 `{}`，仍須記錄核對證據及剩餘問題。未解決的警示需留在 CSV 備註與 evidence；推算／異常稅額未確認時，清空相應 CSV 值與補充稅額，不冒用 `source`。

一般 `rows` 恰好一筆。確認重掃而刪除時用 `rows: []`，且 `reason` 必須記錄可對照的來源、發票與原單判定理由。確認同頁漏拆帳單時可有多筆 rows，各筆覆寫不同欄位；合併器會自動計算該頁的 manifest resolutions。不可為了號碼唯一而刪除不同期帳單；無法判定時停止並回報。跨小批的號碼衝突由最後完整性檢查攔截。

寫好 candidate 後，執行啟動提示指定的 `review_checkpoint.py commit`。只有返回碼 0 才算存檔成功。修正驗證錯誤後可重新提交；已完成且內容不同的批次不允許覆寫。存檔只提交本小批，最後回報剩餘警示即可，勿繼續其他批次。

完成存檔的批次會在重新啟動時自動跳過；新對話不需要先前圖片或對話記憶。外層在所有小批完成後合併、驗證來源頁面與重複號碼、產生 ready 標記，再由桌面 Excel 匯出。

直接在對話執行完整技能時，先以 `review_checkpoint.py init --workdir <workdir> --size 5` 建立計畫，再用 `status --workdir <workdir>` 取得 `next_batch`，用 `input --workdir <workdir> --batch <批號>` 產生該小批 input。每次 commit 後讀 status 接續下一批。全部完成後執行 `materialize --workdir <workdir> --csv <最終CSV> --details <details.json>`，再把 `reviewed_manifest.json` 傳給既有匯出工具。這些子命令都使用同一支 `scripts/review_checkpoint.py`。
