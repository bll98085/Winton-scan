"""One OCR process for an entire batch, with hash-checked per-page/crop resume."""
import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

from batch_support import atomic_json, atomic_text, read_json, sha256


def preflight(sources_json, template=None):
    import pypdfium2 as pdfium
    import PIL.Image  # Fail before OCR if a required dependency is unavailable.
    import ocr_page
    ocr_page.load_engines()
    if not ocr_page.winocr.OcrEngine.is_language_supported(ocr_page.winocr.Language(ocr_page.WINOCR_LANG)):
        raise ValueError("Windows 缺少 zh-Hant-TW OCR 語言；請先完成環境設定")
    if template:
        template = Path(template)
        with template.open("rb") as stream:
            if template.suffix.lower() != ".xls" or stream.read(8) != bytes.fromhex("D0CF11E0A1B11AE1"):
                raise ValueError("範本須為可讀取的原生 XLS")
    selected = read_json(sources_json)
    if not isinstance(selected, list) or not selected or not all(isinstance(p, str) for p in selected):
        raise ValueError("來源清單須為非空的 PDF 路徑陣列")
    files = [Path(p).resolve(strict=True) for p in selected]
    if any(p.suffix.lower() != ".pdf" for p in files):
        raise ValueError("來源清單只能包含 PDF")
    if len({p.name.casefold() for p in files}) != len(files):
        raise ValueError("同批 PDF 檔名不能重複，請先使用不同檔名")
    if len({p.parent for p in files}) != 1:
        raise ValueError("同批 PDF 須位於同一資料夾，以維持來源與裁圖對應")
    sources = []
    for pdf in files:
        with pdfium.PdfDocument(pdf) as doc:
            count = len(doc)
        if count < 1:
            raise ValueError(f"PDF 沒有頁面：{pdf.name}")
        sources.append({"path": str(pdf), "sha256": sha256(pdf), "pages": count})
    return sources


class SharedOCR:
    def __init__(self, cache_dir, sources):
        import ocr_page
        ocr_page.load_engines()
        versions = {}
        for package in ("pypdfium2", "Pillow", "winocr", "rapidocr-onnxruntime", "onnxruntime"):
            try:
                versions[package] = importlib.metadata.version(package)
            except importlib.metadata.PackageNotFoundError:
                versions[package] = "absent"
        versions["engine"] = "rapidocr" if ocr_page._HAVE_RAPID else "windows"
        versions["ocr_code"] = sha256(Path(__file__).with_name("ocr_page.py"))
        self.version = hashlib.sha256(json.dumps(versions, sort_keys=True).encode()).hexdigest()
        self.cache_dir = Path(cache_dir)
        self.sources = {str(Path(s["path"]).resolve()): s["sha256"] for s in sources}
        self.documents = {}
        self.hits = self.misses = 0

    def recognize(self, pdf, page, previous=None, rotation=None, region=None, scale=3.0):
        import ocr_page
        import pypdfium2 as pdfium
        pdf = str(Path(pdf).resolve())
        identity = {"pdf": self.sources[pdf], "page": page, "rotation": rotation,
                    "region": region, "scale": scale, "version": self.version}
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        cache = self.cache_dir / key[:2] / (key + ".json")
        try:
            saved = read_json(cache)
            text = saved["text"]
            valid = saved["identity"] == identity and saved["sha256"] == hashlib.sha256(text.encode()).hexdigest()
            valid = valid and re.search(rf"^== page {page} rotation \d+ ", text) and "embedded-text" not in text
            if valid and re.search(r"^\[y[0-9.]+ x[0-9.]+\] .+", text, re.M):
                self.hits += 1
                return text, saved["rotation"], True
        except (OSError, ValueError, KeyError, TypeError):
            pass
        # Keep only one PDF open, but retain the recognition models for the process.
        if pdf not in self.documents:
            self.close()
            self.documents[pdf] = pdfium.PdfDocument(pdf)
        text, actual_rotation = ocr_page.process_page(self.documents[pdf], page, previous,
            forced_rot=rotation, region=region, scale=scale, force_ocr=True)
        self.misses += 1
        if not re.search(r"^\[y[0-9.]+ x[0-9.]+\] .+", text, re.M):
            if region is None:
                raise ValueError(f"{Path(pdf).name} p{page}：OCR 無文字，須檢查空白／影像品質，未標記完成")
            return text, actual_rotation, False
        atomic_json(cache, {"identity": identity, "text": text, "rotation": actual_rotation,
                            "sha256": hashlib.sha256(text.encode()).hexdigest()})
        return text, actual_rotation, False

    def refine(self, pdf, page, rotation, region, scale):
        return self.recognize(pdf, page, rotation=rotation, region=region, scale=scale)[0]

    def close(self):
        for doc in self.documents.values():
            doc.close()
        self.documents.clear()


