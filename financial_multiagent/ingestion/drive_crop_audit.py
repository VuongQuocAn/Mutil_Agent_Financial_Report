"""Audit whether referenced OCR crops exist on the read-only source Drive.

This checks Drive metadata only. It does not download crop bytes, judge visual
quality, or promote a report to ready. Results belong in the writable Drive.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .drive_colab_runner import DriveStore, FOLDER_MIME

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
