"""Small, immutable review batches; only validated checkpoints enter the export."""
import argparse
import copy
import csv
import hashlib
import io
import json
import os
import re
import tempfile
from collections import Counter
from pathlib import Path

from batch_support import CSV_COLUMNS, atomic_json, atomic_text, read_csv, read_json, sha256, validate_manifest
from build_winton_import import build_payload

SOURCE = "來源檔案"
NUMBER = "電號/水號/電話號碼"
NOTES = "備註(不確定標記)"


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def baseline(work):
    manifest = read_json(work / "manifest.json")
    rows = read_csv(work / "draft.csv")
    validate_manifest(work / "manifest.json", rows)
    for path, expected in manifest.get("artifacts", {}).items():
        if sha256(path) != expected:
            raise ValueError("草稿已變更，不能沿用複核進度：" + path)
    return manifest, rows


def initialize(work, size=5):
    if not 1 <= size <= 10:
        raise ValueError("每批須為 1 至 10 筆")
    manifest, rows = baseline(work)
    plan_path = work / "review_batches" / "plan.json"
    binding = {name: sha256(work / name) for name in
               ("draft.csv", "details.draft.json", "flags.json", "review.json", "manifest.json", "sources.json")}
    if plan_path.exists():
        plan = read_json(plan_path)
        if plan["binding"] != binding:
            raise ValueError("來源或草稿已變更，不能沿用複核進度")
        return plan
    flags = read_json(work / "flags.json")
    details = read_json(work / "details.draft.json")
    counts = Counter(row[NUMBER] for row in rows)
    records = []
    for index, row in enumerate(rows, 1):
        matches = [(i, f) for i, f in enumerate(flags, 1)
                   if f"{f['source']} p{f['page']}" == row[SOURCE] and f.get("number", "") == row[NUMBER]]
        candidates = matches[0][1].get("detail_candidates", {}) if len(matches) == 1 else {}
        detail = candidates or (details.get(row[NUMBER], {}) if counts[row[NUMBER]] == 1 else {})
        crops = []
        for i, flag in matches:
            number = (flag.get("number") or flag.get("type") or "row").replace("/", "-")
            crops.append(str(work / "crops" / f"crop_{i}_{Path(flag['source']).stem}_p{flag['page']}_{number}.png"))
        records.append({"index": index, "csv": row, "details": detail,
                        "flags": [s for _, f in matches for s in f.get("flags", [])], "crops": crops})
    plan = {"schema_version": 1, "binding": binding, "batch_size": size,
            "user_code": manifest["user_code"], "submission_month": manifest["submission_month"],
            "records": records}
    plan["plan_id"] = digest(plan)
    atomic_json(plan_path, plan)
    return plan


def load_plan(work):
    plan = read_json(work / "review_batches" / "plan.json")
    if digest({k: v for k, v in plan.items() if k != "plan_id"}) != plan["plan_id"]:
        raise ValueError("複核計畫已變更")
    for name, expected in plan["binding"].items():
        if sha256(work / name) != expected:
            raise ValueError("複核輸入已變更：" + name)
    baseline(work)
    return plan


def selected(plan, batch):
    start = (batch - 1) * plan["batch_size"]
    items = plan["records"][start:start + plan["batch_size"]] if batch > 0 else []
    if not items:
        raise ValueError("複核批號無效")
    return items


def validate_candidate(plan, batch, candidate):
    originals = {r["index"]: r for r in selected(plan, batch)}
    if candidate.get("plan_id") != plan["plan_id"] or candidate.get("batch") != batch:
        raise ValueError("複核結果不屬於本批")
    records = candidate.get("records", [])
    ids = [r.get("index") for r in records]
    if len(ids) != len(originals) or set(ids) != set(originals):
        raise ValueError("複核結果有漏列、重複列或額外列")
    normalized = []
    for result in sorted(records, key=lambda r: r["index"]):
        original = originals[result["index"]]
        evidence = result.get("evidence", "")
        if not isinstance(evidence, str) or not evidence.strip():
            raise ValueError("每筆須記錄核對欄位、來源頁碼或無法確認原因")
        outputs = result.get("rows")
        if not isinstance(outputs, list):
            raise ValueError("rows 須為陣列")
        reason = result.get("reason", "")
        if len(outputs) != 1 and (not isinstance(reason, str) or not reason.strip()):
            raise ValueError("增減列須記錄原單判定原因")
        merged = []
        for output in outputs:
            updates = output.get("csv_updates", {})
            detail_updates = output.get("details_updates", {})
            if not isinstance(updates, dict) or set(updates) - set(CSV_COLUMNS):
                raise ValueError("CSV 修改包含未知欄位")
            if not all(isinstance(v, str) for v in updates.values()):
                raise ValueError("CSV 值須為文字，以保留前導零")
            if not isinstance(detail_updates, dict):
                raise ValueError("details_updates 須為物件")
            row = dict(original["csv"], **updates)
            if row[SOURCE] != original["csv"][SOURCE]:
                raise ValueError("不可更換來源頁面")
            detail = dict(original["details"], **detail_updates)
            # Null removes an unresolved candidate rather than keeping an old value.
            detail = {k: v for k, v in detail.items() if v is not None}
            if detail.get("tax_status") == "reviewed" and not str(detail.get("evidence", "")).strip():
                raise ValueError("已複核稅額須附原單證據")
            build_payload([row], plan["user_code"], plan["submission_month"], {row[NUMBER]: detail})
            merged.append({"csv": row, "details": detail})
        normalized.append({"index": result["index"], "rows": merged, "evidence": evidence, "reason": reason})
    return {"plan_id": plan["plan_id"], "batch": batch, "records": normalized,
            "candidate": candidate}


