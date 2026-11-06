"""Build a reviewed Winton import payload; Excel writes it with the PS wrapper."""
import argparse
import csv
import json
import re
import sys
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

USERS = {
    "H00162": "茂景開發股份有限公司",
    "H00563": "時常旅宿管理顧問有限公司",
    "H00927": "宇盛創業投資有限公司",
    "H04822": "伊格控股股份有限公司",
    "H06044": "伊格公寓大廈管理維護股份有限公司",
    "H06062": "貫登服務股份有限公司台南分公司",
    "H08088": "和居家居股份有限公司",
    "H11037": "貫登服務股份有限公司",
    "H2992": "一隻魚國際有限公司",
    "H6738": "彰化縣租賃住宅服務商業同業公會",
    "H8182": "伊格公寓大廈管理維護股份有限公司彰化分公司",
    "T2992": "一隻魚國際有限公司（內帳）",
}
HEADERS = ["送件年月", "用戶代號", "所屬年(起)", "所屬月(起)", "所屬月(迄)",
           "憑證類別", "第冊數", "扣抵否", "序號(進項憑證)", "類別(進項憑證)",
           "憑證金額", "稅別", "稅額", "日期", "賣方統編", "營業稅項目類別代號",
           "供應商代號", "公斤數", "出售註記", "徵收費", "註記", "報單號碼",
           "發票號碼&繳納證號碼", "年期別(公用事業憑證使用)", "列印註記",
           "公用事業註記", "公用事業分攤註記", "借方項目", "借方摘要", "貸方項目",
           "貸方摘要", "稅額項目", "稅額摘要"]
TEXT_COLUMNS = [1, 2, 3, 4, 5, 6, 8, 10, 12, 14, 15, 23]
NUMBER_COLUMN = "電號/水號/電話號碼"
DETAIL_KEYS = {"invoice_date", "invoice_month", "printed_invoice", "gross_amount",
               "certificate_amount", "tax_amount", "evidence", "tax_status"}


def roc_month(value):
    if not re.fullmatch(r"\d{5}", value):
        raise ValueError("年月須為五碼民國年月")
    date(int(value[:3]) + 1911, int(value[3:]), 1)
    if int(value[:3]) < 1:
        raise ValueError("民國年份須大於零")
    return value


def invoice_date(value):
    value = str(value).strip()
    m = re.fullmatch(r"(\d{3}|\d{4})[/.-](\d{1,2})[/.-](\d{1,2})", value)
    if m:
        year, month, day = m.groups()
    elif re.fullmatch(r"\d{7}|\d{8}", value):
        year, month, day = value[:-4], value[-4:-2], value[-2:]
    else:
        raise ValueError("發票日期格式須為民國或西元年月日")
    western_year = int(year) + 1911 if len(year) == 3 else int(year)
    parsed = date(western_year, int(month), int(day))
    roc_year = parsed.year - 1911
    if not 1 <= roc_year <= 999:
        raise ValueError("發票日期超出三碼民國年份範圍")
    return f"{roc_year:03d}{parsed.month:02d}", f"{roc_year:03d}{parsed.month:02d}{parsed.day:02d}"


def amount(value):
    if value is None or str(value).strip() == "":
        return None
    value = str(value).strip().replace(",", "")
    if not re.fullmatch(r"\d+(?:\.\d{1,2})?", value):
        raise ValueError("金額須為非負數，最多兩位小數")
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("金額無法解析") from error
    if not parsed.is_finite() or parsed < 0:
        raise ValueError("金額須為有限非負數")
    return int(parsed) if parsed == parsed.to_integral_value() else float(parsed)


def billing_period(value):
    """Read both bill dates without confusing ISO date hyphens with the range."""
    date_pattern = r"(?:\d{3,4}[/.-]\d{1,2}[/.-]\d{1,2}|\d{7,8})"
    match = re.fullmatch(
        rf"\s*({date_pattern})\s*(?:至|到|~|～|〜|－|–|—|-)\s*({date_pattern})\s*",
        str(value),
    )
    if not match:
        raise ValueError("計費期間須有完整起日與迄日")
    start_month, start_day = invoice_date(match[1])
    end_month, end_day = invoice_date(match[2])
    if end_day < start_day:
        raise ValueError("計費期間迄日早於起日")
    return start_month, end_month


