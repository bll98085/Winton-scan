"""Offline recovery tests: synthetic files and a fake Codex; no API or Excel calls."""
import csv
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import review_checkpoint as review
from batch_support import CSV_COLUMNS, atomic_json, atomic_text, read_json, sha256, read_csv, validate_manifest

SCRIPTS = Path(__file__).resolve().parent


def powershell_env(**updates):
    env = dict(os.environ)
    if os.name == "nt" and not env.get("PSModulePath"):
        env["PSModulePath"] = str(Path(os.environ.get("WINDIR", r"C:\Windows")) /
                                  "System32" / "WindowsPowerShell" / "v1.0" / "Modules")
    env.update({key: str(value) for key, value in updates.items()})
    return env


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="winton_review_test_")
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        pdf = self.work / "bill.pdf"
        pdf.write_bytes(b"source fixture")
        ocr = self.work / "bill_p1.txt"
        atomic_text(ocr, "== page 1 rotation 0 ==\n[y0.500 x0.10] fixture")
        atomic_json(self.work / "sources.json", [str(pdf)])
        rows = []
        for i in range(6):
            row = dict.fromkeys(CSV_COLUMNS, "")
            row.update({review.SOURCE: "bill.pdf p1", review.NUMBER: f"001234{i}", "帳單類型": "電費",
                        "應繳總金額": "1050", "營業稅": "50", "計費期間": "115/03/01-115/04/30"})
            rows.append(row)
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS)
        writer.writeheader(); writer.writerows(rows)
        atomic_text(self.work / "draft.csv", buffer.getvalue(), "utf-8-sig")
        atomic_json(self.work / "details.draft.json", {r[review.NUMBER]: {"tax_status": "source", "tax_amount": "50"} for r in rows})
        atomic_json(self.work / "flags.json", [])
        atomic_json(self.work / "review.json", {})
        manifest = {"schema_version": 1, "status": "parsed", "user_code": "H06062", "submission_month": "11511",
                    "source_list_path": str(self.work / "sources.json"), "source_list_sha256": sha256(self.work / "sources.json"),
                    "sources": [{"path": str(pdf), "sha256": sha256(pdf), "pages": 1}],
                    "pages": [{"source": "bill.pdf", "page": 1, "status": "done", "ocr_path": str(ocr), "ocr_sha256": sha256(ocr)}],
                    "parse": {"pages": [{"source": "bill.pdf", "page": 1, "parsed": 6, "kept": 6, "duplicates": 0}]},
                    "resolutions": {}, "artifacts": {str(self.work / name): sha256(self.work / name) for name in
                        ["draft.csv", "details.draft.json", "flags.json", "review.json"]}}
        atomic_json(self.work / "manifest.json", manifest)
        self.plan = review.initialize(self.work)

    def candidate(self, batch):
        return {"plan_id": self.plan["plan_id"], "batch": batch,
                "records": [{"index": r["index"], "evidence": "bill.pdf p1: verified tax label; date unavailable",
                             "reason": "", "rows": [{"csv_updates": {}, "details_updates": {}}]}
                            for r in review.selected(self.plan, batch)]}

    def save(self, batch, candidate=None):
        path = self.work / "candidate.json"
        atomic_json(path, candidate or self.candidate(batch))
        return review.commit(self.work, batch, path)

    def test_resume_skips_completed_batch_and_merges_idempotently(self):
        self.save(1)
        self.assertEqual(review.status(self.work)["next_batch"], 2)
        self.assertEqual(review.initialize(self.work)["plan_id"], self.plan["plan_id"])
        self.save(2)
        csv_path, detail_path = self.work / "final.csv", self.work / "details.json"
        review.materialize(self.work, csv_path, detail_path)
        before = csv_path.read_bytes()
        review.materialize(self.work, csv_path, detail_path)
        self.assertEqual(before, csv_path.read_bytes())
        self.assertEqual(len(read_csv(csv_path)), 6)
        self.assertEqual(read_csv(csv_path)[0][review.NUMBER], "0012340")
        validate_manifest(self.work / "reviewed_manifest.json", read_csv(csv_path))

    def test_missing_duplicate_or_foreign_rows_cannot_commit(self):
        for mode in ("missing", "duplicate", "foreign"):
            candidate = self.candidate(1)
            if mode == "missing": candidate["records"].pop()
            elif mode == "duplicate": candidate["records"][1]["index"] = 1
            else: candidate["records"][0]["index"] = 999
            with self.assertRaises(ValueError): self.save(1, candidate)
        self.assertEqual(review.status(self.work)["completed_batches"], 0)

    def test_incomplete_review_never_materializes(self):
        self.save(1)
        with self.assertRaises(ValueError): review.materialize(self.work, self.work / "final.csv", self.work / "details.json")
        self.assertFalse((self.work / "final.csv").exists())

    def test_changed_pdf_blocks_resume(self):
        self.save(1)
        (self.work / "bill.pdf").write_bytes(b"changed")
        with self.assertRaises(ValueError): review.status(self.work)

    def test_changed_draft_blocks_resume(self):
        with (self.work / "draft.csv").open("ab") as stream: stream.write(b"changed")
        with self.assertRaises(ValueError): review.status(self.work)

    def test_different_checkpoint_cannot_overwrite(self):
        self.save(1)
        c = self.candidate(1)
        c["records"][0]["rows"][0]["csv_updates"] = {"地址": "changed"}
        with self.assertRaises(ValueError): self.save(1, c)
        self.save(1)

    def test_tax_review_requires_evidence(self):
        c = self.candidate(1)
        c["records"][0]["rows"][0]["details_updates"] = {"tax_status": "reviewed", "evidence": ""}
        with self.assertRaises(ValueError): self.save(1, c)

    def test_row_removal_requires_reason_and_updates_coverage(self):
        c = self.candidate(1)
        c["records"][0]["rows"] = []
        with self.assertRaises(ValueError): self.save(1, c)
        c["records"][0]["reason"] = "confirmed rescan of bill 2 with identical invoice and amounts"
        self.save(1, c); self.save(2)
        review.materialize(self.work, self.work / "final.csv", self.work / "details.json")
        self.assertEqual(read_json(self.work / "reviewed_manifest.json")["resolutions"]["bill.pdf p1"]["row_count"], 5)
        self.assertEqual(read_json(self.work / "manifest.json")["resolutions"], {})

    def test_cross_batch_duplicate_blocks_export(self):
        self.save(1)
        c = self.candidate(2)
        c["records"][0]["rows"][0]["csv_updates"] = {review.NUMBER: "0012340"}
        self.save(2, c)
        with self.assertRaises(ValueError): review.materialize(self.work, self.work / "final.csv", self.work / "details.json")
        self.assertFalse((self.work / "final.csv").exists())

    def test_existing_output_is_preserved(self):
        self.save(1); self.save(2)
        destination = self.work / "final.csv"
        destination.write_bytes(b"manual output")
        with self.assertRaises(ValueError): review.materialize(self.work, destination, self.work / "details.json")
        self.assertEqual(destination.read_bytes(), b"manual output")

    def test_ready_marker_prevents_rematerializing(self):
        self.save(1); self.save(2)
        atomic_json(self.work / "export_ready.json", {})
        with self.assertRaises(ValueError): review.materialize(self.work, self.work / "final.csv", self.work / "details.json")

    def test_ready_marker_allows_hash_verified_recovery(self):
        self.save(1); self.save(2)
        csv_path, detail_path = self.work / "final.csv", self.work / "details.json"
        review.materialize(self.work, csv_path, detail_path)
        expected_csv, expected_details = csv_path.read_bytes(), detail_path.read_bytes()
        manifest_path = self.work / "reviewed_manifest.json"
        atomic_json(self.work / "export_ready.json", {
            "schema_version": 2,
            "csv_path": str(csv_path), "csv_sha256": hashlib.sha256(expected_csv).hexdigest(),
            "details_path": str(detail_path), "details_sha256": hashlib.sha256(expected_details).hexdigest(),
            "manifest_path": str(manifest_path), "manifest_sha256": sha256(manifest_path),
        })
        csv_path.unlink(); detail_path.unlink()
        result = review.materialize(self.work, csv_path, detail_path, recover=True)
        self.assertTrue(result["recovered"])
        self.assertEqual(csv_path.read_bytes(), expected_csv)
        self.assertEqual(detail_path.read_bytes(), expected_details)

    def launcher_metadata(self):
        template = self.work / "template.xls"
        template.write_bytes(b"template fixture")
        temporary = self.work / "isolated_temp"
        work = temporary / "winton_scan_existing"
        work.mkdir(parents=True)
        pdf = self.work / "bill.pdf"
        metadata = {"schema_version": 1, "user_code": "H06062", "submission_month": "11511", "scan_dir": str(self.work),
                    "template": str(template), "template_sha256": sha256(template).upper(), "cache_dir": str(self.work / "cache"),
                    "model": "gpt-6-luna", "csv": str(self.work / "saved.csv"), "xls": str(self.work / "saved.xls"),
                    "work_dir": str(work), "source_signature": json.dumps([{"path": str(pdf), "sha256": sha256(pdf).upper()}], separators=(",", ":"))}
        atomic_json(work / "launcher.json", metadata)
        return temporary, metadata

    def dry_run(self, temporary, metadata, extra=None):
        result = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPTS / "start_winton_scan.ps1"),
                                 "-UserCode", "H06062", "-SubmissionMonth", "11511", "-ScanDir", str(self.work),
                                 "-Template", metadata["template"], "-DryRun", *(extra or [])],
                                env=powershell_env(TEMP=temporary), capture_output=True, encoding="utf-8", errors="replace", timeout=30)
        return result

    @unittest.skipUnless(os.name == "nt", "PowerShell integration")
    def test_launcher_auto_resume_and_force_new_batch_are_read_only(self):
        temporary, metadata = self.launcher_metadata()
        before = set(self.work.rglob("*"))
        result = self.dry_run(temporary, metadata)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout[result.stdout.index("{"):])["work_dir"], metadata["work_dir"])
        result = self.dry_run(temporary, metadata, ["-NewBatch"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotEqual(json.loads(result.stdout[result.stdout.index("{"):])["work_dir"], metadata["work_dir"])
        self.assertEqual(before, set(self.work.rglob("*")))

    @unittest.skipUnless(os.name == "nt", "PowerShell integration")
    def test_launcher_explicit_resume_rejects_changed_source(self):
        temporary, metadata = self.launcher_metadata()
        result = self.dry_run(temporary, metadata, ["-ResumeWorkDir", metadata["work_dir"]])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        (self.work / "bill.pdf").write_bytes(b"changed")
        result = self.dry_run(temporary, metadata, ["-ResumeWorkDir", metadata["work_dir"]])
        self.assertNotEqual(result.returncode, 0)

    @unittest.skipUnless(os.name == "nt", "PowerShell integration")
    def test_launcher_completes_review_prepare_export_in_order(self):
        temporary, metadata = self.launcher_metadata()
        metadata["work_dir"] = str(self.work)
        atomic_json(self.work / "launcher.json", metadata)
        stub = self.work / "isolated_skill" / "scripts"
        stub.mkdir(parents=True)
        for name in ["start_winton_scan.ps1", "review_checkpoint.py", "build_winton_import.py", "batch_support.py"]:
            shutil.copy2(SCRIPTS / name, stub / name)
        atomic_text(stub / "run_batch.ps1", "param($SourcesJson,$WorkDir,$CacheDir,$UserCode,$SubmissionMonth,$Template)\nWrite-Output 'Reusing synthetic parsed fixture'", "utf-8-sig")
        fake = self.fake_codex()
        runner = str(SCRIPTS / "run_review.ps1").replace("'", "''")
        atomic_text(stub / "run_review.ps1", "param($WorkDir,$ScanDir,$CodexPath,$PythonPath,$Model,$BatchSize)\n"
                    + f"& '{runner}' -WorkDir $WorkDir -ScanDir $ScanDir -CodexPath '{fake}' -PythonPath $PythonPath -Model $Model -BatchSize $BatchSize -RetryDelaySeconds 0", "utf-8-sig")
        atomic_text(stub / "export_winton_import.ps1", '''param($Csv,$UserCode,$SubmissionMonth,$DetailsJson,$Manifest,$SourcesJson,$Template,$Output,[switch]$PrepareOnly,$ReadyFile)
$trace = Join-Path (Split-Path -Parent $ReadyFile) 'export_trace.txt'
if (-not (Test-Path -LiteralPath $Csv) -or -not (Test-Path -LiteralPath $DetailsJson)) { throw 'Missing reviewed output' }
if ([IO.Path]::GetFileName($Manifest) -ne 'reviewed_manifest.json') { throw 'Wrong manifest' }
if ($PrepareOnly) {
    [IO.File]::WriteAllText($ReadyFile, '{}')
    [IO.File]::AppendAllText($trace, "prepare\n")
} else {
    if (-not (Test-Path -LiteralPath $ReadyFile)) { throw 'Missing ready marker' }
    [IO.File]::WriteAllText($Output, 'synthetic XLS placeholder')
    [IO.File]::AppendAllText($trace, "export\n")
}
''', "utf-8-sig")
        result = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(stub / "start_winton_scan.ps1"),
                                 "-ResumeWorkDir", str(self.work)], env=powershell_env(TEMP=temporary,
                                 WINTON_TEST_SCRIPTS=SCRIPTS, WINTON_TEST_MODE="once"),
                                capture_output=True, encoding="utf-8", errors="replace", timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.work / "export_trace.txt").read_text(), "prepare\nexport\n")
        import_dir = self.work / "Excel及匯入檔"
        archived_dir = self.work / "已掃描PDF"
        pdf = self.work / "bill.pdf"
        self.assertFalse(Path(metadata["csv"]).exists())
        self.assertFalse(Path(metadata["xls"]).exists())
        self.assertEqual(len(read_csv(import_dir / Path(metadata["csv"]).name)), 6)
        self.assertTrue((import_dir / Path(metadata["xls"]).name).exists())
        self.assertFalse(pdf.exists())
        self.assertTrue((archived_dir / pdf.name).exists())
        self.assertEqual(read_json(self.work / "calls.json"), [1, 1, 2])

    def test_retry_classifier_ignores_quoted_tool_errors_and_rejects_auth_quota(self):
        path = self.work / "events.jsonl"
        for text, expected in [("stream disconnected before completion: An error occurred while processing your request", True),
                               ("stream disconnected: HTTP 401 unauthorized", False),
                               ("stream disconnected: quota exceeded", False),
                               ("unknown failure", False)]:
            atomic_text(path, json.dumps({"type": "error", "message": text}))
            self.assertEqual(review.retryable([path])["retryable"], expected)
        atomic_text(path, json.dumps({"type": "item.completed", "item": {"type": "command_execution", "output": "stream disconnected"}}))
        self.assertFalse(review.retryable([path])["retryable"])

    def fake_codex(self):
        fake = self.work / "fake_codex.py"
        atomic_text(fake, '''import sys, json, pathlib, os
sys.path.insert(0, os.environ['WINTON_TEST_SCRIPTS'])
import review_checkpoint as r
from batch_support import atomic_json, read_json
sys.stdout.reconfigure(encoding='utf-8')
prompt = sys.stdin.read()
w = pathlib.Path(sys.argv[sys.argv.index('--cd')+1])
calls = w/'calls.json'
history = read_json(calls) if calls.exists() else []
b = r.status(w)['next_batch']
history.append(b); atomic_json(calls, history)
mode = os.environ['WINTON_TEST_MODE']
if mode == 'always' or (mode == 'once' and len(history) == 1) or mode == 'auth':
    msg = 'stream disconnected before completion: ' + ('401 unauthorized' if mode == 'auth' else 'An error occurred while processing your request')
    print(json.dumps({'type':'error','message':msg}))
    print('ERROR: ' + msg, file=sys.stderr)
    sys.exit(1)
if mode == 'missing': sys.exit(0)
p = r.load_plan(w)
c = {'plan_id':p['plan_id'],'batch':b,'records':[{'index':i['index'],'evidence':'bill.pdf p1 verified','reason':'','rows':[{'csv_updates':{},'details_updates':{}}]} for i in r.selected(p,b)]}
f = w/'fake_candidate.json'; atomic_json(f,c); r.commit(w,b,f)
if mode in ('after_commit', 'auth_after_commit'):
    msg = 'stream disconnected before completion' if mode == 'after_commit' else 'stream disconnected: 401 unauthorized'
    print(json.dumps({'type':'error','message':msg})); sys.exit(1)
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'saved'}}))
''')
        cmd = self.work / "fake codex.cmd"
        cmd.write_text(f'@echo off\r\n"{sys.executable}" "{fake}" %*\r\n', encoding="utf-8")
        return cmd

    def run_fake(self, mode):
        env = dict(os.environ, WINTON_TEST_SCRIPTS=str(SCRIPTS), WINTON_TEST_MODE=mode)
        return subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPTS / "run_review.ps1"),
                               "-WorkDir", str(self.work), "-ScanDir", str(self.work), "-CodexPath", str(self.fake_codex()),
                               "-PythonPath", sys.executable, "-RetryDelaySeconds", "0"],
                              env=env, capture_output=True, encoding="utf-8", errors="replace", timeout=60)

    @unittest.skipUnless(os.name == "nt", "PowerShell integration")
    def test_disconnect_retries_only_current_batch(self):
        result = self.run_fake("once")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(read_json(self.work / "calls.json"), [1, 1, 2])
        self.assertEqual(review.status(self.work)["completed_batches"], 2)
        self.assertTrue(list((self.work / "review_batches").glob("*.stderr.log")))

    @unittest.skipUnless(os.name == "nt", "PowerShell integration")
    def test_retry_exhaustion_is_bounded_and_later_resume_succeeds(self):
        result = self.run_fake("always")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(read_json(self.work / "calls.json"), [1, 1, 1])
        result = self.run_fake("success")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(review.status(self.work)["completed_batches"], 2)

    @unittest.skipUnless(os.name == "nt", "PowerShell integration")
    def test_disconnect_after_commit_does_not_repeat_finished_work(self):
        result = self.run_fake("after_commit")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(read_json(self.work / "calls.json"), [1, 2])

    @unittest.skipUnless(os.name == "nt", "PowerShell integration")
    def test_auth_failure_is_not_retried(self):
        result = self.run_fake("auth")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(read_json(self.work / "calls.json"), [1])

    @unittest.skipUnless(os.name == "nt", "PowerShell integration")
    def test_auth_failure_after_commit_preserves_work_and_stops(self):
        result = self.run_fake("auth_after_commit")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(read_json(self.work / "calls.json"), [1])
        self.assertEqual(review.status(self.work)["completed_batches"], 1)

    @unittest.skipUnless(os.name == "nt", "PowerShell integration")
    def test_success_exit_without_checkpoint_is_failure(self):
        result = self.run_fake("missing")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(review.status(self.work)["completed_batches"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
