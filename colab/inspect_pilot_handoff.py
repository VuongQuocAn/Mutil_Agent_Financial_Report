"""Verify completed pilot handoffs and print a small Drive 3 summary."""

import json

import google.auth
import google_auth_httplib2
import httplib2
from google.colab import auth
from googleapiclient.discovery import build

from financial_multiagent.ingestion.drive_colab_runner import DriveStore, RESULT_FOLDER_ID


def main() -> None:
    auth.authenticate_user()
    credentials, _ = google.auth.default()
    http = google_auth_httplib2.AuthorizedHttp(credentials, http=httplib2.Http(timeout=180))
    store = DriveStore(build("drive", "v3", http=http, cache_discovery=False))
    pilot = store.require_folder(RESULT_FOLDER_ID, "module_01_pilot")
    root = store.require_folder(pilot, "handoff")
    status = store.read_result_json(root, "batch_status.json")
    if status is None or len(status.get("reports", [])) != 15:
        raise RuntimeError("Expected 15 handoff status entries")
    summary = {"handoff_folder_id": root, "reports": 0, "valid_checkpoints": 0,
               "b02_candidate_facts": 0, "b01_b03_candidate_facts": 0,
               "without_b01_b03_candidates": [], "warnings": {}}
    for entry in status["reports"]:
        if entry["status"] == "failed":
            raise RuntimeError(f"Failed handoff: {entry['ticker']}/{entry['year']}")
        folder = entry["drive_folder_id"]
        marker = store.read_result_json(folder, "completion.json")
        handoff = store.read_result_json(folder, "handoff.json")
        if marker is None or handoff is None:
            raise RuntimeError(f"Missing output: {entry['ticker']}/{entry['year']}")
        if (entry["ticker"], entry["year"]) != (handoff["ticker"], handoff["year"]):
            raise RuntimeError("Handoff identity mismatch")
        if store.completion_valid(folder, source=marker["source_fingerprint"],
                                  revision=marker["code_revision"],
                                  config=marker["config_fingerprint"]):
            summary["valid_checkpoints"] += 1
        else:
            raise RuntimeError(f"Invalid checkpoint: {entry['ticker']}/{entry['year']}")
        summary["reports"] += 1
        summary["b02_candidate_facts"] += (handoff["b02"] or {}).get("fact_count", 0)
        statement_count = (handoff["b01_b03"] or {}).get("fact_count", 0)
        summary["b01_b03_candidate_facts"] += statement_count
        if statement_count == 0:
            summary["without_b01_b03_candidates"].append(f"{entry['ticker']}/{entry['year']}")
        for warning in handoff["warnings"]:
            summary["warnings"][warning] = summary["warnings"].get(warning, 0) + 1
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