def checkpoint_path(work, batch):
    return work / "review_batches" / f"batch_{batch:03d}.done.json"


def read_checkpoint(work, plan, batch):
    saved = read_json(checkpoint_path(work, batch))
    normalized = validate_candidate(plan, batch, saved["candidate"])
    if saved != normalized:
        raise ValueError("複核存檔內容已變更")
    return saved


def commit(work, batch, candidate_path):
    plan = load_plan(work)
    value = validate_candidate(plan, batch, read_json(candidate_path))
    path = checkpoint_path(work, batch)
    if path.exists():
        if read_checkpoint(work, plan, batch) != value:
            raise ValueError("已完成批次不可覆寫；保留既有核對結果")
    else:
        atomic_json(path, value)
    return {"batch": batch, "saved": True, "rows_reviewed": len(value["records"])}


def status(work):
    plan = load_plan(work)
    total = (len(plan["records"]) + plan["batch_size"] - 1) // plan["batch_size"]
    done = []
    for batch in range(1, total + 1):
        if checkpoint_path(work, batch).exists():
            read_checkpoint(work, plan, batch)
            done.append(batch)
    return {"total_batches": total, "completed_batches": len(done),
            "next_batch": next((b for b in range(1, total + 1) if b not in done), None),
            "total_rows": len(plan["records"])}


def prepare_input(work, batch):
    plan = load_plan(work)
    value = {"plan_id": plan["plan_id"], "batch": batch, "user_code": plan["user_code"],
             "submission_month": plan["submission_month"], "records": selected(plan, batch)}
    path = work / "review_batches" / f"batch_{batch:03d}.input.json"
    atomic_json(path, value)
    return {"input": str(path), "batch": batch}


