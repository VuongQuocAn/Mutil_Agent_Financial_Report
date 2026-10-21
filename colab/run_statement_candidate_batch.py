"""Colab entry point: PYTHONPATH=. python colab/run_statement_candidate_batch.py"""

import argparse

import google.auth
import google_auth_httplib2
import httplib2
from google.colab import auth
from googleapiclient.discovery import build

from financial_multiagent.ingestion.statement_candidate_batch import run_statement_candidate_batch


def main() -> None:
    parser = argparse.ArgumentParser(description="Run B01/B03 pilot candidates")
    parser.add_argument("--ticker", choices=("BTS", "BTW", "BPC"))
    parser.add_argument("--year", choices=tuple(str(year) for year in range(2021, 2026)))
    args = parser.parse_args()
    if bool(args.ticker) != bool(args.year):
        parser.error("--ticker and --year must be supplied together")
    auth.authenticate_user()
    credentials, _ = google.auth.default()
    http = google_auth_httplib2.AuthorizedHttp(credentials, http=httplib2.Http(timeout=180))
    service = build("drive", "v3", http=http, cache_discovery=False)
    selection = {(args.ticker, args.year)} if args.ticker else None
    run_statement_candidate_batch(service, selection=selection)


if __name__ == "__main__":
    main()
