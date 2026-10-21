"""Assemble versioned Module 1 pilot handoffs from existing Drive 3 artifacts.

No OCR or normalization is rerun. Source Drive folders are never written.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
from typing import Any

from .drive_colab_runner import (
    DriveStore, PILOT_TICKERS, RESULT_FOLDER_ID, _revision, _sha256, _write_json,
)

HANDOFF_VERSION = "1"
SCOPE = "pilot-drive-scope"
_ACTIVE = {"processed", "processed_with_gaps", "skipped_completed"}


def _status_entries(store: DriveStore, root: str) -> list[dict]:
    names = sorted(item["name"] for item in store.children(root)
                   if item["name"] == "batch_status.json" or
                   item["name"].startswith("batch_status_selection_") and
                   item["name"].endswith(".json"))
    entries = []
    for name in names:
        status = store.read_result_json(root, name)
        if status:
            entries.extend(item for item in status.get("reports", [])
                           if item.get("status") in _ACTIVE and item.get("drive_folder_id"))
    return entries


def _marker(store: DriveStore, entry: dict) -> dict | None:
    marker = store.read_result_json(entry["drive_folder_id"], "completion.json")
    return marker if marker and marker.get("status") == "completed" else None


def _b02_count(marker: dict) -> int:
    return sum(table.get("normalization", {}).get("fact_count", 0)
               for table in marker["summary"].get("income_statement_tables", []))


def choose_b02(markers: list[tuple[str, dict]], source_fingerprint: str) -> tuple[str, dict] | None:
    matching = [(folder, marker) for folder, marker in markers
                if marker["source_fingerprint"] == source_fingerprint]
    if not matching:
        return None
    return max(matching, key=lambda pair: (_b02_count(pair[1]),
                                            pair[1].get("code_revision", ""), pair[0]))


def choose_statements(markers: list[tuple[str, dict]], source_fingerprint: str,
                      manifest_md5: str, elements_md5: str) -> tuple[str, dict] | None:
    expected = _sha256({"source_fingerprint": source_fingerprint,
                        "manifest_md5": manifest_md5, "elements_md5": elements_md5})
    matching = [(folder, marker) for folder, marker in markers
                if marker["source_fingerprint"] == expected]
    if not matching:
        return None
    return max(matching, key=lambda pair: (int(pair[1]["summary"].get("batch_version", 0)),
                                            pair[1]["summary"].get("fact_count", 0), pair[0]))


def _selected_artifacts(marker: dict, group: str) -> dict[str, dict]:
    artifacts = marker["artifacts"]
    if group == "adapter":
        return {key: artifacts[key] for key in ("manifest.json", "elements.jsonl",
                                               "drive_source_map.json")}
    if group == "quality":
        return {key: value for key, value in artifacts.items()
                if key.startswith("quality/") and
                key.endswith(("/quality_report.json", "/quality_issues.jsonl"))}
    if group == "b02":
        return {key: value for key, value in artifacts.items()
                if (key.startswith("normalization/") and
                    key.endswith(("/financial_facts.jsonl", "/normalization_issues.jsonl"))) or
                (key.startswith("c1/") and
                 key.endswith(("/c1_report.json", "/validation_results.jsonl")))}
    if group == "statements":
        return {key: value for key, value in artifacts.items()
                if key in ("financial_facts.jsonl", "normalization_issues.jsonl",
                           "statement_candidate_summary.json") or
                key.startswith("normalization/") and
                key.endswith(("/financial_facts.jsonl", "/normalization_issues.jsonl"))}
    raise ValueError(f"Unknown artifact group: {group}")


def _verified_refs(store: DriveStore, artifacts: dict[str, dict], *, required: bool,
                   warnings: list[str]) -> dict[str, dict]:
    verified = {}
    for name, value in artifacts.items():
        file_id = value["id"]
        try:
            meta = store.service.files().get(
                fileId=file_id, fields="id,md5Checksum,trashed", supportsAllDrives=True,
            ).execute(num_retries=3)
        except Exception as exc:
            from googleapiclient.errors import HttpError

            if not isinstance(exc, HttpError) or exc.resp.status != 404:
                raise
            meta = {"trashed": True}
        if meta.get("trashed") or meta.get("md5Checksum") != value["md5"]:
            if required:
                raise ValueError(f"Required source artifact changed: {name}")
            warnings.append(f"OUTPUT_ARTIFACT_UNAVAILABLE:{name}")
            continue
        verified[name] = {"drive_file_id": file_id, "md5": value["md5"],
                          "size": value["size"]}
    return verified


def build_handoff(store: DriveStore, *, ticker: str, year: str,
                  adapter_folder: str, adapter_marker: dict,
                  b02: tuple[str, dict] | None, statements: tuple[str, dict] | None,
                  crop_audit: dict | None) -> dict:
    summary = adapter_marker["summary"]
    if (summary["ticker"], summary["year"], summary["access_scope"]) != (ticker, year, SCOPE):
        raise ValueError("Adapter summary is outside requested report/scope")
    warnings: list[str] = []
    adapter = _verified_refs(store, _selected_artifacts(adapter_marker, "adapter"),
                             required=True, warnings=warnings)
    quality = _verified_refs(store, _selected_artifacts(adapter_marker, "quality"),
                             required=False, warnings=warnings)
    if not quality:
        warnings.append("OCR_QUALITY_REPORT_UNAVAILABLE")
    if summary.get("ocr_review_issue_count", 0):
        warnings.append("OCR_REVIEW_ISSUES_PRESENT")
    if crop_audit is None:
        warnings.append("CROP_AUDIT_UNAVAILABLE")

    b02_component = None
    if b02 is None:
        warnings.append("B02_CANDIDATES_UNAVAILABLE")
    else:
        folder, marker = b02
        refs = _verified_refs(store, _selected_artifacts(marker, "b02"),
                              required=False, warnings=warnings)
        b02_component = {
            "drive_run_folder_id": folder, "code_revision": marker["code_revision"],
            "fact_count": _b02_count(marker),
            "tables": marker["summary"].get("income_statement_tables", []),
            "artifacts": refs,
            "legal_status": "unknown",
        }
        if _b02_count(marker) == 0 or not any(k.endswith("/financial_facts.jsonl") for k in refs):
            warnings.append("B02_CANDIDATES_UNAVAILABLE")
    statement_component = None
    if statements is None:
        warnings.append("B01_B03_CANDIDATES_UNAVAILABLE")
    else:
        folder, marker = statements
        statement_summary = marker["summary"]
        refs = _verified_refs(store, _selected_artifacts(marker, "statements"),
                              required=False, warnings=warnings)
        statement_component = {
            "drive_run_folder_id": folder, "code_revision": marker["code_revision"],
            "batch_version": statement_summary["batch_version"],
            "fact_count": statement_summary["fact_count"],
            "tables": statement_summary["tables"], "artifacts": refs,
        }
        if statement_summary["fact_count"] == 0 or not any(
                k.endswith("financial_facts.jsonl") for k in refs):
            warnings.append("B01_B03_CANDIDATES_UNAVAILABLE")
        if any(table.get("fact_count", 0) == 0 for table in statement_summary["tables"]):
            warnings.append("B01_B03_TABLES_NEED_REVIEW")
    return {
        "schema_version": "1", "handoff_version": HANDOFF_VERSION,
        "ticker": ticker, "year": year, "document_id": summary["document_id"],
        "document_version": summary["document_version"], "access_scope": SCOPE,
        "source_fingerprint": adapter_marker["source_fingerprint"],
        "adapter_run_folder_id": adapter_folder, "page_count": summary["page_count"],
        "adapter_artifacts": adapter, "quality_artifacts": quality,
        "ocr_quality_status": summary["ocr_quality_status"],
        "ocr_review_issue_count": summary["ocr_review_issue_count"],
        "b02": b02_component, "b01_b03": statement_component,
        "crop_audit": ({"drive_file_id": crop_audit["id"], "md5": crop_audit["md5Checksum"],
                        "checked_by": "drive_metadata_only"} if crop_audit else None),
        "warnings": sorted(set(warnings)), "ingestion_status": "needs_review",
        "handoff_status": "completed_with_warnings",
        "legal_compliance_status": "unknown",
        "original_pdf_ref": None, "knowledge_storage_published": False,
        "interpretation_note": (
            "OCR blocks and tables in elements.jsonl are available for later evidence-based "
            "interpretation. Candidate numeric facts are not verified; missing values must not be invented."
        ),
    }


def run_pilot_handoff(service: Any) -> list[dict]:
    store = DriveStore(service)
    pilot = store.require_folder(RESULT_FOLDER_ID, "module_01_pilot")
    adapter_status = store.read_result_json(pilot, "batch_status.json")
    if adapter_status is None or len(adapter_status.get("reports", [])) != 15:
        raise ValueError("Expected the completed 15-report adapter pilot")
    adapter_entries = adapter_status["reports"]
    if len({(e["ticker"], e["year"]) for e in adapter_entries}) != 15:
        raise ValueError("Duplicate pilot ticker/year")
    b02_entries = _status_entries(store, pilot)
    statement_root = store.require_folder(pilot, "statement_candidates")
    statement_entries = _status_entries(store, statement_root)
    b02_markers: dict[tuple[str, str], list[tuple[str, dict]]] = {}
    statement_markers: dict[tuple[str, str], list[tuple[str, dict]]] = {}
    for entries, destination in ((b02_entries, b02_markers),
                                 (statement_entries, statement_markers)):
        seen = set()
        for entry in entries:
            folder = entry["drive_folder_id"]
            if folder in seen:
                continue
            seen.add(folder)
            marker = _marker(store, entry)
            if marker:
                destination.setdefault((entry["ticker"], entry["year"]), []).append((folder, marker))
    crop_root = store.child(pilot, "crop_audits")
    crop_by_report = {}
    if crop_root:
        for ticker in PILOT_TICKERS:
            ticker_folder = store.child(crop_root["id"], ticker)
            if ticker_folder:
                for item in store.children(ticker_folder["id"]):
                    if item["name"].endswith(".json"):
                        crop_by_report[ticker, item["name"][:-5]] = item

    handoff_root = store.ensure_result_folder(pilot, "handoff")
    revision = _revision()
    config = _sha256({"stage": "module1_pilot_handoff", "version": HANDOFF_VERSION,
                      "access_scope": SCOPE})
    output = []
    for entry in adapter_entries:
        ticker, year = entry["ticker"], entry["year"]
        try:
            if ticker not in PILOT_TICKERS or entry["status"] not in _ACTIVE:
                raise ValueError("Invalid pilot adapter entry")
            adapter_marker = _marker(store, entry)
            if adapter_marker is None:
                raise ValueError("Adapter completion marker missing")
            source = adapter_marker["source_fingerprint"]
            artifacts = adapter_marker["artifacts"]
            b02 = choose_b02(b02_markers.get((ticker, year), []), source)
            statements = choose_statements(
                statement_markers.get((ticker, year), []), source,
                artifacts["manifest.json"]["md5"], artifacts["elements.jsonl"]["md5"])
            crop = crop_by_report.get((ticker, year))
            fingerprint = _sha256({
                "adapter": source,
                "b02": [b02[1]["code_revision"], b02[1]["artifacts"]] if b02 else None,
                "b01_b03": [statements[1]["code_revision"], statements[1]["artifacts"]]
                if statements else None,
                "crop_audit_md5": crop.get("md5Checksum") if crop else None,
            })
            ticker_folder = store.ensure_result_folder(handoff_root, ticker)
            year_folder = store.ensure_result_folder(ticker_folder, year)
            run_folder = store.ensure_result_folder(
                year_folder, f"run-{fingerprint[:16]}-{revision[:12]}-{config[:8]}")
            if store.completion_valid(run_folder, source=fingerprint,
                                      revision=revision, config=config):
                result = {"ticker": ticker, "year": year, "status": "skipped_completed",
                          "drive_folder_id": run_folder}
            else:
                handoff = build_handoff(
                    store, ticker=ticker, year=year,
                    adapter_folder=entry["drive_folder_id"], adapter_marker=adapter_marker,
                    b02=b02, statements=statements, crop_audit=crop)
                with tempfile.TemporaryDirectory(prefix="pilot-handoff-") as temporary:
                    base = Path(temporary)
                    path = base / "handoff.json"
                    _write_json(path, handoff)
                    uploaded = store.upload_result(run_folder, path)
                    completion_path = base / "completion.json"
                    _write_json(completion_path, {
                        "status": "completed", "source_fingerprint": fingerprint,
                        "code_revision": revision, "config_fingerprint": config,
                        "artifacts": {"handoff.json": uploaded},
                        "summary": {"ticker": ticker, "year": year,
                                    "handoff_status": handoff["handoff_status"],
                                    "warning_count": len(handoff["warnings"]),
                                    "b02_fact_count": handoff["b02"]["fact_count"] if handoff["b02"] else 0,
                                    "b01_b03_fact_count": handoff["b01_b03"]["fact_count"]
                                    if handoff["b01_b03"] else 0},
                    })
                    store.upload_result(run_folder, completion_path)
                result = {"ticker": ticker, "year": year, "status": "processed",
                          "drive_folder_id": run_folder,
                          "warning_count": len(handoff["warnings"])}
            output.append(result)
            print(json.dumps(result), flush=True)
        except Exception as exc:
            result = {"ticker": ticker, "year": year, "status": "failed",
                      "error_type": type(exc).__name__, "error": str(exc)}
            output.append(result)
            print(json.dumps(result), flush=True)
    with tempfile.TemporaryDirectory(prefix="pilot-handoff-batch-") as temporary:
        path = Path(temporary) / "batch_status.json"
        _write_json(path, {"handoff_version": HANDOFF_VERSION, "code_revision": revision,
                           "config_fingerprint": config, "reports": output})
        store.upload_result(handoff_root, path)
    return output
