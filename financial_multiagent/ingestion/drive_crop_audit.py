"""Audit whether referenced OCR crops exist on the read-only source Drive.

This checks Drive metadata only. It does not download crop bytes, judge visual
quality, or promote a report to ready. Results belong in the writable Drive.
"""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Any

from .drive_colab_runner import DriveStore, FOLDER_MIME, PILOT_TICKERS, RESULT_FOLDER_ID

AUDIT_VERSION = "1"


def audit_crop_refs(store: DriveStore, elements_path: Path, *, ticker: str,
                    year: str, crop_folder_id: str | None) -> dict[str, Any]:
    """Resolve adapter asset refs against Drive 1 without changing source files."""
    prefix = f"Financial_Report_OCR/{ticker}/{year}/ocr_crops/"
    references: dict[str, list[str]] = {}
    with elements_path.open(encoding="utf-8") as stream:
        for line in stream:
            record = json.loads(line)
            for ref in (record.get("asset_refs") or {}).values():
                if isinstance(ref, str):
                    references.setdefault(ref, []).append(record["source_ref"])

    listing_cache: dict[str, list[dict]] = {}

    def children(folder_id: str) -> list[dict]:
        if folder_id not in listing_cache:
            listing_cache[folder_id] = store.children(folder_id)
        return listing_cache[folder_id]

    assets = []
    for ref, sources in sorted(references.items()):
        suffix = ref[len(prefix):] if ref.startswith(prefix) else ""
        parts = suffix.split("/") if suffix else []
        if not parts or any(part in {"", ".", ".."} or "\\" in part for part in parts):
            assets.append({"ref": ref, "status": "invalid_ref", "source_refs": sources})
            continue
        if crop_folder_id is None:
            assets.append({"ref": ref, "status": "missing_crop_root", "source_refs": sources})
            continue
        folder_id = crop_folder_id
        status = "found"
        item = None
        for index, part in enumerate(parts):
            want_folder = index < len(parts) - 1
            matches = [child for child in children(folder_id)
                       if child["name"] == part and (child["mimeType"] == FOLDER_MIME) == want_folder]
            if len(matches) != 1:
                status = "ambiguous" if len(matches) > 1 else "missing"
                break
            item = matches[0]
            if want_folder:
                folder_id = item["id"]
        result = {"ref": ref, "status": status, "source_refs": sources}
        if status == "found" and item is not None:
            result["drive_file_id"] = item["id"]
            result["size"] = item.get("size")
            result["md5"] = item.get("md5Checksum")
        assets.append(result)
    counts = {status: sum(asset["status"] == status for asset in assets)
              for status in ("found", "missing", "missing_crop_root", "ambiguous", "invalid_ref")}
    return {"audit_version": AUDIT_VERSION, "ticker": ticker, "year": year,
            "crop_folder_id": crop_folder_id, "checked_by": "drive_metadata_only",
            "reference_count": sum(len(x) for x in references.values()),
            "unique_asset_count": len(references), "counts": counts, "assets": assets,
            "visual_quality": "not_checked"}


def run_drive_crop_audit(service: Any, *, ticker: str, year: str,
                         elements_file_id: str, crop_folder_id: str | None) -> dict[str, Any]:
    """Save one scoped audit to Drive 3; never write to source Drive folders."""
    if ticker not in PILOT_TICKERS or not re.fullmatch(r"20[0-9]{2}", year):
        raise ValueError("Audit is restricted to pilot ticker/year selections")
    store = DriveStore(service)
    metadata = service.files().get(fileId=elements_file_id, fields="id,name,size,md5Checksum",
                                   supportsAllDrives=True).execute(num_retries=3)
    if metadata.get("name") != "elements.jsonl":
        raise ValueError("Expected adapter elements.jsonl")
    with tempfile.TemporaryDirectory(prefix="module1-crop-audit-") as temporary:
        elements = Path(temporary) / "elements.jsonl"
        store.download(metadata, elements)
        report = audit_crop_refs(store, elements, ticker=ticker, year=year,
                                 crop_folder_id=crop_folder_id)
        report["elements_drive_file_id"] = elements_file_id
        report["elements_md5"] = metadata.get("md5Checksum")
        path = Path(temporary) / f"{year}.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
        root = store.ensure_result_folder(RESULT_FOLDER_ID, "module_01_pilot")
        audits = store.ensure_result_folder(root, "crop_audits")
        folder = store.ensure_result_folder(audits, ticker)
        uploaded = store.upload_result(folder, path)
    return {"ticker": ticker, "year": year, "counts": report["counts"],
            "unique_asset_count": report["unique_asset_count"],
            "reference_count": report["reference_count"],
            "drive_file_id": uploaded["id"]}
