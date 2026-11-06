import importlib
import importlib.metadata
from pathlib import Path
import struct
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
failed = []
print(f"Python: {sys.version.split()[0]} ({struct.calcsize('P') * 8} bit)")
if sys.version_info[:2] != (3, 12):
    print("[WARN] 本包依來源電腦 Python 3.12 整理，其他版本尚未驗證。")
for package, module in [("pypdfium2", "pypdfium2"), ("Pillow", "PIL.Image"),
                        ("rapidocr-onnxruntime", "rapidocr_onnxruntime"), ("winocr", "winocr")]:
    try:
        importlib.import_module(module)
        print(f"[OK] {package} {importlib.metadata.version(package)}")
    except Exception as error:
        failed.append(package)
        print(f"[FAIL] {package}: {error}")
try:
    import ocr_page
    ocr_page.load_engines()
    if not ocr_page.winocr.OcrEngine.is_language_supported(ocr_page.winocr.Language(ocr_page.WINOCR_LANG)):
        raise RuntimeError("未安裝 Windows 繁體中文（台灣）OCR 語言")
    print("[OK] Windows zh-Hant-TW OCR")
    if ocr_page._HAVE_RAPID:
        ocr_page.rapid()
        print("[OK] RapidOCR 模型可載入")
except Exception as error:
    failed.append("OCR")
    print(f"[FAIL] OCR: {error}")
try:
    template = Path(__file__).resolve().parent.parent / "assets" / "進項憑證明細檔.xls"
    with template.open("rb") as stream:
        if stream.read(8) != bytes.fromhex("D0CF11E0A1B11AE1"):
            raise ValueError("範本不是原生 XLS")
    print("[OK] 本地 XLS 範本")
except Exception as error:
    failed.append("Template")
    print(f"[FAIL] 範本: {error}")
sys.exit(1 if failed else 0)
