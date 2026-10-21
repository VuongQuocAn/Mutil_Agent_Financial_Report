"""Colab entry point: PYTHONPATH=. python colab/run_pilot_handoff.py"""

import google.auth
import google_auth_httplib2
import httplib2
from google.colab import auth
from googleapiclient.discovery import build

from financial_multiagent.ingestion.pilot_handoff import run_pilot_handoff


def main() -> None:
    auth.authenticate_user()
    credentials, _ = google.auth.default()
    http = google_auth_httplib2.AuthorizedHttp(credentials, http=httplib2.Http(timeout=180))
    service = build("drive", "v3", http=http, cache_discovery=False)
    results = run_pilot_handoff(service)
    print(f"HANDOFF_DONE completed={sum(r['status'] != 'failed' for r in results)} "
          f"failed={sum(r['status'] == 'failed' for r in results)}", flush=True)


if __name__ == "__main__":
    main()
