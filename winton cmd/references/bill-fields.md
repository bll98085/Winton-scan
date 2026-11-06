# Bill fields and geometric recognition rules

## Consistency checks (built into parse_bills.py)

Applied to electricity bills. Source values passing checks may be accepted; derived or uncertain values retain review flags:

- Pre-tax total + VAT = total due (exact)
- VAT ≈ pre-tax × 5% (independent check, tolerance 1.5). Derived/repaired VAT is flagged and is excluded from XLS tax/deductibility until verified against the source.
- Peak kWh + off-peak kWh ≈ carbon emissions (kg) ÷ 0.474 (±3; on mismatch the parser first re-OCRs the kWh block at high DPI, then falls back to the carbon-derived total when the average-price check confirms it; also derives a missing time-of-use component, accurate to ±1)
- Current average price per kWh × total kWh ≈ total due (±2%)

Water bills (also automatic):

- Pre-tax subtotal + VAT = 水費小計 (and VAT ≈ pre-tax × 5%)
- 水費小計 + 代徵費用小計 = 代收總金額 (a missing value is derived from the others)

Telecom (FET): 月租費 block subtotal + 國內通話費 block subtotal ≈ 本期金額 (the total is printed 2–3 times per stub; the copy matching the line items wins — this also catches truncated reads like `$51` for `$514`).

## Target fields (per bill)

| Field | Electricity (Taipower) | Water (Taiwan Water) | Telecom (FET) |
|------|----------------|----------------|----------------|
| 1. Bill type | `電費` | `水費` | `電信費` |
| 2. Usage (kWh/units) | peak kWh + off-peak kWh | current period 實際用水度數 | **leave blank** |
| 3. Total due | 應繳總金額 | 代收總金額 | 本期金額 |
| 4. VAT | 營業稅 | 營業稅 (only the water charge is taxed) | leave blank |
| 5. Call charges | leave blank | leave blank | 本期金額 − 月租小計 (see telecom rules below) |
| 6. Address | 用電地址 in the customer info section | 用水地址 | **leave blank** |
| 7. Billing period | 計費期間 | 本期用水計費期間 | billing cycle |
| 8. Meter/water/phone number | 電號 (**no dashes**, e.g. `10333054115`) | 水號 (**no dashes**, e.g. `4P516664203`) | **phone number** |
| 9. 統一編號 (biller's UBN) | the 8 digits after 本公司營利事業統一編號 | same as left | **fixed `97179430`** (FET's UBN, not printed on the stub) |
| 10. 年期碼 | first 5 digits of the 本期載具號碼 barcode text (ROC year + period, e.g. `11506`) | same as left | same as left |
| 11. Invoice number | the 10 characters after the 年期碼 in the 本期載具號碼 barcode text (carrier serial number) | same as left | same as left |
| 12. 收款日期 | 收款章內日期；沒有收款章日期時取上方繳費日期 | 同左 | 同左 |

### Extra optional fields (for traceability, not required)
- Pre-tax amount, average price per kWh (electricity), etc. — record them if easy to extract.

## Telecom bill rules (FET)

