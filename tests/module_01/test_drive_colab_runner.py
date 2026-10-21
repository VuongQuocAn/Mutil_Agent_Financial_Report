"""Pilot selection and resume gates without connecting to Drive."""

import json
import pytest

from financial_multiagent.ingestion.drive_colab_runner import (
    DriveStore, PilotConfig, _batch_status_name, _source_identity, _tables,
)


def _item(number, kind, checksum):
    return {"id": f"id-{kind}-{number}", "name": f"page_{number}.{kind}",
            "size": "42", "md5Checksum": checksum, "modifiedTime": "2026-10-20T00:00:00Z"}


def test_source_fingerprint_changes_when_a_page_changes():
    json_pages = {1: _item(1, "json", "a"), 2: _item(2, "json", "b")}
    png_pages = {1: _item(1, "png", "c"), 2: _item(2, "png", "d")}
    original = _source_identity(json_pages, png_pages)
    png_pages[2] = _item(2, "png", "changed")
    assert _source_identity(json_pages, png_pages) != original


def test_unpaired_or_missing_pages_stop_before_processing():
    with pytest.raises(ValueError):
        _source_identity({1: _item(1, "json", "a")}, {})
    with pytest.raises(ValueError):
        _source_identity({2: _item(2, "json", "a")}, {2: _item(2, "png", "b")})


def test_pilot_does_not_expand_to_other_tickers():
    assert PilotConfig().tickers == ("BTS", "BTW", "BPC")
    with pytest.raises(ValueError):
        PilotConfig(tickers=("CAG",))


def test_selected_runs_do_not_replace_full_batch_status():
    assert _batch_status_name(PilotConfig()) == "batch_status.json"
    selected = PilotConfig(tickers=("BTW",), years=("2022", "2023"))
    assert _batch_status_name(selected).startswith("batch_status_selection_")
    assert _batch_status_name(selected) == _batch_status_name(selected)
    assert _batch_status_name(selected) != _batch_status_name(PilotConfig(tickers=("BPC",), years=("2024",)))


def test_only_primary_income_statement_is_selected(tmp_path):
    records = [
        {"kind": "table", "name": "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
         "page": 12, "element_id": "table_001"},
        {"kind": "table", "name": "Thay đổi trên Báo cáo kết quả hoạt động kinh doanh cho năm 2021",
         "page": 38, "element_id": "table_002"},
    ]
    (tmp_path / "elements.jsonl").write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records), encoding="utf-8")
    assert _tables(tmp_path) == [(12, "table_001")]


class _FakeGet:
    def __init__(self, value):
        self.value = value

    def execute(self, **_kwargs):
        return self.value


class _FakeFiles:
    def __init__(self, files):
        self.files = files

    def get(self, *, fileId, **_kwargs):
        return _FakeGet(self.files[fileId])


class _FakeService:
    def __init__(self, files):
        self._files = _FakeFiles(files)

    def files(self):
        return self._files


def test_resume_requires_matching_inputs_code_config_and_uploaded_hashes():
    store = DriveStore(_FakeService({"artifact-id": {"id": "artifact-id", "md5Checksum": "abc", "trashed": False}}))
    store.read_result_json = lambda *_args: {
        "status": "completed", "source_fingerprint": "source-1", "code_revision": "commit-1",
        "config_fingerprint": "config-1", "artifacts": {"manifest.json": {"id": "artifact-id", "md5": "abc"}},
    }
    assert store.completion_valid("folder", source="source-1", revision="commit-1", config="config-1")
    assert not store.completion_valid("folder", source="source-2", revision="commit-1", config="config-1")
    store.service = _FakeService({"artifact-id": {"id": "artifact-id", "md5Checksum": "different", "trashed": False}})
    assert not store.completion_valid("folder", source="source-1", revision="commit-1", config="config-1")
