"""Import existing page OCR JSON and PNG files without changing either source.

This adapter handles only the precomputed-OCR input path of Module 1. It does
not infer financial facts, run OCR, validate compliance, or mark a document ready.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import struct
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any


PAGE_PATTERN = re.compile(r"page_(\d+)\.(json|png)\Z", re.IGNORECASE)
IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class OCRBundleError(ValueError):
    """The source bundle is incomplete or cannot be mapped without guessing."""


def _identifier(value: str, label: str) -> str:
    if not IDENTIFIER_PATTERN.fullmatch(value):
        raise OCRBundleError(f"{label} must use letters, digits, '.', '_' or '-': {value!r}")
    return value


def _page_files(directory: Path, suffix: str) -> dict[int, Path]:
    if not directory.is_dir():
        raise OCRBundleError(f"Missing {suffix} directory: {directory}")
    pages: dict[int, Path] = {}
    for path in directory.iterdir():
        if not path.is_file():
            continue
        match = PAGE_PATTERN.fullmatch(path.name)
        if not match or match.group(2).lower() != suffix:
            continue
        number = int(match.group(1))
        if number < 1 or number in pages:
            raise OCRBundleError(f"Duplicate or invalid page number: {path}")
        pages[number] = path
    if not pages:
        raise OCRBundleError(f"No page_*.{suffix} files in {directory}")
    return pages


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _png_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as stream:
        header = stream.read(24)
    if len(header) != 24 or header[:8] != PNG_SIGNATURE or header[12:16] != b"IHDR":
        raise OCRBundleError(f"Invalid PNG header: {path}")
    width, height = struct.unpack(">II", header[16:24])
    if not width or not height:
        raise OCRBundleError(f"Invalid PNG dimensions: {path}")
    return width, height


def _relative(path: Path, source_root: Path) -> str:
    # Keep the visible Drive path: a MyDrive shortcut may resolve to
    # /content/drive/.shortcut-targets-by-id outside MyDrive.
    absolute = Path(os.path.abspath(path))
    root = Path(os.path.abspath(source_root))
    try:
        return absolute.relative_to(root).as_posix()
    except ValueError as exc:
        raise OCRBundleError(f"Asset is outside source root: {path}") from exc


def _bbox(value: Any, width: int, height: int, location: str) -> list[int] | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) != 4 or any(
        isinstance(part, bool) or not isinstance(part, (int, float)) for part in value
    ):
        raise OCRBundleError(f"Invalid bbox at {location}: {value!r}")
    x0, y0, x1, y1 = value
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        raise OCRBundleError(f"BBox outside page at {location}: {value!r}")
    return list(value)


def _crop_refs(
    element: dict[str, Any], crop_root: Path, source_root: Path,
    issues: list[dict[str, Any]], location: str, *, validate_crops: bool = True,
) -> dict[str, str]:
    refs: dict[str, str] = {}
    for key, value in element.items():
        if not (key == "crop" or key.endswith("_crop")) or not isinstance(value, str):
            continue
        parts = Path(value.replace("\\", "/")).parts
        if "ocr_crops" not in parts:
            issues.append({"code": "UNMAPPABLE_CROP", "location": location, "field": key})
            continue
        suffix = parts[parts.index("ocr_crops") + 1 :]
        asset = crop_root.joinpath(*suffix)
        _relative(asset, crop_root)
        try:
            asset.resolve().relative_to(crop_root.resolve())
        except ValueError as exc:
            raise OCRBundleError(f"Crop escapes its source directory: {asset}") from exc
        if validate_crops and not asset.is_file():
            issues.append({"code": "MISSING_CROP", "location": location, "field": key, "expected_ref": _relative(asset, source_root)})
            continue
        refs[key] = _relative(asset, source_root)
    return refs


def _read_page(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise OCRBundleError(f"Invalid OCR JSON: {path}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("page"), dict) or not isinstance(data.get("elements"), list):
        raise OCRBundleError(f"OCR JSON needs page and elements: {path}")
    return data


def _records_for_page(
    data: dict[str, Any], *, number: int, width: int, height: int,
    image_ref: str, json_ref: str, document_id: str, document_version: str,
    access_scope: str, crop_root: Path, source_root: Path, issues: list[dict[str, Any]],
    validate_crops: bool,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen_elements: set[str] = set()
    for element in data["elements"]:
        if not isinstance(element, dict):
            raise OCRBundleError(f"Invalid element on page {number}")
        element_id = _identifier(str(element.get("id", "")), "element ID")
        if element_id in seen_elements:
            raise OCRBundleError(f"Duplicate element ID on page {number}: {element_id}")
        seen_elements.add(element_id)
        element_type = str(element.get("type", ""))
        if not element_type:
            raise OCRBundleError(f"Missing element type on page {number}: {element_id}")
        prefix = f"{document_id}/{document_version}/p{number}"
        base: dict[str, Any] = {
            "kind": "table" if element_type == "table" else "ocr_block" if element_type == "text" else "visual_element",
            "source_type": element_type,
            "source_ref": f"{prefix}/{element_id}",
            "document_id": document_id,
            "document_version": document_version,
            "access_scope": access_scope,
            "page": number,
            "element_id": element_id,
            "bbox": _bbox(element.get("bbox"), width, height, f"p{number}/{element_id}"),
            "text": element.get("text"),
            "ocr_confidence": element.get("confidence"),
            "page_image_ref": image_ref,
            "ocr_json_ref": json_ref,
        }
        asset_refs = _crop_refs(element, crop_root, source_root, issues, f"p{number}/{element_id}", validate_crops=validate_crops)
        if asset_refs:
            base["asset_refs"] = asset_refs
        if base["bbox"] is None:
            raise OCRBundleError(f"Missing bbox at p{number}/{element_id}")
        if element_type == "table":
            base["name"] = element.get("name")
            cells = element.get("cells", [])
            if not isinstance(cells, list):
                raise OCRBundleError(f"Invalid cells at p{number}/{element_id}")
            base["cell_count"] = len(cells)
        records.append(base)
        if element_type != "table":
            continue
        seen_cells: set[str] = set()
        for cell in cells:
            if not isinstance(cell, dict):
                raise OCRBundleError(f"Invalid cell at p{number}/{element_id}")
            cell_id = _identifier(str(cell.get("id", "")), "cell ID")
            if cell_id in seen_cells:
                raise OCRBundleError(f"Duplicate cell ID at p{number}/{element_id}: {cell_id}")
            seen_cells.add(cell_id)
            row, col = cell.get("row"), cell.get("col")
            if isinstance(row, bool) or not isinstance(row, int) or row < 0 or isinstance(col, bool) or not isinstance(col, int) or col < 0:
                raise OCRBundleError(f"Invalid row/col at p{number}/{element_id}/{cell_id}")
            records.append({
                "kind": "table_cell",
                "source_type": "table_cell",
                "source_ref": f"{prefix}/{element_id}/{cell_id}",
                "document_id": document_id,
                "document_version": document_version,
                "access_scope": access_scope,
                "page": number,
                "table_id": element_id,
                "cell_id": cell_id,
                "row": row,
                "col": col,
                "bbox": _bbox(cell.get("bbox"), width, height, f"p{number}/{element_id}/{cell_id}"),
                "text": cell.get("text"),
                "ocr_confidence": cell.get("confidence"),
                "page_image_ref": image_ref,
                "ocr_json_ref": json_ref,
            })
            if records[-1]["bbox"] is None:
                raise OCRBundleError(f"Missing bbox at p{number}/{element_id}/{cell_id}")
    return records


def import_ocr_bundle(
    ocr_json_dir: Path | str,
    page_image_dir: Path | str,
    output_dir: Path | str,
    *,
    document_id: str,
    document_version: str,
    access_scope: str,
    source_root: Path | str | None = None,
    validate_crops: bool = True,
) -> dict[str, Any]:
    """Write deterministic manifest.json and elements.jsonl for one report.

    document_version is supplied by the caller. For a legacy image bundle it
    identifies that bundle; it is not represented as a hash/version of a PDF.
    """
    document_id = _identifier(document_id, "document_id")
    document_version = _identifier(document_version, "document_version")
    if not access_scope.strip():
        raise OCRBundleError("access_scope must be supplied by the caller")
    json_dir = Path(os.path.abspath(ocr_json_dir))
    image_dir = Path(os.path.abspath(page_image_dir))
    destination = Path(os.path.abspath(output_dir))
    root = Path(os.path.abspath(source_root)) if source_root is not None else Path(os.path.commonpath([json_dir, image_dir]))
    _relative(json_dir, root)
    _relative(image_dir, root)
    crop_root = json_dir.parent / "ocr_crops"
    source_directories = (json_dir, image_dir, crop_root)
    if any(
        destination == source or destination in source.parents or source in destination.parents
        or destination.resolve() == source.resolve()
        or destination.resolve() in source.resolve().parents
        or source.resolve() in destination.resolve().parents
        for source in source_directories
    ):
        raise OCRBundleError("Output directory must be separate from source JSON, image, and crop directories")
    json_pages = _page_files(json_dir, "json")
    image_pages = _page_files(image_dir, "png")
    if json_pages.keys() != image_pages.keys():
        missing_images = sorted(json_pages.keys() - image_pages.keys())
        missing_json = sorted(image_pages.keys() - json_pages.keys())
        raise OCRBundleError(f"Unpaired pages: missing PNG {missing_images}, missing JSON {missing_json}")
    numbers = sorted(json_pages)
    if numbers != list(range(1, numbers[-1] + 1)):
        raise OCRBundleError(f"Pages are not contiguous from 1: {numbers}")

    pages: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    issues: list[dict[str, Any]] = []
    bundle_hash = hashlib.sha256()
    symbol: str | None = None
    report_year: str | None = None
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ocr-import-", dir=destination.parent) as temporary:
        stage = Path(temporary)
        records_path = stage / "elements.jsonl"
        with records_path.open("w", encoding="utf-8", newline="\n") as records_file:
            for number in numbers:
                json_path, image_path = json_pages[number], image_pages[number]
                data = _read_page(json_path)
                page_info = data["page"]
                if page_info.get("page") != number:
                    raise OCRBundleError(f"Page number mismatch: {json_path}")
                current_symbol, current_year = str(page_info.get("symbol", "")), str(page_info.get("year", ""))
                if not current_symbol or not current_year:
                    raise OCRBundleError(f"Missing symbol/year: {json_path}")
                if symbol is None:
                    symbol, report_year = current_symbol, current_year
                elif (symbol, report_year) != (current_symbol, current_year):
                    raise OCRBundleError(f"Mixed symbol/year in bundle: {json_path}")
                width, height = _png_dimensions(image_path)
                if (page_info.get("width"), page_info.get("height")) != (width, height):
                    raise OCRBundleError(f"PNG dimensions differ from OCR page metadata: {image_path}")
                image_ref = _relative(image_path, root)
                json_ref = _relative(json_path, root)
                json_hash, image_hash = _sha256(json_path), _sha256(image_path)
                bundle_hash.update(f"{number}:{json_hash}:{image_hash}\n".encode("ascii"))
                page_records = _records_for_page(
                    data, number=number, width=width, height=height,
                    image_ref=image_ref, json_ref=json_ref,
                    document_id=document_id, document_version=document_version,
                    access_scope=access_scope, crop_root=crop_root, source_root=root, issues=issues,
                    validate_crops=validate_crops,
                )
                for record in page_records:
                    records_file.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                    counts[record["kind"]] += 1
                pages.append({
                    "page_number": number,
                    "ocr_json_ref": json_ref,
                    "page_image_ref": image_ref,
                    "ocr_json_sha256": json_hash,
                    "page_image_sha256": image_hash,
                    "width": width,
                    "height": height,
                })
        manifest = {
            "source_format": "page_images",
            "symbol": symbol,
            "report_year": report_year,
            "document_id": document_id,
            "document_version": document_version,
            "original_pdf_ref": None,
            "original_pdf_sha256": None,
            "access_scope": access_scope,
            "adapter_status": "validated_with_warnings" if issues else "validated",
            "ingestion_status": "processing",
            "source_bundle_sha256": bundle_hash.hexdigest(),
            "pages": pages,
            "record_counts": dict(sorted(counts.items())),
            "issues": issues,
        }
        if not validate_crops:
            manifest["crop_validation"] = "deferred"
        (stage / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        destination.mkdir(parents=True, exist_ok=True)
        existing = destination / "manifest.json"
        if existing.is_file():
            old = json.loads(existing.read_text(encoding="utf-8"))
            if old != manifest:
                raise OCRBundleError("Output already contains a different source bundle or document version")
        os.replace(records_path, destination / "elements.jsonl")
        os.replace(stage / "manifest.json", destination / "manifest.json")
    return manifest


def resolve_source_ref(
    output_dir: Path | str, source_root: Path | str, source_ref: str, *, access_scope: str,
) -> tuple[dict[str, Any], Path]:
    """Resolve one imported element to its record and local page image."""
    output = Path(output_dir)
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    if manifest["access_scope"] != access_scope:
        raise PermissionError("Source is outside the supplied access scope")
    prefix = f"{manifest['document_id']}/{manifest['document_version']}/"
    if not source_ref.startswith(prefix):
        raise KeyError(source_ref)
    root = Path(os.path.abspath(source_root))
    with (output / "elements.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            record = json.loads(line)
            if record["source_ref"] != source_ref:
                continue
            image = Path(os.path.abspath(root / record["page_image_ref"]))
            _relative(image, root)
            if not image.is_file():
                raise OCRBundleError(f"Page image no longer exists: {image}")
            return record, image
    raise KeyError(source_ref)


def main() -> None:
    parser = argparse.ArgumentParser(description="Import existing page OCR JSON and PNG files")
    parser.add_argument("--ocr-json-dir", type=Path, required=True)
    parser.add_argument("--page-image-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--document-id", required=True)
    parser.add_argument("--document-version", required=True, help="Version of this OCR bundle, not a PDF version")
    parser.add_argument("--access-scope", required=True)
    args = parser.parse_args()
    try:
        manifest = import_ocr_bundle(
            args.ocr_json_dir, args.page_image_dir, args.output_dir,
            document_id=args.document_id,
            document_version=args.document_version,
            access_scope=args.access_scope,
            source_root=args.source_root,
        )
    except OCRBundleError as exc:
        parser.exit(2, f"OCR bundle error: {exc}\n")
    print(json.dumps({
        "document_id": manifest["document_id"],
        "document_version": manifest["document_version"],
        "pages": len(manifest["pages"]),
        "record_counts": manifest["record_counts"],
        "output_dir": str(args.output_dir),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
