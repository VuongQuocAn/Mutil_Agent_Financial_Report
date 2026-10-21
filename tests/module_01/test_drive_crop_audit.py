import json

from financial_multiagent.ingestion.drive_crop_audit import audit_crop_refs
from financial_multiagent.ingestion.drive_colab_runner import FOLDER_MIME


class FakeStore:
    def __init__(self):
        self.calls = []

    def children(self, folder_id):
        self.calls.append(folder_id)
        return {
            "root": [{"id": "p7", "name": "page_7", "mimeType": FOLDER_MIME}],
            "p7": [{"id": "sig", "name": "signatures", "mimeType": FOLDER_MIME}],
            "sig": [{"id": "a", "name": "signature_001.png", "mimeType": "image/png",
                     "size": "123", "md5Checksum": "abc"}],
        }[folder_id]


def test_audit_checks_metadata_and_preserves_source_refs(tmp_path):
    path = tmp_path / "elements.jsonl"
    prefix = "Financial_Report_OCR/BPC/2024/ocr_crops/page_7/signatures/"
    records = [
        {"source_ref": "BPC-2024/v1/p7/e1", "asset_refs": {"crop": prefix + "signature_001.png"}},
        {"source_ref": "BPC-2024/v1/p7/e2", "asset_refs": {"crop": prefix + "signature_001.png"}},
        {"source_ref": "BPC-2024/v1/p7/e3", "asset_refs": {"crop": prefix + "missing.png"}},
        {"source_ref": "BPC-2024/v1/p7/e4", "asset_refs": {"crop": "../wrong.png"}},
    ]
    path.write_text("\n".join(json.dumps(x) for x in records) + "\n", encoding="utf-8")
    store = FakeStore()
    result = audit_crop_refs(store, path, ticker="BPC", year="2024", crop_folder_id="root")
    assert result["counts"] == {"found": 1, "missing": 1, "missing_crop_root": 0,
                                "ambiguous": 0, "invalid_ref": 1}
    assert result["reference_count"] == 4
    assert result["visual_quality"] == "not_checked"
    assert result["assets"][2]["source_refs"] == ["BPC-2024/v1/p7/e1", "BPC-2024/v1/p7/e2"]
    assert store.calls.count("root") == 1
