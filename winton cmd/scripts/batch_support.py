"""Shared file formats and coverage checks for the Winton batch workflow."""
import csv
import hashlib
import json
import os
import tempfile
from collections import Counter
from pathlib import Path

CSV_COLUMNS = ["來源檔案", "帳單類型", "用量(度)", "應繳總金額", "營業稅", "通話費",
               "地址", "計費期間", "收款日期", "電號/水號/電話號碼", "統一編號", "年期碼",
               "發票號碼", "備註(不確定標記)"]


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def atomic_text(path, text, encoding="utf-8"):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_json(path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2))


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def read_csv(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != CSV_COLUMNS:
            raise ValueError("CSV 須維持指定的 14 欄名稱與順序")
        rows = list(reader)
        if any(None in row or any(v is None for v in row.values()) for row in rows):
            raise ValueError("CSV 列的欄位數不正確")
        return rows


def validate_manifest(path, rows, user_code=None, submission_month=None, sources_json=None):
    """Require every selected source page, including explicitly resolved blank pages."""
    manifest = read_json(path)
    if manifest.get("schema_version") != 1 or manifest.get("status") != "parsed":
        raise ValueError("批次清單尚未完成辨識與解析")
    for field, expected in (("user_code", user_code), ("submission_month", submission_month)):
        if expected is not None and manifest.get(field) != expected:
            raise ValueError(f"批次清單的 {field} 與本批不符")
    sources = manifest.get("sources", [])
    if not sources:
        raise ValueError("批次來源清單為空")
    if sha256(manifest["source_list_path"]) != manifest.get("source_list_sha256"):
        raise ValueError("原始來源選取清單已變更")
    if not sources_json:
        sources_json = manifest["source_list_path"]
    actual_paths = [str(Path(s["path"]).resolve()).casefold() for s in sources]
    if len(actual_paths) != len(set(actual_paths)):
        raise ValueError("批次來源重複")
    if sources_json:
        selected = read_json(sources_json)
        if not isinstance(selected, list) or sorted(actual_paths) != sorted(str(Path(p).resolve()).casefold() for p in selected):
            raise ValueError("批次清單未完整對應啟動器選定的 PDF")
    expected_pages = set()
    for source in sources:
        pdf = Path(source["path"])
        if sha256(pdf) != source["sha256"]:
            raise ValueError(f"來源 PDF 已變更：{pdf.name}")
        if not isinstance(source["pages"], int) or source["pages"] < 1:
            raise ValueError("來源頁數無效")
        for page in range(1, source["pages"] + 1):
            expected_pages.add((pdf.name, page))
    page_results = manifest.get("pages", [])
    if len(page_results) != len(expected_pages) or {(p["source"], p["page"]) for p in page_results} != expected_pages:
        raise ValueError("OCR 頁面清單有漏頁或額外頁面")
    for page in page_results:
        if page.get("status") != "done" or sha256(page["ocr_path"]) != page.get("ocr_sha256"):
            raise ValueError("OCR 頁面未完成或結果已變更")
        text = Path(page["ocr_path"]).read_text(encoding="utf-8")
        if "embedded-text" in text or "[y" not in text:
            raise ValueError("OCR 頁面缺少座標資料")
    parsed = manifest.get("parse", {}).get("pages", [])
    if len(parsed) != len(expected_pages) or {(p["source"], p["page"]) for p in parsed} != expected_pages:
        raise ValueError("解析清單有漏頁")
    resolutions = manifest.get("resolutions", {})
    if not isinstance(resolutions, dict):
        raise ValueError("人工處理紀錄須為物件")
    expected_counts = {}
    for page in parsed:
        if page["parsed"] < 1 or page["kept"] < 0 or page["duplicates"] < 0 or page["parsed"] != page["kept"] + page["duplicates"]:
            raise ValueError("解析筆數與重掃紀錄不符")
        key = f"{page['source']} p{page['page']}"
        count = page["kept"]
        if key in resolutions:
            resolution = resolutions[key]
            count = resolution.get("row_count")
            if not isinstance(count, int) or isinstance(count, bool) or count < 0 or not str(resolution.get("reason", "")).strip():
                raise ValueError(f"{key}：增減列須記錄目視複核原因及最終筆數")
        expected_counts[key] = count
    if set(resolutions) - set(expected_counts):
        raise ValueError("人工處理紀錄含本批不存在的頁面")
    actual_counts = Counter(row["來源檔案"] for row in rows)
    if +Counter(expected_counts) != actual_counts:
        raise ValueError("最終 CSV 漏列或多列；請核對來源頁面，必要時記錄 resolutions")
    return manifest