def build_payload(rows, user_code, submission_month, details=None, mode="carrier"):
    if user_code not in USERS:
        raise ValueError("用戶代號不在本技能的選單中")
    if not submission_month:
        raise ValueError("送件年月須由使用者輸入，不自動帶入當月")
    roc_month(submission_month)
    if mode not in {"carrier", "printed"}:
        raise ValueError("發票號碼來源須為 carrier 或 printed")
    if not rows or len(rows) > 65535:
        raise ValueError("匯入資料須為 1 至 65535 筆（XLS 第一列保留標題）")
    details = details or {}
    if not isinstance(details, dict):
        raise ValueError("補充資料須為以帳單號碼索引的 JSON 物件")
    numbers = [r[NUMBER_COLUMN].strip() for r in rows]
    if any(not n for n in numbers) or len(set(numbers)) != len(numbers):
        raise ValueError("帳單號碼有空白或重複，請先完成 CSV 複核")
    if set(details) - set(numbers):
        raise ValueError("補充資料含本批 CSV 沒有的帳單號碼")
    output, reviews, seen_invoices = [], [], set()
    for index, row in enumerate(rows, 1):
        number = numbers[index - 1]
        detail = details.get(number, {})
        if not isinstance(detail, dict) or set(detail) - DETAIL_KEYS:
            raise ValueError(f"{number}：補充資料欄位不正確")
        flags = [part.strip() for part in row["備註(不確定標記)"].split(";") if "⚠️" in part]
        tax_status = detail.get("tax_status", "")
        if tax_status not in {"", "source", "derived", "missing", "reviewed"}:
            raise ValueError(f"{number}：tax_status 不正確")
        reviewed_tax = tax_status == "reviewed"
        if reviewed_tax and (not str(detail.get("evidence", "")).strip() or detail.get("tax_amount") in (None, "")):
            raise ValueError(f"{number}：核對稅額須有 tax_amount 及原單 evidence")
        start_month = end_month = ""
        try:
            start_month, end_month = billing_period(row.get("計費期間", ""))
        except ValueError as error:
            flags.append(f"⚠️所屬年／所屬月起迄無法確認：{error}")
        if start_month and start_month[:3] != end_month[:3]:
            flags.append("⚠️計費期間跨年，範本沒有所屬年(迄)欄；已填起年與起迄月，請確認匯入方式")
        day = ""
        if row.get("收款日期"):
            try:
                _, day = invoice_date(row["收款日期"])
            except ValueError as error:
                flags.append(f"⚠️收款日期無法確認：{error}")
        if not day:
            flags.append("⚠️日期缺少收款章日期或繳費日期")
        if mode == "carrier":
            serial = row["發票號碼"].strip().upper()
            pattern = r"[A-Z0-9]{10}"
        else:
            serial = str(detail.get("printed_invoice", "")).strip().upper()
            if not serial and row["帳單類型"] == "水費":
                match = re.search(r"帳面發票號碼([A-Z]{2}\d{8})", row["備註(不確定標記)"])
                serial = match[1] if match else ""
            serial = serial.replace("-", "")
            pattern = r"[A-Z]{2}\d{8}"
        if not re.fullmatch(pattern, serial):
            flags.append("⚠️本期發票號碼缺少或格式不符")
            serial = ""
        if serial:
            if serial in seen_invoices:
                raise ValueError(f"本期發票號碼重複：{serial}")
            seen_invoices.add(serial)
        ubn = row["統一編號"].strip()
        if not re.fullmatch(r"\d{8}", ubn):
            flags.append("⚠️賣方統編缺少或不是八碼")
            ubn = ""
        kind = row["帳單類型"]
        gross_source = detail.get("gross_amount")
        if gross_source is None:
            if kind == "水費":
                match = re.search(r"水費小計([\d,.]+)元", row["備註(不確定標記)"])
                gross_source = match[1] if match else None
            elif kind == "電費":
                gross_source = row["應繳總金額"]
        tax_source = detail.get("tax_amount", row["營業稅"])
        tax_suspect = tax_status in {"derived", "missing"} or bool(re.search(
            r"(?:^|;)(?:⚠️)?(?:營業稅[^;]*(?:推得|推算|更正|修正|檢核不符)|稅前/營業稅由總額推得)", row["備註(不確定標記)"]))
        if tax_suspect and not reviewed_tax:
            tax_source = None
            flags.append("⚠️稅額含推算／修正或未確認資料；核對原單前不填稅額與扣抵否")
        gross = tax = None
        for label, value in (("發票含稅金額", gross_source), ("稅額", tax_source)):
            try:
                parsed = amount(value)
            except ValueError as error:
                parsed = None
                flags.append(f"⚠️{label}格式不符：{error}")
            if parsed is None and label == "稅額":
                flags.append(f"⚠️{label}缺少原單金額")
            if label == "發票含稅金額":
                gross = parsed
            else:
                tax = parsed
        if gross is not None and tax is not None and tax > gross:
            raise ValueError(f"{number}：稅額大於發票含稅金額")
        if kind in {"電費", "水費"} and gross is not None and tax is not None:
            if abs((Decimal(str(gross)) - Decimal(str(tax))) * Decimal("0.05") - Decimal(str(tax))) > Decimal("1.5"):
                flags.append("⚠️原單稅額與本技能 5% 檢核不符，須核對原單")
                if not reviewed_tax:
                    tax = None
        certificate = None
        certificate_source = detail.get("certificate_amount")
        if certificate_source is not None:
            try:
                certificate = amount(certificate_source)
            except ValueError as error:
                flags.append(f"⚠️憑證金額格式不符：{error}")
        elif gross is not None and tax is not None:
            certificate = amount(str(Decimal(str(gross)) - Decimal(str(tax))))
        if certificate is None:
            flags.append("⚠️憑證金額缺少原單未稅金額或可核對的含稅金額與稅額")
        elif gross is not None and tax is not None:
            if Decimal(str(certificate)) + Decimal(str(tax)) != Decimal(str(gross)):
                raise ValueError(f"{number}：憑證金額加稅額與發票含稅金額不符")
        if kind not in {"電費", "水費", "電信費"}:
            flags.append("⚠️帳單類型需人工判讀")
        values = [None] * len(HEADERS)
        values[:15] = [submission_month, user_code, start_month[:3], start_month[3:], end_month[3:],
                       "1", None, "Y" if tax is not None and tax > 0 else None,
                       None, "2", certificate, "5", tax, day, ubn]
        values[22] = serial
        output.append(values)
        if flags:
            reviews.append({"列號": index + 1, "來源檔案": row["來源檔案"],
                            "號碼": number, "待確認": list(dict.fromkeys(flags))})
    return {"headers": HEADERS, "text_columns": TEXT_COLUMNS, "rows": output,
            "user_code": user_code, "user_name": USERS[user_code],
            "submission_month": submission_month, "invoice_mode": mode,
            "reviews": reviews}


