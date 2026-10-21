"""Run metadata-only crop audits for the existing BTS/BTW/BPC pilot artifacts.

Execute in Colab after Google authentication. Reads source Drive folders and
completed adapter artifacts; writes audits only under Drive 3.
"""

from __future__ import annotations

import json

import google.auth
import google_auth_httplib2
import httplib2
from google.colab import auth
from googleapiclient.discovery import build

from financial_multiagent.ingestion.drive_colab_runner import DriveStore, PILOT_TICKERS, RESULT_FOLDER_ID
from financial_multiagent.ingestion.drive_crop_audit import run_drive_crop_audit


def main() -> None:
    auth.authenticate_user()
    credentials, _ = google.auth.default()
    http = google_auth_httplib2.AuthorizedHttp(credentials, http=httplib2.Http(timeout=180))
    service = build("drive", "v3", http=http, cache_discovery=False)
    store = DriveStore(service)
    root = store.require_folder(RESULT_FOLDER_ID, "module_01_pilot")
    batch = store.read_result_json(root, "batch_status.json")
    if batch is None:
        raise FileNotFoundError("Missing existing pilot batch_status.json")
    results = []
    for entry in batch["reports"]:
        ticker, year = entry["ticker"], entry["year"]
        if ticker not in PILOT_TICKERS:
            raise ValueError("Batch contains ticker outside the pilot")
        try:
            run_folder = entry["drive_folder_id"]
            marker = store.read_result_json(run_folder, "completion.json")
            source_map = store.read_result_json(run_folder, "drive_source_map.json")
            if not marker or marker.get("status") != "completed" or not source_map:
                raise ValueError("Completed adapter artifacts are required")
            elements_id = marker["artifacts"]["elements.jsonl"]["id"]
            result = run_drive_crop_audit(
                service, ticker=ticker, year=year, elements_file_id=elements_id,
                crop_folder_id=source_map.get("crop_folder_id"),
            )
            print(json.dumps(result, ensure_ascii=False), flush=True)
            results.append(result)
        except Exception as exc:
            failure = {"ticker": ticker, "year": year, "error_type": type(exc).__name__,
                       "error": str(exc)}
            print(json.dumps(failure, ensure_ascii=False), flush=True)
            results.append(failure)
    print(json.dumps({"completed": sum("counts" in r for r in results),
                      "failed": sum("error_type" in r for r in results)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
