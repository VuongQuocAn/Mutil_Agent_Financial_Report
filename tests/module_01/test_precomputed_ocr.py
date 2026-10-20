"""Contract tests for importing already-produced OCR, without rerunning OCR."""

import json
import struct
import zlib
from pathlib import Path

import pytest

from financial_multiagent.ingestion.precomputed_ocr import (
    OCRBundleError,
    import_ocr_bundle,
    resolve_source_ref,
)


def _png(path: Path, width: int = 20, height: int = 20) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    def chunk(kind: bytes, payload: bytes) -> bytes:
        data = kind + payload
        return struct.pack(">I", len(payload)) + data + struct.pack(">I", zlib.crc32(data))

    rows = b"".join(b"\x00" + b"\xff\xff\xff" * width for _ in range(height))
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def _sample(tmp_path: Path, *, missing_crop: bool = False) -> tuple[Path, Path, Path, Path]:
    root = tmp_path / "data"
    json_dir = root / "Financial_Report_OCR" / "AAA" / "2025" / "ocr_json"
    image_dir = root / "Financial_OCR" / "AAA" / "2025"
    crop = json_dir.parent / "ocr_crops" / "page_1" / "signatures" / "signature_001.png"
    json_dir.mkdir(parents=True)
    _png(image_dir / "page_1.png")
    if not missing_crop:
        _png(crop)
    page = {
        "page": {"symbol": "AAA", "year": "2025", "page": 1, "width": 20, "height": 20},
        "elements": [
            {"id": "text_0001", "type": "text", "bbox": [1, 1, 8, 4], "text": "Báo cáo"},
            {"id": "table_001", "type": "table", "bbox": [1, 5, 18, 17], "cells": [
                {"id": "table_001_cell_001", "row": 0, "col": 0, "bbox": [2, 6, 12, 9], "text": "1.234"}
            ]},
            {"id": "signature_001", "type": "signature", "bbox": [1, 17, 5, 19],
             "crop": "/content/drive/MyDrive/Financial_Report_OCR/AAA/2025/ocr_crops/page_1/signatures/signature_001.png"},
        ],
        "provenance": {"text_recognizer": "example"},
    }
    source_json = json_dir / "page_1.json"
    source_json.write_text(json.dumps(page, ensure_ascii=False), encoding="utf-8")
    return root, json_dir, image_dir, source_json


def _import(root: Path, json_dir: Path, image_dir: Path, output: Path) -> dict:
    return import_ocr_bundle(
        json_dir, image_dir, output, document_id="AAA-2025-report",
        document_version="ocr-bundle-v1", access_scope="tenant-a", source_root=root,
    )


