"""Colab entry point: PYTHONPATH=. python colab/run_statement_candidate_batch.py"""

import google.auth
import google_auth_httplib2
import httplib2
from google.colab import auth
from googleapiclient.discovery import build

from financial_multiagent.ingestion.statement_candidate_batch import run_statement_candidate_batch


def main() -> None:
    auth.authenticate_user()
    credentials, _ = google.auth.default()
    http = google_auth_httplib2.AuthorizedHttp(credentials, http=httplib2.Http(timeout=180))
    service = build("drive", "v3", http=http, cache_discovery=False)
    run_statement_candidate_batch(service)


if __name__ == "__main__":
    main()
