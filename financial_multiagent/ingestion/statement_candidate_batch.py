"""Run B01/B03 candidate extraction from completed pilot artifacts on Drive 3."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
from typing import Any

from .drive_colab_runner import (
    DriveStore, PILOT_TICKERS, RESULT_FOLDER_ID, _revision, _sha256, _write_json,
)
from .financial_normalization import NormalizationContext, NormalizationError
from .statement_candidates import normalize_statement_table, statement_kind

BATCH_VERSION = "2"


def summarize_table_coverage(tables: list[dict]) -> dict:
    """Describe extraction coverage without treating OCR gaps as job failures."""
    extracted = sum(item.get("table_status") == "extracted" for item in tables)
    quarantined = sum(item.get("table_status") == "quarantined" for item in tables)
    return {
        "table_count": len(tables),
        "extracted_table_count": extracted,
        "quarantined_table_count": quarantined,
        "candidate_fact_count": sum(item.get("fact_count", 0) for item in tables),
        "coverage_status": ("none" if not tables or extracted == 0 else
                            "partial" if quarantined else "all_detected_tables"),
    }


def run_statement_candidate_batch(service: Any) -> list[dict]:
    store = DriveStore(service)
    revision = _revision()
    config = _sha256({"stage": "B01_B03_candidates", "version": BATCH_VERSION,
                      "access_scope": "pilot-drive-scope"})
    pilot = store.require_folder(RESULT_FOLDER_ID, "module_01_pilot")
    status = store.read_result_json(pilot, "batch_status.json")
    if status is None or len(status.get("reports", [])) != 15:
        raise ValueError("Expected the completed 15-report pilot status")
    candidates_root = store.ensure_result_folder(pilot, "statement_candidates")
    output = []
    for entry in status["reports"]:
        ticker, year = entry["ticker"], entry["year"]
        if ticker not in PILOT_TICKERS:
            raise ValueError("Input contains a ticker outside the pilot")
        try:
            marker = store.read_result_json(entry["drive_folder_id"], "completion.json")
            if not marker or marker.get("status") != "completed":
                raise ValueError("Missing completed source run")
            source_files = marker["artifacts"]
            source = _sha256({"source_fingerprint": marker["source_fingerprint"],
                              "manifest_md5": source_files["manifest.json"]["md5"],
                              "elements_md5": source_files["elements.jsonl"]["md5"]})
            ticker_folder = store.ensure_result_folder(candidates_root, ticker)
            year_folder = store.ensure_result_folder(ticker_folder, year)
            run_folder = store.ensure_result_folder(
                year_folder, f"run-{source[:16]}-{revision[:12]}-{config[:8]}")
            if store.completion_valid(run_folder, source=source, revision=revision, config=config):
                output.append({"ticker": ticker, "year": year, "status": "skipped_completed",
                               "drive_folder_id": run_folder})
                print(json.dumps(output[-1]), flush=True)
                continue
            with tempfile.TemporaryDirectory(prefix=f"statement-{ticker}-{year}-") as temporary:
                base = Path(temporary)
                bundle = base / "bundle"
                bundle.mkdir()
                for name in ("manifest.json", "elements.jsonl"):
                    meta = source_files[name]
                    store.download({"id": meta["id"], "name": name, "size": meta["size"],
                                    "md5Checksum": meta["md5"]}, bundle / name)
                results_dir = base / "result"
                tables = []
                with (bundle / "elements.jsonl").open(encoding="utf-8") as stream:
                    for line in stream:
                        record = json.loads(line)
                        kind = statement_kind(record.get("name")) if record["kind"] == "table" else None
                        if kind:
                            tables.append((record["page"], record["element_id"], kind))
                table_results = []
                for page, table_id, kind in tables:
                    item = {"page": page, "table_id": table_id, "statement_kind": kind}
                    try:
                        normalized = normalize_statement_table(
                            bundle, page=page, table_id=table_id, access_scope="pilot-drive-scope",
                            context=NormalizationContext(company_id=ticker,
                                company_evidence="Drive folder ticker matches OCR symbol; legal entity not verified"),
                            output_dir=results_dir / "normalization",
                        )
                        item.update(table_status="extracted", run_id=normalized["run_id"],
                                    fact_count=normalized["fact_count"],
                                    mapped_metric_count=normalized["mapped_metric_count"],
                                    quality_status=normalized["quality_status"])
                    except NormalizationError as exc:
                        item.update(table_status="quarantined", quality_status="needs_review",
                                    reason=str(exc), fact_count=0, mapped_metric_count=0)
                    table_results.append(item)
                coverage = summarize_table_coverage(table_results)
                summary = {"schema_version": "1", "batch_version": BATCH_VERSION,
                           "ticker": ticker, "year": year, "source_fingerprint": source,
                           "code_revision": revision, "config_fingerprint": config,
                           **coverage, "fact_count": coverage["candidate_fact_count"],
                           "tables": table_results, "quality_status": "needs_review",
                           "limitations": ["Numeric B01/B03 candidates require source and metadata review.",
                                           "Quarantined tables may require a separate OCR/layout review.",
                                           "No legal C1 conclusion or Knowledge Storage publish."]}
                _write_json(results_dir / "statement_candidate_summary.json", summary)
                artifacts = store.upload_tree(run_folder, results_dir)
                completion = {"status": "completed", "source_fingerprint": source,
                              "code_revision": revision, "config_fingerprint": config,
                              "artifacts": artifacts, "summary": summary}
                completion_path = base / "completion.json"
                _write_json(completion_path, completion)
                store.upload_result(run_folder, completion_path)
            result = {"ticker": ticker, "year": year,
                      "status": "processed_with_gaps" if coverage["coverage_status"] != "all_detected_tables"
                                else "processed",
                      "drive_folder_id": run_folder, "table_count": len(tables),
                      "extracted_table_count": coverage["extracted_table_count"],
                      "quarantined_table_count": coverage["quarantined_table_count"],
                      "fact_count": summary["fact_count"]}
            output.append(result)
            print(json.dumps(result), flush=True)
        except Exception as exc:
            result = {"ticker": ticker, "year": year, "status": "failed",
                      "error_type": type(exc).__name__, "error": str(exc)}
            output.append(result)
            print(json.dumps(result), flush=True)
    with tempfile.TemporaryDirectory(prefix="statement-batch-") as temporary:
        path = Path(temporary) / "batch_status.json"
        _write_json(path, {"batch_version": BATCH_VERSION, "code_revision": revision,
                           "config_fingerprint": config, "reports": output})
        store.upload_result(candidates_root, path)
    return output
