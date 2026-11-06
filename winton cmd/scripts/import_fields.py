"""Conservative import-only extraction from each bill's own geometric fragments."""
import re


def extract_import_fields(row):
    from build_winton_import import invoice_date, amount, billing_period
    frags = row.get("_frags", [])
    details, flags, evidence = {}, [], []
    if row.get("_collection_evidence"):
        evidence.append("收款日期：" + row["_collection_evidence"])

    def pick(key, label, pattern, transform):
        candidates = {}
        for lab in frags:
            match = re.search(label, lab.text)
            if not match or re.search(r"前期|上期", lab.text):
                continue
            tail = lab.text[match.end():].lstrip(" ：:＄$")
            values = [(tail, lab.text)]
            values += [(f.text.strip().lstrip("＄$"), lab.text + " → " + f.text)
                       for f in frags if f is not lab and abs(f.y - lab.y) <= 0.006
                       and 0 < f.x - lab.x <= 0.30]
            for value, proof in values:
                found = re.fullmatch(pattern, value)
                if not found:
                    continue
                try:
                    result = transform(found[1])
                except (ValueError, TypeError):
                    continue
                candidates[str(result)] = (result, proof)
        if len(candidates) == 1:
            result, proof = next(iter(candidates.values()))
            details[key] = result
            evidence.append(proof)
        elif len(candidates) > 1:
            flags.append(f"⚠️匯入欄位 {key} 有多個原單候選值，須目視確認")

    pick("invoice_date", r"發票(?:開立)?日期|開立日期",
         r"((?:\d{3,4}[/.-]\d{1,2}[/.-]\d{1,2})|\d{7,8})",
         lambda value: invoice_date(value)[1])
    pick("printed_invoice", r"(?<!載具)發票號碼", r"([A-Z]{2}\d{8})", str)
    kind = row["type"]
    status = row.get("_tax_status", "missing")
    if kind in {"電費", "水費"}:
        details["tax_status"] = status
        details["tax_amount"] = row["tax"] if status == "source" else ""
        if status == "source":
            evidence.append("營業稅：帳單稅額標籤原讀值 " + row["tax"])
        if kind == "水費":
            gross = row.get("_water_gross")
            if gross is not None:
                details["gross_amount"] = str(gross)
                evidence.append(f"水費小計（排除代收費用）：{gross:g}")
    elif kind == "電信費":
        pick("tax_amount", r"營業稅(?:額)?|(?<!未)稅額",
             r"([\d,]+(?:\.\d{1,2})?)\s*元?", amount)
        pick("gross_amount", r"發票含稅金額|含稅總額",
             r"([\d,]+(?:\.\d{1,2})?)\s*元?", amount)
        details["tax_status"] = "source" if "tax_amount" in details else "missing"
    if not row.get("receipt_date"):
        flags.append("⚠️匯入日期缺少收款章日期或繳費日期，須查原單；無依據留白")
    if details.get("tax_status") not in {"source", "reviewed"}:
        flags.append("⚠️匯入稅額缺少已核對原單金額；未確認前稅額與扣抵否留白")
    if kind == "電信費" and "gross_amount" not in details:
        flags.append("⚠️電信發票含稅金額須核對，不能直接將繳費總額視為發票金額")
    if not re.fullmatch(r"[A-Z0-9]{10}", row.get("invoice", "")):
        flags.append("⚠️匯入本期載具流水號缺少或格式不符")
    if not re.fullmatch(r"\d{8}", row.get("ubn", "")):
        flags.append("⚠️匯入賣方統編缺少或格式不符")
    try:
        start, end = billing_period(row.get("period", ""))
        if start[:3] != end[:3]:
            flags.append("⚠️計費期間跨年，範本無所屬年(迄)，須確認匯入方式")
    except ValueError:
        flags.append("⚠️匯入所屬年／月缺少有效完整計費期間")
    if kind == "水費" and "gross_amount" not in details:
        flags.append("⚠️水費小計缺少；不可把含代收費用的總額當憑證金額")
    details["evidence"] = "; ".join(evidence)
    return details, flags