def main():
    parser = argparse.ArgumentParser(description="準備文中進項憑證 XLS 匯入資料")
    parser.add_argument("--list-users", action="store_true")
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--user-code")
    parser.add_argument("--submission-month")
    parser.add_argument("--details-json", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--sources-json", type=Path)
    parser.add_argument("--invoice-mode", choices=["carrier", "printed"], default="carrier")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.list_users:
        print(json.dumps(USERS, ensure_ascii=False))
        return
    if not args.csv or not args.user_code or not args.out or not args.submission_month:
        parser.error("須提供 --csv、--user-code、--submission-month、--out；送件年月由使用者輸入")
    month = args.submission_month.strip()
    from batch_support import read_csv, validate_manifest
    rows = read_csv(args.csv)
    if not args.manifest:
        raise ValueError("匯出須提供 --manifest，以驗證每份來源 PDF 的所有頁面")
    validate_manifest(args.manifest, rows, args.user_code, month, args.sources_json)
    details = json.loads(args.details_json.read_text(encoding="utf-8-sig")) if args.details_json else {}
    payload = build_payload(rows, args.user_code, month, details, args.invoice_mode)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已準備 {len(rows)} 筆，{len(payload['reviews'])} 筆匯入資料待確認")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        main()
    except (ValueError, OSError, KeyError) as error:
        print(f"匯入資料錯誤：{error}", file=sys.stderr)
        sys.exit(1)