- **Write the phone number into the `電號/水號/電話號碼` column** (renamed from `電號`; it covers meter numbers, water numbers, and phone numbers).
- **統一編號 is always the fixed value `97179430`** (遠傳電信's own UBN — the stub does not print it; parse_bills.py fills it in automatically).
- **Do not fill address or usage** — leave both blank.
- **Call charges** are the extra variable cost, in their own column: call charges = 本期金額 − the 月租費 block's printed 小計 (each phone number may be on a different plan — 4G599−100優惠, 5G599 with no discount, etc., so the subtotal is read from the bill itself, never assumed).
- **Write bill info into the remarks column**, format: `遠傳電信綜合帳單;<plan>月租<fee>[-優惠<disc>|(無月租優惠)]+通話<call charges>`, e.g. `遠傳電信綜合帳單;4G月租599-優惠100+通話15(通話明細14.7,總額四捨五入)` or `遠傳電信綜合帳單;5G月租599(無月租優惠)+通話0`.
- parse_bills.py handles all of the above automatically from the bill's line items. Only when the 月租 subtotal can't be read does it fall back to the `FET_MONTHLY, FET_DISCOUNT` constants **with a ⚠️ flag** (update the constants if the default plan changes). A negative computed call charge is never emitted — the row gets a ⚠️ instead.

## Extraction notes

- **Summing kWh**: the electricity usage column = peak kWh + off-peak kWh; write only the sum in the usage column.
- **Water bill amount levels**: water bills have three levels — 水費小計, 代收費用小計 (清除處理費, 水源保育費, etc.), and 代收總金額. Total due takes **代收總金額**; VAT takes the 營業稅 line printed on the bill (only the water charge is taxed, the collected fees are not).
- **Take the address from the bill body**: the mailing address at the top of the page is often the company headquarters — do not use it; take the actual address from the 用電地址 / 用水地址 field.
- **Address line wrapping**: the 用電/用水 address usually wraps after 號, with the floor/unit suffix (e.g. `4樓`, `地下室`, `樓右邊`) on the next line — the parser merges continuation lines automatically (re-OCRing at high DPI when needed) and flags addresses still missing a floor-style ending or matching known truncation patterns (`弄號`, `號地` without a following 下, `里` followed by `巷` with no 街/路). Known residual risks: sub-units like `4樓-2` may drop the `-2`, and a wrapped 樓 may merge as `一樓` vs `1樓` without being flagged — worth a glance during spot checks.
- **Period format**: keep the bill's original format (ROC calendar year, e.g. `115/02/04至115/03/08`).
- **Invoice number and 年期碼**: taken from the 本期載具號碼 block — the barcode text starts with a 5-digit year-period prefix (ROC year + 2-digit period, e.g. `11506`) followed by the 10-character carrier serial number. Taipower prints exactly those 15 characters; Taiwan Water appends a 6-char check code and FET appends the last 5 digits of the account number — in every case the `年期碼` column records the **5-digit prefix** and the invoice column records **only the 10-character serial** after it. parse_bills.py handles this automatically (including accepting an unlabeled but structurally valid `BB…` serial with a note instead of a flag); apply the same rule when re-reading a page manually.
- **統一編號**: the 8 digits printed after `本公司營利事業統一編號` (the biller's own UBN — Taipower/Taiwan Water print it on the bill; the label with 公司/營利/事業 wins over a bare 統一編號 label, which may be the customer's UBN and gets a 請抽查 note). FET rows use the fixed value `97179430`. A missing UBN gets a ⚠️ flag.
- **Numbers are written without dashes**: parse_bills.py strips `-` from 電號/水號 in the CSV (`10-33-3054-11-5` → `10333054115`, `4P-51666420-3` → `4P516664203`). Apply the same rule when fixing a row manually.
- **Water bills**: fully auto-extracted — usage (本期實用度數), total (代收總金額), VAT, 用水地址 (multi-line, merged), period, 水號 (站所-號-檢號 read as `4P-51666420-3`, written without dashes as `4P516664203`), and the carrier serial. The printed cloud-invoice number (e.g. `AN13377352`) goes into the remarks as `帳面發票號碼…` for traceability. Only fields that fail extraction or the identities get a ⚠️.

## CSV / Excel column order

The consolidated CSV and optional ordinary Excel summary use the same column order, kept consistent:

`來源檔案` | `帳單類型` | `用量(度)` | `應繳總金額` | `營業稅` | `通話費` | `地址` | `計費期間` | `收款日期` | `電號/水號/電話號碼` | `統一編號` | `年期碼` | `發票號碼` | `備註(不確定標記)`

## Output CSV format example

```
來源檔案,帳單類型,用量(度),應繳總金額,營業稅,通話費,地址,計費期間,收款日期,電號/水號/電話號碼,統一編號,年期碼,發票號碼,備註(不確定標記)
台電.pdf,電費,3000,"10,339","492",,台南市仁德區中洲里中洲一街103巷15號一樓,115/02/04至115/03/08,115/05/06,10333054115,03795904,11504,BB12345678,
電信.pdf,電信費,,513.7,,14.7,,115/05/21-115/06/20,115/06/25,0912345678,97179430,11506,BB87654321,遠傳電信綜合帳單;4G月租599-優惠100+通話14.7
```

