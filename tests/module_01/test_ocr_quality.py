import copy
import errno
import json
from pathlib import Path

import pytest

from financial_multiagent.ingestion import ocr_quality as quality


def bundle(tmp_path):
    folder = tmp_path / "bundle"
    folder.mkdir()
    prefix = "AAA-2025/v1/p1"
    common = dict(document_id="AAA-2025", document_version="v1", access_scope="tenant-a",
                  page=1, page_image_ref="Financial_OCR/AAA/2025/page_1.png",
                  ocr_json_ref="Financial_Report_OCR/AAA/2025/ocr_json/page_1.json",
                  ocr_confidence=None)
    records = [
        dict(common, kind="ocr_block", element_id="text_1", source_ref=f"{prefix}/text_1",
             text="Báo cáo", bbox=[1, 1, 90, 10]),
        dict(common, kind="table", element_id="table_1", source_ref=f"{prefix}/table_1",
             text=None, bbox=[1, 20, 90, 90], cell_count=1),
        dict(common, kind="table_cell", table_id="table_1", cell_id="cell_1",
             source_ref=f"{prefix}/table_1/cell_1", text="1.234", bbox=[2, 21, 40, 30], row=0, col=0),
    ]
    manifest = dict(document_id="AAA-2025", document_version="v1", access_scope="tenant-a",
                    source_bundle_sha256="a" * 64, ingestion_status="processing", issues=[],
                    pages=[dict(page_number=1, width=100, height=100,
                                page_image_ref=common["page_image_ref"], ocr_json_ref=common["ocr_json_ref"])],
                    record_counts={"ocr_block": 1, "table": 1, "table_cell": 1})
    save(folder, manifest, records)
    return folder, manifest, records