def run_batch(sources_json, workdir, cache_dir, user_code, submission_month, template=None):
    from build_winton_import import USERS, roc_month
    from parse_bills import main as parse
    if user_code not in USERS:
        raise ValueError("客戶代號不在選單內")
    roc_month(submission_month)
    sources = preflight(sources_json, template)
    workdir = Path(workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    manifest_path = workdir / "manifest.json"
    ocr = SharedOCR(cache_dir, sources)
    workflow_version = hashlib.sha256("".join(sha256(Path(__file__).with_name(name)) for name in
        ("run_batch.py", "parse_bills.py", "import_fields.py", "build_winton_import.py", "batch_support.py")).encode()).hexdigest()
    identity = {"schema_version": 1, "sources": sources, "user_code": user_code,
        "submission_month": submission_month, "ocr_version": ocr.version,
        "workflow_version": workflow_version, "source_list_path": str(Path(sources_json).resolve()),
        "source_list_sha256": sha256(sources_json)}
    if manifest_path.exists():
        previous = read_json(manifest_path)
        if any(previous.get(k) != v for k, v in identity.items()):
            raise ValueError("暫存批次的來源／參數／程式版本已變更；請使用新 WorkDir，仍可共用 OCR 快取")
        if previous.get("status") == "parsed":
            artifacts = previous.get("artifacts", {})
            if not artifacts or any(not Path(p).is_file() or sha256(p) != h for p, h in artifacts.items()):
                raise ValueError("已完成的草稿有人工變更或遺失；請保留複核資料，另開 WorkDir 重解析")
            print(f"此批次已完成，保留現有草稿：{manifest_path}")
            return previous
    manifest = dict(identity, status="ocr", pages=[], resolutions={})
    started = time.monotonic()
    atomic_json(manifest_path, manifest)
    files = []
    try:
        total_pages = sum(s["pages"] for s in sources)
        for source in sources:
            pdf = Path(source["path"])
            previous_rotation = None
            for page in range(1, source["pages"] + 1):
                text, rotation, cached = ocr.recognize(pdf, page, previous_rotation)
                previous_rotation = rotation
                output = workdir / "ocr" / f"{pdf.stem}_p{page}.txt"
                atomic_text(output, text)
                files.append(str(output))
                manifest["pages"].append({"source": pdf.name, "page": page, "status": "done",
                    "rotation": rotation, "ocr_path": str(output), "ocr_sha256": sha256(output), "cached": cached})
                atomic_json(manifest_path, manifest)
                print(f"[{len(files)}/{total_pages}] {pdf.name} p{page}：{'沿用已驗證快取' if cached else '辨識完成'}", flush=True)
        manifest["status"] = "parsing"
        atomic_json(manifest_path, manifest)
        report = parse(["--refine", str(Path(sources[0]["path"]).parent),
            "--flags-json", str(workdir / "flags.json"), "--review-json", str(workdir / "review.json"),
            "--details-json", str(workdir / "details.draft.json"), "-o", str(workdir / "draft.csv"), *files],
            ocr_backend=ocr.refine)
        if any(sha256(s["path"]) != s["sha256"] for s in sources):
            raise ValueError("來源 PDF 在辨識期間變更，請重新建立批次")
        manifest.update(status="parsed", parse=report,
            stats={"cache_hits": ocr.hits, "ocr_calls": ocr.misses, "seconds": round(time.monotonic() - started, 2)},
            artifacts={str(workdir / name): sha256(workdir / name) for name in
                       ("draft.csv", "details.draft.json", "review.json", "flags.json")})
        atomic_json(manifest_path, manifest)
        print(f"草稿與統一待確認清單已完成：{workdir}")
        return manifest
    except Exception as error:
        manifest.update(status="failed", error=f"{type(error).__name__}: {error}")
        atomic_json(manifest_path, manifest)
        raise
    finally:
        ocr.close()


def main():
    ap = argparse.ArgumentParser(description="整批帳單辨識、續跑及統一複核資料")
    ap.add_argument("--sources-json", required=True, type=Path)
    ap.add_argument("--workdir", type=Path)
    ap.add_argument("--cache-dir", type=Path, default=Path(tempfile.gettempdir()) / "winton_ocr_cache")
    ap.add_argument("--user-code")
    ap.add_argument("--submission-month")
    ap.add_argument("--template", type=Path)
    ap.add_argument("--preflight", action="store_true")
    args = ap.parse_args()
    if args.preflight:
        sources = preflight(args.sources_json, args.template)
        print(f"環境與來源檢查通過：{len(sources)} 份 PDF，共 {sum(s['pages'] for s in sources)} 頁")
        return
    if not args.workdir or not args.user_code or not args.submission_month:
        ap.error("請提供 --workdir、--user-code、--submission-month")
    run_batch(args.sources_json, args.workdir, args.cache_dir, args.user_code, args.submission_month, args.template)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        main()
    except Exception as error:
        print(f"批次未完成：{error}", file=sys.stderr)
        sys.exit(1)