def test_import_preserves_sources_and_resolves_cell(tmp_path: Path) -> None:
    root, json_dir, image_dir, source_json = _sample(tmp_path)
    source_before = source_json.read_bytes()
    output = tmp_path / "artifacts"

    manifest = _import(root, json_dir, image_dir, output)

    assert manifest["source_format"] == "page_images"
    assert manifest["original_pdf_ref"] is None
    assert manifest["adapter_status"] == "validated"
    assert manifest["ingestion_status"] == "processing"
    assert manifest["record_counts"] == {"ocr_block": 1, "table": 1, "table_cell": 1, "visual_element": 1}
    ref = "AAA-2025-report/ocr-bundle-v1/p1/table_001/table_001_cell_001"
    record, image = resolve_source_ref(output, root, ref, access_scope="tenant-a")
    assert (record["text"], record["row"], record["col"], record["bbox"]) == ("1.234", 0, 0, [2, 6, 12, 9])
    assert image == (image_dir / "page_1.png").resolve()
    assert record["ocr_confidence"] is None
    assert record["access_scope"] == "tenant-a"
    assert source_json.read_bytes() == source_before
    assert "/content/drive/" not in (output / "elements.jsonl").read_text(encoding="utf-8")
    with pytest.raises(PermissionError):
        resolve_source_ref(output, root, ref, access_scope="tenant-b")

    records_before = (output / "elements.jsonl").read_bytes()
    manifest_before = (output / "manifest.json").read_bytes()
    _import(root, json_dir, image_dir, output)
    assert (output / "elements.jsonl").read_bytes() == records_before
    assert (output / "manifest.json").read_bytes() == manifest_before

    page = json.loads(source_json.read_text(encoding="utf-8"))
    page["elements"][0]["text"] = "Báo cáo đã đổi"
    source_json.write_text(json.dumps(page, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(OCRBundleError, match="different source bundle"):
        _import(root, json_dir, image_dir, output)
    assert (output / "manifest.json").read_bytes() == manifest_before


def test_missing_optional_crop_is_reported_without_losing_page_source(tmp_path: Path) -> None:
    root, json_dir, image_dir, _ = _sample(tmp_path, missing_crop=True)
    output = tmp_path / "artifacts"
    manifest = _import(root, json_dir, image_dir, output)
    assert manifest["adapter_status"] == "validated_with_warnings"
    assert manifest["issues"][0]["code"] == "MISSING_CROP"
    ref = "AAA-2025-report/ocr-bundle-v1/p1/signature_001"
    record, image = resolve_source_ref(output, root, ref, access_scope="tenant-a")
    assert "asset_refs" not in record
    assert image.is_file()


def test_remote_crop_ref_is_preserved_without_downloading_crop(tmp_path: Path) -> None:
    root, json_dir, image_dir, _ = _sample(tmp_path, missing_crop=True)
    output = tmp_path / "artifacts"
    manifest = import_ocr_bundle(
        json_dir, image_dir, output, document_id="AAA-2025-report",
        document_version="drive-v1", access_scope="tenant-a", source_root=root,
        validate_crops=False,
    )
    assert manifest["crop_validation"] == "deferred"
    assert manifest["issues"] == []
    records = [json.loads(line) for line in (output / "elements.jsonl").read_text(encoding="utf-8").splitlines()]
    signature = next(item for item in records if item.get("element_id") == "signature_001")
    assert signature["asset_refs"]["crop"].endswith("ocr_crops/page_1/signatures/signature_001.png")


@pytest.mark.parametrize("change", ["missing_image", "missing_json", "invalid_json", "wrong_bbox", "missing_bbox", "duplicate_id", "page_mismatch"])
def test_invalid_bundle_fails_without_publishing(tmp_path: Path, change: str) -> None:
    root, json_dir, image_dir, source_json = _sample(tmp_path)
    page = json.loads(source_json.read_text(encoding="utf-8"))
    if change == "missing_image":
        (image_dir / "page_1.png").unlink()
    elif change == "missing_json":
        source_json.unlink()
    elif change == "invalid_json":
        source_json.write_text("{invalid", encoding="utf-8")
    elif change == "wrong_bbox":
        page["elements"][1]["cells"][0]["bbox"] = [2, 6, 100, 9]
    elif change == "missing_bbox":
        del page["elements"][1]["cells"][0]["bbox"]
    elif change == "duplicate_id":
        page["elements"][0]["id"] = "table_001"
    elif change == "page_mismatch":
        page["page"]["page"] = 2
    if change not in {"missing_json", "invalid_json"}:
        source_json.write_text(json.dumps(page, ensure_ascii=False), encoding="utf-8")
    output = tmp_path / "artifacts"
    with pytest.raises(OCRBundleError):
        _import(root, json_dir, image_dir, output)
    assert not (output / "manifest.json").exists()


def test_output_cannot_be_written_into_source_root(tmp_path: Path) -> None:
    root, json_dir, image_dir, _ = _sample(tmp_path)
    for output in (root, json_dir / "generated", image_dir / "generated", json_dir.parent / "ocr_crops" / "generated"):
        with pytest.raises(OCRBundleError, match="separate from source"):
            _import(root, json_dir, image_dir, output)


def test_output_can_be_sibling_folder_within_drive_root(tmp_path: Path) -> None:
    root, json_dir, image_dir, _ = _sample(tmp_path)
    output = root / "Financial_MultiAgent_Results" / "AAA" / "2025"
    manifest = _import(root, json_dir, image_dir, output)
    assert manifest["record_counts"]["table_cell"] == 1
    assert (output / "manifest.json").is_file()
    assert (output / "elements.jsonl").is_file()


def test_drive_shortcuts_keep_visible_source_refs(tmp_path: Path) -> None:
    root, json_dir, image_dir, _ = _sample(tmp_path)
    target_root = tmp_path / "drive" / ".shortcut-targets-by-id" / "example-id"
    target_root.mkdir(parents=True)
    for folder in ("Financial_Report_OCR", "Financial_OCR"):
        source = root / folder
        target = target_root / folder
        source.rename(target)
        try:
            source.symlink_to(target, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"Directory symlinks unavailable: {exc}")

    output = root / "Financial_MultiAgent_Results" / "AAA" / "2025"
    manifest = _import(root, json_dir, image_dir, output)
    assert manifest["pages"][0]["ocr_json_ref"] == "Financial_Report_OCR/AAA/2025/ocr_json/page_1.json"
    assert manifest["pages"][0]["page_image_ref"] == "Financial_OCR/AAA/2025/page_1.png"
    ref = "AAA-2025-report/ocr-bundle-v1/p1/table_001/table_001_cell_001"
    record, image = resolve_source_ref(output, root, ref, access_scope="tenant-a")
    assert record["text"] == "1.234"
    assert image == image_dir / "page_1.png"
    assert image.is_file()


def test_bts_2025_sample_when_available(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2] / "ocr_samples"
    json_dir = root / "Financial_Report_OCR" / "BTS" / "2025" / "ocr_json"
    image_dir = root / "Financial_OCR" / "BTS" / "2025"
    if not json_dir.is_dir() or not image_dir.is_dir():
        pytest.skip("BTS OCR sample is not present locally")
    output = tmp_path / "bts"
    manifest = import_ocr_bundle(
        json_dir, image_dir, output, document_id="BTS-2025-report",
        document_version="ocr-bundle-bts-2025-v1", access_scope="demo-scope", source_root=root,
    )
    assert [page["page_number"] for page in manifest["pages"]] == list(range(1, 38))
    assert manifest["record_counts"]["table_cell"] == 1410
    assert manifest["original_pdf_ref"] is None
    ref = "BTS-2025-report/ocr-bundle-bts-2025-v1/p12/table_001/table_001_cell_009"
    record, image = resolve_source_ref(output, root, ref, access_scope="demo-scope")
    assert record["text"] == "2.818.277.552.061"
    assert (record["row"], record["col"], record["bbox"]) == (1, 3, [1509, 757, 1856, 795])
    assert image == (image_dir / "page_12.png").resolve()