def materialize(work, csv_path, details_path, recover=False):
    ready_path = work / "export_ready.json"
    ready = read_json(ready_path) if ready_path.exists() else None
    if ready is not None and not recover:
        raise ValueError("完成標記已建立，不可再修改匯出資料")
    progress = status(work)
    if progress["next_batch"] is not None:
        raise ValueError("尚有未完成複核批次，不能匯出")
    plan = load_plan(work)
    manifest = read_json(work / "manifest.json")
    rows, details, reasons, evidence = [], {}, {}, []
    for batch in range(1, progress["total_batches"] + 1):
        for item in read_checkpoint(work, plan, batch)["records"]:
            source = plan["records"][item["index"] - 1]["csv"][SOURCE]
            if len(item["rows"]) != 1:
                reasons.setdefault(source, []).append(item["reason"])
            evidence.append({k: item[k] for k in ("index", "evidence", "reason")})
            for output in item["rows"]:
                row = output["csv"]
                if row[NUMBER] in details:
                    raise ValueError("同號碼仍有多筆帳單，保留複核成果並停止匯出：" + row[NUMBER])
                rows.append(row)
                details[row[NUMBER]] = output["details"]
    counts = Counter(r[SOURCE] for r in rows)
    reviewed_manifest = copy.deepcopy(manifest)
    for source, why in reasons.items():
        reviewed_manifest["resolutions"][source] = {"row_count": counts[source], "reason": "；".join(why)}
    manifest_path = work / "reviewed_manifest.json"
    if ready is None:
        atomic_json(manifest_path, reviewed_manifest)
    validate_manifest(manifest_path, rows, plan["user_code"], plan["submission_month"])
    payload = build_payload(rows, plan["user_code"], plan["submission_month"], details)
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(rows)
    outputs = [(Path(csv_path), buffer.getvalue().encode("utf-8-sig")),
               (Path(details_path), json.dumps(details, ensure_ascii=False, indent=2).encode("utf-8"))]
    if ready is not None:
        if ready.get("schema_version") != 2:
            raise ValueError("完成標記版本不支援安全重建")
        expected_paths = {"csv_path": Path(csv_path), "details_path": Path(details_path),
                          "manifest_path": manifest_path}
        for key, path in expected_paths.items():
            if os.path.normcase(os.path.abspath(str(path))) != os.path.normcase(os.path.abspath(str(ready.get(key, "")))):
                raise ValueError("重建路徑與完成標記不一致：" + key)
        expected_hashes = {"csv_sha256": hashlib.sha256(outputs[0][1]).hexdigest(),
                           "details_sha256": hashlib.sha256(outputs[1][1]).hexdigest()}
        for key, value in expected_hashes.items():
            if value.lower() != str(ready.get(key, "")).lower():
                raise ValueError("重建內容與完成標記不一致：" + key)
        if not manifest_path.is_file() or sha256(manifest_path).lower() != str(ready.get("manifest_sha256", "")).lower():
            raise ValueError("複核清單與完成標記不一致，不能安全重建")
        if read_json(manifest_path) != reviewed_manifest:
            raise ValueError("複核清單內容與批次資料不一致，不能安全重建")
    # Check both before writing either; a partial previous write can be recovered idempotently.
    for path, content in outputs:
        if path.exists() and path.read_bytes() != content:
            raise ValueError("輸出已有不同內容，不覆寫：" + str(path))
    for path, content in outputs:
        if not path.exists():
            # Publish a complete file without replacing a file created by another process.
            fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    if path.read_bytes() != content:
                        raise ValueError("輸出已有不同內容，不覆寫：" + str(path))
            finally:
                Path(temporary).unlink(missing_ok=True)
    if ready is not None:
        return {"rows": len(rows), "manifest": str(manifest_path), "csv": str(csv_path), "recovered": True}
    atomic_json(work / "review_evidence.json", evidence)
    summary = [f"已完成 {len(rows)} 筆帳單複核。", f"客戶：{plan['user_code']}；送件年月：{plan['submission_month']}",
               "帳單類型：" + json.dumps(dict(Counter(r['帳單類型'] for r in rows)), ensure_ascii=False)]
    summary += [f"第{r['列號']}列 [{r['號碼']}]：{'；'.join(r['待確認'])}" for r in payload["reviews"]]
    summary += [f"{i + 1}. {r[NOTES]}" for i, r in enumerate(rows) if "⚠" in r[NOTES]]
    atomic_text(work / "review_summary.txt", "\n".join(summary) + "\n")
    return {"rows": len(rows), "manifest": str(manifest_path), "csv": str(csv_path)}


def retryable(log_paths):
    # Inspect errors only. Tool output or a prompt quoting an error is not a retry signal.
    messages = []
    for path in log_paths:
        if not Path(path).exists():
            continue
        for line in Path(path).read_text(encoding="utf-8-sig", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                if re.search(r"error|failed|disconnect|timeout|timed out|reconnect", line, re.I):
                    messages.append(line)
                continue
            if isinstance(event, dict) and (event.get("type") in ("error", "turn.failed") or event.get("error")):
                messages.append(json.dumps(event))
    message = "\n".join(messages)
    fatal = r"\b401\b|\b403\b|unauthori[sz]ed|forbidden|quota|usage.limit|billing|insufficient|policy.violation|context.length|invalid.api.key|cancelled|canceled"
    temporary = r"stream disconnected|reconnecting|timed out|timeout|temporarily|server_is_overloaded|internal.server.error|error occurred while processing your request|connection.reset|connection.closed|HTTP 50[0234]"
    return {"retryable": bool(re.search(temporary, message, re.I)) and not bool(re.search(fatal, message, re.I)),
            "error": message[-4000:]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init", "status", "input", "commit", "materialize", "retryable"])
    parser.add_argument("--workdir", type=Path)
    parser.add_argument("--batch", type=int)
    parser.add_argument("--size", type=int, default=5)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--details", type=Path)
    parser.add_argument("--recover", action="store_true")
    parser.add_argument("--logs", nargs="*")
    args = parser.parse_args()
    if args.command == "init":
        initialize(args.workdir, args.size)
        result = status(args.workdir)
    elif args.command == "status": result = status(args.workdir)
    elif args.command == "input": result = prepare_input(args.workdir, args.batch)
    elif args.command == "commit": result = commit(args.workdir, args.batch, args.candidate)
    elif args.command == "materialize": result = materialize(args.workdir, args.csv, args.details, args.recover)
    else: result = retryable(args.logs)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
