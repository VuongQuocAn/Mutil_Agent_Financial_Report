"""Inspect B01/B03 pilot outputs on Drive 3 without downloading source reports."""

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
    candidates_root = store.require_folder(pilot, "statement_candidates")
    status = store.read_result_json(candidates_root, "batch_status.json")
    if status is None:
        raise RuntimeError("Statement candidate batch status is absent")
    for report in status["reports"]:
        output = {key: report.get(key) for key in ("ticker", "year", "status", "error")}
        if report["status"] in ("processed", "skipped_completed"):
            marker = store.read_result_json(report["drive_folder_id"], "completion.json")
            if marker is None:
                output["error"] = "Missing completion marker"
            else:
                summary = marker["summary"]
                output["fact_count"] = summary["fact_count"]
                output["tables"] = [
                    {key: item.get(key) for key in
                     ("page", "statement_kind", "fact_count", "mapped_metric_count", "reason")}
                    for item in summary["tables"]
                ]
        print(json.dumps(output, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
