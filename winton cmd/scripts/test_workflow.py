"""Offline regression tests. All generated files stay in isolated temporary folders."""
import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import parse_bills as parser
from batch_support import atomic_json, atomic_text, read_csv, sha256, validate_manifest
from build_winton_import import build_payload
from import_fields import extract_import_fields

BASE = [(0.95, .1, "10-33-3054-11-5"),
        (.90, .1, "本公司營利事業統一編號03795904"),
        (.85, .1, "用電地址:台中市龍井區新興路3巷18號4樓"),
        (.80, .1, "115/03/01至115/04/30"), (.75, .1, "***1,050元"),
        (.65, .1, "經常度數"), (.65, .3, "300"),
        (.55, .1, "本期載具號碼"), (.54, .1, "11504BB12345678")]
TAX = [(.70, .1, "稅前"), (.70, .3, "1,000元"), (.60, .1, "營業稅50元")]


def fragments(lines):
    return [parser.Frag(*line) for line in lines]


def bill(lines):
    return parser.extract_bill("10333054115", fragments(lines))


def csv_row(r):
    return dict(zip(("來源檔案", "帳單類型", "電號/水號/電話號碼", "發票號碼", "統一編號",
                     "計費期間", "收款日期", "應繳總金額", "營業稅", "備註(不確定標記)"),
                    ("a.pdf p1", r["type"], r["number"], r["invoice"], r["ubn"], r["period"],
                     r.get("receipt_date", "115/05/01"), r["total"], r["tax"], r["notes"])))


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="winton_test_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def fixture(self, name="a", lines=None):
        path = self.root / (name + "_p1.txt")
        atomic_text(path, "== page 1 rotation 0 score 100 ==\n" + "\n".join(
            f"[y{y:.3f} x{x:.2f}] {text}" for y, x, text in (BASE + TAX if lines is None else lines)))
        return path

    def parse(self, files):
        report = parser.main(["--flags-json", str(self.root / "flags.json"),
            "--details-json", str(self.root / "details.json"), "--review-json", str(self.root / "review.json"),
            "-o", str(self.root / "draft.csv"), *map(str, files)])
        return read_csv(self.root / "draft.csv"), report

    def payload(self, r, detail=None):
        return build_payload([csv_row(r)], "H11037", "11510",
            {r["number"]: dict(invoice_date="115/05/01", **(detail or {}))})

    def test_derived_tax_cannot_be_imported_as_original(self):
        r = bill(BASE)
        self.assertEqual(r["tax"], "50")
        self.assertTrue(any("推算" in f for f in r["_flags"]))
        payload = self.payload(r)
        self.assertIsNone(payload["rows"][0][7])
        self.assertIsNone(payload["rows"][0][12])
        self.assertTrue(payload["reviews"])

    def test_tax_ratio_checked_even_when_sum_matches(self):
        r = bill(BASE + [(.7, .1, "稅前"), (.7, .3, "900元"), (.6, .1, "營業稅150元")])
        self.assertTrue(any("營業稅檢核不符" in f for f in r["_flags"]))
        self.assertIsNone(self.payload(r)["rows"][0][12])

    def test_source_tax_and_leading_zero_ubn_preserved(self):
        payload = self.payload(bill(BASE + TAX))
        self.assertEqual(payload["rows"][0][7], "Y")
        self.assertEqual(payload["rows"][0][12], 50)
        self.assertEqual(payload["rows"][0][14], "03795904")

    def test_repaired_pretax_does_not_invalidate_original_tax(self):
        r = bill(BASE + [(.7, .1, "稅前"), (.7, .3, "900元"), (.6, .1, "營業稅50元")])
        self.assertEqual(r["_tax_status"], "source")
        self.assertEqual(self.payload(r)["rows"][0][12], 50)

    def test_reviewed_tax_requires_evidence(self):
        with self.assertRaises(ValueError):
            self.payload(bill(BASE), {"tax_status": "reviewed", "tax_amount": "50"})
        payload = self.payload(bill(BASE), {"tax_status": "reviewed", "tax_amount": "50", "evidence": "a.pdf p1 營業稅欄目視50元"})
        self.assertEqual(payload["rows"][0][12], 50)

    def test_different_invoices_are_retained_and_block_export(self):
        other = [(y, x, t.replace("BB12345678", "BB87654321")) for y, x, t in BASE + TAX]
        rows, report = self.parse([self.fixture(), self.fixture("b", other)])
        self.assertEqual(len(rows), 2)
        self.assertTrue(all("保留衝突列" in row["備註(不確定標記)"] for row in rows))
        self.assertEqual(report["dropped_duplicates"], [])
        with self.assertRaises(ValueError):
            build_payload(rows, "H11037", "11510")

    def test_identical_rescan_is_accounted_for(self):
        rows, report = self.parse([self.fixture(), self.fixture("b")])
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(report["dropped_duplicates"]), 1)
        self.assertEqual(sum(p["parsed"] for p in report["pages"]), 2)

    def test_different_tax_is_not_dropped(self):
        other = [(y, x, t.replace("1,000元", "900元").replace("營業稅50元", "營業稅150元")) for y, x, t in BASE + TAX]
        rows, _ = self.parse([self.fixture(), self.fixture("b", other)])
        self.assertEqual(len(rows), 2)

    def test_missing_invoice_never_proves_rescan(self):
        lines = [v for v in BASE + TAX if "BB123" not in v[2]]
        rows, _ = self.parse([self.fixture(lines=lines), self.fixture("b", lines)])
        self.assertEqual(len(rows), 2)

    def test_different_carrier_period_is_not_dropped(self):
        lines = [(y, x, t.replace("11504BB", "11506BB")) for y, x, t in BASE + TAX]
        rows, _ = self.parse([self.fixture(), self.fixture("b", lines)])
        self.assertEqual(len(rows), 2)

    def test_embedded_page_fails_before_overwriting_draft(self):
        draft = self.root / "draft.csv"
        draft.write_text("existing reviewed data", encoding="utf-8")
        embedded = self.root / "digital_p1.txt"
        embedded.write_text("== page 1 embedded-text ==\n原生文字", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.parse([self.fixture(), embedded])
        self.assertEqual(draft.read_text(encoding="utf-8"), "existing reviewed data")

    def test_empty_ocr_page_is_not_silently_skipped(self):
        with self.assertRaises(ValueError):
            self.parse([self.fixture(lines=[])])

    def test_collection_date_prefers_receipt_stamp_then_payment_date(self):
        value, source, _, flags = parser.extract_collection_date(fragments([
            (.40, .10, "收款章Receipt Stamp"), (.36, .11, "1150502"),
            (.80, .10, "繳費日期Payment Date"), (.77, .11, "115/05/01")]))
        self.assertEqual((value, source, flags), ("115/05/02", "收款章", []))
        value, source, _, flags = parser.extract_collection_date(fragments([
            (.80, .10, "繳費日期Payment Date"), (.77, .11, "115/05/01")]))
        self.assertEqual((value, source, flags), ("115/05/01", "繳費日期", []))

    def test_ambiguous_receipt_dates_stay_blank(self):
        value, _, _, flags = parser.extract_collection_date(fragments([
            (.40, .10, "收款章"), (.36, .10, "1150501"), (.35, .11, "1150502")]))
        self.assertEqual(value, "")
        self.assertTrue(any("多個日期" in flag for flag in flags))

    def test_telecom_total_is_not_assumed_invoice_gross(self):
        r = bill(BASE + TAX)
        r["type"], r["tax"] = "電信費", ""
        payload = self.payload(r, {"tax_amount": "25", "tax_status": "source"})
        self.assertIsNone(payload["rows"][0][10])

    def test_missing_collection_date_is_visible_in_csv_and_review_queue(self):
        rows, report = self.parse([self.fixture()])
        self.assertIn("⚠️收款日期", rows[0]["備註(不確定標記)"])
        self.assertTrue(any("日期" in f for f in report["review"][0]["flags"]))
        self.assertTrue(report["review"][0]["full_page"])

    def coverage(self):
        rows, report = self.parse([self.fixture()])
        pdf = self.root / "a.pdf"
        pdf.write_bytes(b"synthetic source for integrity checks")
        selected = self.root / "sources.json"
        atomic_json(selected, [str(pdf)])
        ocr = self.root / "a_p1.txt"
        manifest = {"schema_version": 1, "status": "parsed", "user_code": "H11037", "submission_month": "11510",
            "source_list_path": str(selected), "source_list_sha256": sha256(selected),
            "sources": [{"path": str(pdf), "sha256": sha256(pdf), "pages": 1}],
            "pages": [{"source": "a.pdf", "page": 1, "status": "done", "ocr_path": str(ocr), "ocr_sha256": sha256(ocr)}],
            "parse": report, "resolutions": {}}
        path = self.root / "manifest.json"
        atomic_json(path, manifest)
        return rows, path, manifest

    def test_coverage_accepts_complete_batch(self):
        rows, path, _ = self.coverage()
        validate_manifest(path, rows, "H11037", "11510")

    def test_coverage_rejects_missing_page(self):
        rows, path, manifest = self.coverage()
        manifest["sources"][0]["pages"] = 2
        atomic_json(path, manifest)
        with self.assertRaises(ValueError):
            validate_manifest(path, rows)

    def test_coverage_rejects_deleted_csv_row(self):
        rows, path, _ = self.coverage()
        with self.assertRaises(ValueError):
            validate_manifest(path, [])

    def test_coverage_rejects_changed_pdf(self):
        rows, path, _ = self.coverage()
        (self.root / "a.pdf").write_bytes(b"changed")
        with self.assertRaises(ValueError):
            validate_manifest(path, rows)

    def test_coverage_rejects_changed_source_selection(self):
        rows, path, _ = self.coverage()
        atomic_json(self.root / "sources.json", [])
        with self.assertRaises(ValueError):
            validate_manifest(path, rows)

    def test_row_exclusion_needs_reason(self):
        rows, path, manifest = self.coverage()
        manifest["resolutions"] = {"a.pdf p1": {"row_count": 0}}
        atomic_json(path, manifest)
        with self.assertRaises(ValueError):
            validate_manifest(path, [])
        manifest["resolutions"]["a.pdf p1"]["reason"] = "與另一頁同一發票，目視確認重掃"
        atomic_json(path, manifest)
        validate_manifest(path, [])

    def test_cache_reuses_page_and_crop_but_rejects_corruption(self):
        import ocr_page
        from run_batch import SharedOCR
        pdf = self.root / "a.pdf"
        pdf.write_bytes(b"fixture")
        sources = [{"path": str(pdf), "sha256": sha256(pdf)}]
        text = self.fixture().read_text(encoding="utf-8")
        with patch.object(ocr_page, "load_engines"), patch("pypdfium2.PdfDocument") as doc, patch.object(ocr_page, "process_page", return_value=(text, 0)) as recognize:
            worker = SharedOCR(self.root / "cache", sources)
            self.assertFalse(worker.recognize(pdf, 1)[2])
            self.assertTrue(worker.recognize(pdf, 1)[2])
            worker.refine(pdf, 1, 0, [.1, .2, .4, .5], 12)
            worker.refine(pdf, 1, 0, [.1, .2, .4, .5], 12)
            self.assertEqual(recognize.call_count, 2)
            self.assertEqual(doc.call_count, 1)
            for cache in (self.root / "cache").rglob("*.json"):
                cache.write_text("corrupt", encoding="utf-8")
            self.assertFalse(worker.recognize(pdf, 1)[2])
            worker.close()

    def test_cache_invalidates_changed_source_and_engine(self):
        import ocr_page
        from run_batch import SharedOCR
        pdf = self.root / "a.pdf"
        pdf.write_bytes(b"before")
        text = self.fixture().read_text(encoding="utf-8")
        with patch.object(ocr_page, "load_engines"), patch("pypdfium2.PdfDocument"), patch.object(ocr_page, "process_page", return_value=(text, 0)) as recognize:
            def worker():
                return SharedOCR(self.root / "cache", [{"path": str(pdf), "sha256": sha256(pdf)}])
            first = worker()
            first.recognize(pdf, 1)
            first.close()
            pdf.write_bytes(b"after")
            second = worker()
            self.assertFalse(second.recognize(pdf, 1)[2])
            second.version = "changed-engine-version"
            self.assertFalse(second.recognize(pdf, 1)[2])
            second.close()
            self.assertEqual(recognize.call_count, 3)

    def test_interrupted_batch_resumes_only_unfinished_page(self):
        import ocr_page
        import run_batch
        pdfs = [self.root / "a.pdf", self.root / "b.pdf"]
        for i, pdf in enumerate(pdfs):
            pdf.write_bytes(f"fixture-{i}".encode())
        sources = [{"path": str(p), "sha256": sha256(p), "pages": 1} for p in pdfs]
        selected = self.root / "sources.json"
        atomic_json(selected, list(map(str, pdfs)))
        text = self.fixture().read_text(encoding="utf-8")
        work = self.root / "work"
        with patch.object(run_batch, "preflight", return_value=sources), patch.object(ocr_page, "load_engines"), patch("pypdfium2.PdfDocument"), patch.object(ocr_page, "process_page", side_effect=[(text, 0), RuntimeError("simulated interruption")]):
            with self.assertRaises(RuntimeError):
                run_batch.run_batch(selected, work, self.root / "cache", "H11037", "11510")
        self.assertEqual(json.loads((work / "manifest.json").read_text(encoding="utf-8"))["status"], "failed")
        with patch.object(run_batch, "preflight", return_value=sources), patch.object(ocr_page, "load_engines"), patch("pypdfium2.PdfDocument"), patch.object(ocr_page, "process_page", return_value=(text, 0)) as recognize:
            result = run_batch.run_batch(selected, work, self.root / "cache", "H11037", "11510")
            self.assertEqual(recognize.call_count, 1)
            self.assertEqual(result["stats"]["cache_hits"], 1)
            self.assertEqual(result["status"], "parsed")
            before = (work / "draft.csv").read_bytes()
            run_batch.run_batch(selected, work, self.root / "cache", "H11037", "11510")
            self.assertEqual(recognize.call_count, 1)
            self.assertEqual((work / "draft.csv").read_bytes(), before)
            (work / "draft.csv").write_bytes(before + b"manual edit")
            with self.assertRaises(ValueError):
                run_batch.run_batch(selected, work, self.root / "cache", "H11037", "11510")


if __name__ == "__main__":
    unittest.main(verbosity=2)
