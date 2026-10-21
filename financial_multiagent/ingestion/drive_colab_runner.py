"""Run the precomputed-OCR branch of Module 1 inside a Colab runtime.

Source folders are accessed only through Drive list/get operations. All Drive
mutations are confined to RESULT_FOLDER_ID and folders created below it.
Downloaded PNGs live in a temporary directory in the Colab VM, one report at
a time. The source map in each result preserves durable Drive file IDs.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from financial_multiagent.ingestion.financial_normalization import (
    NormalizationContext,
    NormalizationError,
    normalize_financial_table,
)
from financial_multiagent.ingestion.ocr_quality import assess_ocr_bundle
from financial_multiagent.ingestion.precomputed_ocr import import_ocr_bundle
from financial_multiagent.validation.c1.runner import C1Input, run_c1


OCR_FOLDER_ID = "1BH5yCw32xQuCfLnPPigEPAf5R6mYBSID"
PAGE_FOLDER_ID = "1bNLJJwk3IhsPa7g2-2m0Zhq6HTGV7qvQ"
RESULT_FOLDER_ID = "1WSI4FwmAWtK_Dan1TGjglUxhxo-WPKc_"
PILOT_TICKERS = ("BTS", "BTW", "BPC")
FOLDER_MIME = "application/vnd.google-apps.folder"
PAGE_RE = re.compile(r"page_([1-9][0-9]*)\.(json|png)\Z", re.IGNORECASE)
ID_RE = re.compile(r"[A-Za-z0-9_-]+\Z")
RUNNER_VERSION = "1"


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _md5(path: Path) -> str:
    digest = hashlib.md5()  # Drive exposes MD5 for ordinary files.
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFD", value.lower())
    return "".join(c for c in normalized if unicodedata.category(c) != "Mn").replace("đ", "d")


def _page_files(files: list[dict], suffix: str) -> dict[int, dict]:
    pages: dict[int, dict] = {}
    for item in files:
        match = PAGE_RE.fullmatch(item["name"])
        if not match or match[2].lower() != suffix:
            continue
        number = int(match[1])
        if number in pages:
            raise ValueError(f"Duplicate {suffix} page {number}")
        pages[number] = item
    return pages


def _source_identity(ocr_pages: dict[int, dict], png_pages: dict[int, dict]) -> str:
    if not ocr_pages or ocr_pages.keys() != png_pages.keys():
        raise ValueError("OCR JSON and page PNG sets are missing or do not match")
    numbers = sorted(ocr_pages)
    if numbers != list(range(1, numbers[-1] + 1)):
        raise ValueError("Page numbers must be contiguous from 1")
    entries = []
    for number in numbers:
        for kind, item in (("json", ocr_pages[number]), ("png", png_pages[number])):
            entries.append({k: item.get(k) for k in ("id", "name", "size", "md5Checksum", "modifiedTime")}
                           | {"kind": kind, "page": number})
    return _sha256(entries)


class DriveStore:
    """Small Google Drive v3 wrapper; source methods never mutate files."""

    def __init__(self, service: Any):
        self.service = service

    def children(self, parent_id: str) -> list[dict]:
        if not ID_RE.fullmatch(parent_id):
            raise ValueError("Invalid Drive folder ID")
        result: list[dict] = []
        token = None
        while True:
            response = self.service.files().list(
                q=f"'{parent_id}' in parents and trashed = false",
                fields="nextPageToken,files(id,name,mimeType,size,md5Checksum,modifiedTime)",
                pageSize=1000, pageToken=token, supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute(num_retries=3)
            result.extend(response.get("files", []))
            token = response.get("nextPageToken")
            if not token:
                return result

    def child(self, parent_id: str, name: str, *, folder: bool = True) -> dict | None:
        matches = [item for item in self.children(parent_id)
                   if item["name"] == name and (item["mimeType"] == FOLDER_MIME) == folder]
        if len(matches) > 1:
            raise ValueError(f"Ambiguous Drive item under {parent_id}: {name}")
        return matches[0] if matches else None

    def require_folder(self, parent_id: str, name: str) -> str:
        item = self.child(parent_id, name)
        if item is None:
            raise FileNotFoundError(f"Missing source folder {name} under {parent_id}")
        return item["id"]

    def ensure_result_folder(self, parent_id: str, name: str) -> str:
        item = self.child(parent_id, name)
        if item:
            return item["id"]
        response = self.service.files().create(
            body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
            fields="id", supportsAllDrives=True,
        ).execute(num_retries=3)
        return response["id"]

    def download(self, item: dict, path: Path) -> None:
        from googleapiclient.http import MediaIoBaseDownload

        path.parent.mkdir(parents=True, exist_ok=True)
        request = self.service.files().get_media(fileId=item["id"], supportsAllDrives=True)
        with path.open("wb") as stream:
            downloader = MediaIoBaseDownload(stream, request, chunksize=4 * 1024 * 1024)
            done = False
            while not done:
                _, done = downloader.next_chunk(num_retries=3)
        if item.get("size") is not None and path.stat().st_size != int(item["size"]):
            raise IOError(f"Drive size mismatch for {item['name']}")
        if item.get("md5Checksum") and _md5(path) != item["md5Checksum"]:
            raise IOError(f"Drive checksum mismatch for {item['name']}")

    def read_result_json(self, parent_id: str, name: str) -> dict | None:
        item = self.child(parent_id, name, folder=False)
        if item is None:
            return None
        if int(item.get("size") or 0) > 2_000_000:
            raise ValueError(f"Unexpectedly large checkpoint: {name}")
        content = self.service.files().get_media(fileId=item["id"], supportsAllDrives=True).execute(num_retries=3)
        return json.loads(content)

    def upload_result(self, parent_id: str, path: Path) -> dict:
        from googleapiclient.http import MediaFileUpload

        existing = self.child(parent_id, path.name, folder=False)
        checksum = _md5(path)
        if existing and existing.get("md5Checksum") == checksum:
            return {"id": existing["id"], "md5": checksum, "size": path.stat().st_size}
        media = MediaFileUpload(str(path), mimetype="application/octet-stream", resumable=True)
        if existing:
            response = self.service.files().update(
                fileId=existing["id"], media_body=media, fields="id,md5Checksum,size",
                supportsAllDrives=True,
            ).execute(num_retries=3)
        else:
            response = self.service.files().create(
                body={"name": path.name, "parents": [parent_id]}, media_body=media,
                fields="id,md5Checksum,size", supportsAllDrives=True,
            ).execute(num_retries=3)
        if response.get("md5Checksum") != checksum:
            raise IOError(f"Uploaded Drive checksum mismatch: {path.name}")
        return {"id": response["id"], "md5": checksum, "size": path.stat().st_size}

    def upload_tree(self, parent_id: str, root: Path) -> dict[str, dict]:
        uploaded: dict[str, dict] = {}
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(root)
            folder_id = parent_id
            for part in relative.parts[:-1]:
                folder_id = self.ensure_result_folder(folder_id, part)
            uploaded[relative.as_posix()] = self.upload_result(folder_id, path)
        return uploaded

    def completion_valid(self, folder_id: str, *, source: str, revision: str, config: str) -> bool:
        try:
            marker = self.read_result_json(folder_id, "completion.json")
        except (ValueError, json.JSONDecodeError):
            return False
        if not marker or any((marker.get("source_fingerprint") != source,
                              marker.get("code_revision") != revision,
                              marker.get("config_fingerprint") != config,
                              marker.get("status") != "completed")):
            return False
        for item in marker.get("artifacts", {}).values():
            try:
                metadata = self.service.files().get(
                    fileId=item["id"], fields="id,md5Checksum,trashed", supportsAllDrives=True,
                ).execute(num_retries=3)
            except Exception as exc:
                from googleapiclient.errors import HttpError

                if isinstance(exc, HttpError) and exc.resp.status == 404:
                    return False
                raise
            if metadata.get("trashed") or metadata.get("md5Checksum") != item["md5"]:
                return False
        return bool(marker.get("artifacts"))


@dataclass(frozen=True)
class PilotConfig:
    tickers: tuple[str, ...] = PILOT_TICKERS
    years: tuple[str, ...] | None = None  # None means all available years for these tickers.
    access_scope: str = "pilot-drive-scope"
    output_name: str = "module_01_pilot"

    def __post_init__(self) -> None:
        if not self.tickers or any(t not in PILOT_TICKERS for t in self.tickers):
            raise ValueError("Pilot is restricted to BTS, BTW and BPC")
        if self.years is not None and any(not re.fullmatch(r"20[0-9]{2}", y) for y in self.years):
            raise ValueError("Invalid selected year")
        if not self.access_scope.strip():
            raise ValueError("access_scope is required")


def _revision() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()


def _tables(bundle_dir: Path) -> list[tuple[int, str]]:
    candidates = []
    with (bundle_dir / "elements.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            record = json.loads(line)
            if (record["kind"] == "table" and isinstance(record.get("name"), str)
                    and _fold(record["name"]) == "bao cao ket qua hoat dong kinh doanh"):
                candidates.append((record["page"], record["element_id"]))
    return candidates


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _process_one(store: DriveStore, ticker: str, year: str, *, ocr_year: str,
                 image_year: str, run_folder: str, ocr_pages: dict[int, dict],
                 png_pages: dict[int, dict], crop_folder: str | None,
                 source_fingerprint: str, revision: str, config_fingerprint: str,
                 access_scope: str) -> dict:
    with tempfile.TemporaryDirectory(prefix=f"module1-{ticker}-{year}-") as temporary:
        base = Path(temporary)
        source_root = base / "source"
        json_dir = source_root / "Financial_Report_OCR" / ticker / year / "ocr_json"
        image_dir = source_root / "Financial_Report" / ticker / year
        bundle = base / "result"
        source_map: dict[str, Any] = {
            "source_fingerprint": source_fingerprint, "ocr_year_folder_id": ocr_year,
            "page_year_folder_id": image_year, "crop_folder_id": crop_folder,
            "crop_validation": "deferred", "files": {},
        }
        for number in sorted(ocr_pages):
            for item, path in ((ocr_pages[number], json_dir / f"page_{number}.json"),
                               (png_pages[number], image_dir / f"page_{number}.png")):
                store.download(item, path)
                source_map["files"][path.relative_to(source_root).as_posix()] = {
                    "drive_file_id": item["id"], "md5": item.get("md5Checksum"),
                    "size": path.stat().st_size,
                }
        manifest = import_ocr_bundle(
            json_dir, image_dir, bundle, document_id=f"{ticker}-{year}-report",
            document_version=f"drive-{source_fingerprint[:16]}", access_scope=access_scope,
            source_root=source_root, validate_crops=False, tolerate_out_of_page=True,
        )
        _write_json(bundle / "drive_source_map.json", source_map)
        quality = assess_ocr_bundle(bundle, access_scope=access_scope)
        table_results = []
        for page, table_id in _tables(bundle):
            entry: dict[str, Any] = {"page": page, "table_id": table_id}
            try:
                context = NormalizationContext(
                    company_id=ticker,
                    company_evidence="Drive folder ticker matches OCR page symbol; legal entity not verified",
                )
                normalized = normalize_financial_table(
                    bundle, page=page, table_id=table_id, access_scope=access_scope,
                    context=context,
                )
                entry["normalization"] = {
                    "run_id": normalized["run_id"], "fact_count": normalized["fact_count"],
                    "quality_status": normalized["quality_status"],
                }
                c1 = run_c1(
                    [C1Input(bundle, normalized["output_dir"])], access_scope=access_scope,
                )
                entry["c1"] = {
                    "run_id": c1["run_id"], "outcomes": c1["outcome_counts"],
                    "legal_status": c1["legal_status"],
                }
            except NormalizationError as exc:
                entry["status"] = "needs_review"
                entry["reason"] = str(exc)
            table_results.append(entry)
        summary = {
            "schema_version": "1", "runner_version": RUNNER_VERSION,
            "ticker": ticker, "year": year, "document_id": manifest["document_id"],
            "document_version": manifest["document_version"], "access_scope": access_scope,
            "source_fingerprint": source_fingerprint, "code_revision": revision,
            "config_fingerprint": config_fingerprint, "page_count": len(ocr_pages),
            "ocr_quality_status": quality["quality_status"],
            "ocr_review_issue_count": quality["review_issue_count"],
            "income_statement_tables": table_results,
            "ingestion_status": "processing",
            "coverage": "Precomputed OCR, quality and supported income-statement tables only; legal rules unavailable",
            "limitations": [
                "Source crops were not downloaded or validated; crop refs are deferred.",
                "Original PDFs are not present in these inputs.",
                "Financial statements beyond supported income-statement layouts require more normalization work.",
                "No reviewed legal rule registry was supplied; legal compliance remains unknown.",
                "No Knowledge Storage/C2 publish or document ready transition occurred.",
            ],
        }
        _write_json(bundle / "pilot_summary.json", summary)
        artifacts = store.upload_tree(run_folder, bundle)
        completion = {
            "status": "completed", "source_fingerprint": source_fingerprint,
            "code_revision": revision, "config_fingerprint": config_fingerprint,
            "artifacts": artifacts, "summary": summary,
        }
        completion_path = base / "completion.json"
        _write_json(completion_path, completion)
        store.upload_result(run_folder, completion_path)  # Commit marker is uploaded last.
        return summary


def run_pilot(service: Any, config: PilotConfig = PilotConfig()) -> list[dict]:
    """Process selected tickers/years; skip verified completed runs on resume."""
    store = DriveStore(service)
    revision = _revision()
    config_fingerprint = _sha256({"runner": RUNNER_VERSION, "scope": config.access_scope})
    result_root = store.ensure_result_folder(RESULT_FOLDER_ID, config.output_name)
    output: list[dict] = []
    for ticker in config.tickers:
        ocr_ticker = store.require_folder(OCR_FOLDER_ID, ticker)
        image_ticker = store.require_folder(PAGE_FOLDER_ID, ticker)
        ocr_years = {x["name"]: x["id"] for x in store.children(ocr_ticker)
                     if x["mimeType"] == FOLDER_MIME and re.fullmatch(r"20[0-9]{2}", x["name"])}
        image_years = {x["name"]: x["id"] for x in store.children(image_ticker)
                       if x["mimeType"] == FOLDER_MIME and re.fullmatch(r"20[0-9]{2}", x["name"])}
        years = list(config.years) if config.years is not None else sorted(set(ocr_years) | set(image_years))
        ticker_result = store.ensure_result_folder(result_root, ticker)
        for year in years:
            try:
                if year not in ocr_years or year not in image_years:
                    raise FileNotFoundError("Year folder missing in one source Drive")
                ocr_year, image_year = ocr_years[year], image_years[year]
                json_folder = store.require_folder(ocr_year, "ocr_json")
                crop_item = store.child(ocr_year, "ocr_crops")
                ocr_pages = _page_files(store.children(json_folder), "json")
                png_pages = _page_files(store.children(image_year), "png")
                source = _source_identity(ocr_pages, png_pages)
                year_result = store.ensure_result_folder(ticker_result, year)
                run_name = f"run-{source[:16]}-{revision[:12]}-{config_fingerprint[:8]}"
                run_folder = store.ensure_result_folder(year_result, run_name)
                if store.completion_valid(run_folder, source=source, revision=revision,
                                          config=config_fingerprint):
                    output.append({"ticker": ticker, "year": year, "status": "skipped_completed",
                                   "drive_folder_id": run_folder})
                    continue
                summary = _process_one(
                    store, ticker, year, ocr_year=ocr_year, image_year=image_year,
                    run_folder=run_folder, ocr_pages=ocr_pages, png_pages=png_pages,
                    crop_folder=crop_item["id"] if crop_item else None,
                    source_fingerprint=source, revision=revision,
                    config_fingerprint=config_fingerprint, access_scope=config.access_scope,
                )
                output.append({"ticker": ticker, "year": year, "status": "processed",
                               "drive_folder_id": run_folder, "summary": summary})
            except Exception as exc:
                output.append({"ticker": ticker, "year": year, "status": "failed",
                               "error_type": type(exc).__name__, "error": str(exc)})
    with tempfile.TemporaryDirectory(prefix="module1-batch-") as temporary:
        status_path = Path(temporary) / "batch_status.json"
        _write_json(status_path, {
            "runner_version": RUNNER_VERSION, "code_revision": revision,
            "config_fingerprint": config_fingerprint, "tickers": list(config.tickers),
            "years": list(config.years) if config.years is not None else None,
            "reports": output,
        })
        store.upload_result(result_root, status_path)
    return output