def save(folder, manifest, records):
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (folder / "elements.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def test_quality_saves_complete_runs_without_reading_images(tmp_path):
    folder, manifest, _ = bundle(tmp_path)
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    first = quality.assess_ocr_bundle(folder, access_scope="tenant-a")
    second = quality.assess_ocr_bundle(folder, access_scope="tenant-a")
    assert first["quality_status"] == "checks_passed"
    assert first["visual_accuracy"] == "not_measured"
    assert first["source_verification"] == "from_adapter_manifest"
    assert first["metrics"]["record_count"] == 3
    assert first["checks"]["confidence"]["status"] == "skipped"
    assert first["metrics"] == second["metrics"]
    assert first["output_dir"] != second["output_dir"]
    assert manifest["ingestion_status"] == "processing"
    for name, value in before.items():
        assert (folder / name).read_bytes() == value
    assert {p.name for p in Path(first["output_dir"]).iterdir()} == {
        "quality_report.json", "quality_issues.jsonl", "quality_report.md"}


@pytest.mark.parametrize("text", ["", "-", "—", "1.234", "100", "2025", "31/12/2025", "Mã số 100"])
def test_valid_cell_text_is_not_invented_or_flagged(tmp_path, text):
    _, manifest, records = bundle(tmp_path)
    records[-1]["text"] = text
    report = quality.OCRQualityValidator().assess(manifest, records)
    assert report.quality_status == "checks_passed"
    assert records[-1]["text"] == text


def test_suspect_text_and_numbers_have_stable_source_issues(tmp_path):
    _, manifest, records = bundle(tmp_path)
    records[0]["text"] = "Báo \ufffd cáo\x00"
    records[-1]["text"] = "1O0"
    before = copy.deepcopy(records)
    first = quality.OCRQualityValidator().assess(manifest, records)
    second = quality.OCRQualityValidator().assess(manifest, records)
    assert first.quality_status == "needs_review"
    assert {i.code for i in first.issues} == {"SUSPICIOUS_CHARACTERS", "SUSPICIOUS_NUMERIC_TEXT"}
    assert [i.to_dict() for i in first.issues] == [i.to_dict() for i in second.issues]
    assert all(i.source_ref for i in first.issues)
    assert before == records


@pytest.mark.parametrize("mutation", ["identity", "ref", "duplicate", "count", "bbox", "text_type", "kind", "orphan", "adapter_issues"])
def test_bad_contract_cannot_look_like_success(tmp_path, mutation):
    _, manifest, records = bundle(tmp_path)
    if mutation == "identity":
        records[0]["document_version"] = "v2"
    elif mutation == "ref":
        records[0]["source_ref"] = "../outside"
    elif mutation == "duplicate":
        records.append(copy.deepcopy(records[0]))
    elif mutation == "count":
        manifest["record_counts"]["table_cell"] = 20
    elif mutation == "bbox":
        records[0]["bbox"] = [0, 0, 200, 300]
    elif mutation == "text_type":
        records[0]["text"] = 123
    elif mutation == "kind":
        records[0]["kind"] = ["table"]
    elif mutation == "orphan":
        records[-1]["table_id"] = "missing"
        records[-1]["source_ref"] = "AAA-2025/v1/p1/missing/cell_1"
    elif mutation == "adapter_issues":
        manifest["issues"] = None
    report = quality.OCRQualityValidator().assess(manifest, records)
    assert report.execution_status == "failed"
    assert report.quality_status == "not_assessed"
    assert report.checks["text"]["status"] == "skipped"


def test_scope_is_checked_before_reading_records(tmp_path, monkeypatch):
    folder, _, _ = bundle(tmp_path)
    original = Path.read_bytes

    def read(path):
        assert path.name != "elements.jsonl", "Records should not be accessed"
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    with pytest.raises(PermissionError):
        quality.assess_ocr_bundle(folder, access_scope="tenant-b")
    assert not (folder / "quality").exists()


def test_foreign_record_scope_does_not_publish_report(tmp_path):
    folder, manifest, records = bundle(tmp_path)
    records[-1]["access_scope"] = "tenant-b"
    save(folder, manifest, records)
    with pytest.raises(PermissionError):
        quality.assess_ocr_bundle(folder, access_scope="tenant-a")
    assert not (folder / "quality").exists()


def test_geometry_and_duplicate_position_require_review(tmp_path):
    _, manifest, records = bundle(tmp_path)
    cell = dict(records[-1], cell_id="cell_2", source_ref="AAA-2025/v1/p1/table_1/cell_2", bbox=[0, 15, 30, 25])
    records.append(cell)
    records[1]["cell_count"] = manifest["record_counts"]["table_cell"] = 2
    report = quality.OCRQualityValidator().assess(manifest, records)
    assert report.execution_status == "completed"
    assert {i.code for i in report.issues} == {"CELL_OUTSIDE_TABLE", "DUPLICATE_CELL_POSITION"}


def test_optional_crops_are_deduplicated_and_do_not_block(tmp_path):
    _, manifest, records = bundle(tmp_path)
    issue = dict(code="MISSING_CROP", location="p1/text_1", field="crop")
    manifest["issues"] = [issue, issue]
    report = quality.OCRQualityValidator().assess(manifest, records)
    assert len(report.issues) == 1
    assert report.quality_status == "checks_passed"
    assert not report.issues[0].review_required


def test_confidence_threshold_requires_explicit_scale(tmp_path):
    _, manifest, records = bundle(tmp_path)
    with pytest.raises(ValueError):
        quality.QualityProfile(minimum_confidence=0.8)
    records[0]["ocr_confidence"] = 20
    report = quality.OCRQualityValidator().assess(
        manifest, records, quality.QualityProfile(confidence_scale=(0, 100), minimum_confidence=80))
    assert report.quality_status == "needs_review"
    assert report.issues[0].code == "LOW_CONFIDENCE"


def test_disabled_required_check_is_not_assessed(tmp_path):
    _, manifest, records = bundle(tmp_path)
    profile = quality.QualityProfile(enabled_checks=("contract",))
    assert quality.OCRQualityValidator().assess(manifest, records, profile).quality_status == "not_assessed"


def test_corrupt_records_produce_failed_run(tmp_path):
    folder, _, _ = bundle(tmp_path)
    (folder / "elements.jsonl").write_text("{invalid", encoding="utf-8")
    report = quality.assess_ocr_bundle(folder, access_scope="tenant-a")
    assert report["execution_status"] == "failed"
    assert report["quality_status"] == "not_assessed"
    assert Path(report["output_dir"]).is_dir()


def test_write_failure_does_not_publish_completed_run(tmp_path, monkeypatch):
    folder, _, _ = bundle(tmp_path)
    original = Path.write_text

    def fail(path, *args, **kwargs):
        if path.name == "quality_report.md":
            raise OSError(errno.EIO, "simulated transient Drive failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail)
    monkeypatch.setattr(quality.time, "sleep", lambda _: None)
    with pytest.raises(OSError):
        quality.assess_ocr_bundle(folder, access_scope="tenant-a")
    assert list((folder / "quality").iterdir()) == []


def test_transient_write_recovers_with_trace(tmp_path, monkeypatch):
    folder, _, _ = bundle(tmp_path)
    original = Path.write_text
    failed = False

    def once(path, *args, **kwargs):
        nonlocal failed
        if path.name == "quality_report.md" and not failed:
            failed = True
            raise OSError(errno.EIO, "temporary")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", once)
    monkeypatch.setattr(quality.time, "sleep", lambda _: None)
    report = quality.assess_ocr_bundle(folder, access_scope="tenant-a")
    saved = json.loads((Path(report["output_dir"]) / "quality_report.json").read_text(encoding="utf-8"))
    assert saved["trace"] == [{"operation": "write_report", "attempt": 1, "errno": errno.EIO}]


def test_bts_quality_when_sample_available(tmp_path):
    from financial_multiagent.ingestion.precomputed_ocr import import_ocr_bundle
    root = Path(__file__).resolve().parents[2] / "ocr_samples"
    json_dir = root / "Financial_Report_OCR/BTS/2025/ocr_json"
    image_dir = root / "Financial_OCR/BTS/2025"
    folder = tmp_path / "bts"
    existing = root.parent / "artifacts/module_01/BTS-2025"
    if (existing / "manifest.json").is_file() and (existing / "elements.jsonl").is_file():
        folder.mkdir()
        for name in ("manifest.json", "elements.jsonl"):
            (folder / name).write_bytes((existing / name).read_bytes())
    elif json_dir.is_dir() and image_dir.is_dir():
        import_ocr_bundle(json_dir, image_dir, folder, document_id="BTS-2025-report",
                          document_version="v1", access_scope="demo-scope", source_root=root)
    else:
        pytest.skip("Neither imported BTS artifacts nor source sample available")
    report = quality.assess_ocr_bundle(folder, access_scope="demo-scope")
    assert report["execution_status"] == "completed"
    assert report["metrics"]["page_count"] == 37
    assert report["metrics"]["record_counts"]["table_cell"] == 1410
    assert report["metrics"]["confidence_present"] == 0


def test_colab_notebook_quality_cells_use_current_api(tmp_path):
    folder, _, _ = bundle(tmp_path)
    notebook = Path(__file__).resolve().parents[2] / "colab/01_ocr_quality_report.ipynb"
    cells = json.loads(notebook.read_text(encoding="utf-8"))["cells"]
    for cell in cells:
        if cell["cell_type"] == "code":
            # A user may have executed the tracked notebook locally. Saved
            # outputs do not change whether its code calls the supported API.
            compile("".join(cell["source"]), str(notebook), "exec")
    context = dict(quality=quality, bundle_dir=folder, ACCESS_SCOPE="tenant-a", Path=Path)
    exec("".join(cells[9]["source"]), context)
    exec("".join(cells[11]["source"]), context)
    assert context["report"]["execution_status"] == "completed"
    assert context["issues"] == []
