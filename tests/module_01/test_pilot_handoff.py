from financial_multiagent.ingestion.drive_colab_runner import _sha256
from financial_multiagent.ingestion.pilot_handoff import (
    build_handoff, choose_b02, choose_statements,
)


def test_handoff_uses_best_b02_run_only_for_same_source():
    def marker(source, facts):
        return {"source_fingerprint": source, "code_revision": str(facts),
                "summary": {"income_statement_tables": [
                    {"normalization": {"fact_count": facts}}]}}

    selected = choose_b02([("old", marker("source-a", 0)),
                           ("reviewed", marker("source-a", 38)),
                           ("other-source", marker("source-b", 50))], "source-a")
    assert selected[0] == "reviewed"


def test_handoff_prefers_new_statement_layout_without_mixing_sources():
    source = _sha256({"source_fingerprint": "source-a", "manifest_md5": "m",
                      "elements_md5": "e"})
    markers = [("v1", {"source_fingerprint": source,
                       "summary": {"batch_version": "1", "fact_count": 51}}),
               ("v3", {"source_fingerprint": source,
                       "summary": {"batch_version": "3", "fact_count": 51}}),
               ("other", {"source_fingerprint": "wrong",
                          "summary": {"batch_version": "9", "fact_count": 100}})]
    selected = choose_statements(markers, "source-a", "m", "e")
    assert selected[0] == "v3"
    assert choose_statements(markers, "new-source", "m", "e") is None


def test_handoff_preserves_raw_ocr_when_no_numeric_table_is_readable():
    class Request:
        def __init__(self, file_id):
            self.file_id = file_id

        def execute(self, **_):
            return {"id": self.file_id, "md5Checksum": self.file_id}

    class Files:
        def get(self, *, fileId, **_):
            return Request(fileId)

    class Service:
        def files(self):
            return Files()

    class Store:
        service = Service()

    artifacts = {name: {"id": name, "md5": name, "size": 1}
                 for name in ("manifest.json", "elements.jsonl", "drive_source_map.json")}
    marker = {"source_fingerprint": "source-a", "artifacts": artifacts,
              "summary": {"ticker": "BPC", "year": "2021", "access_scope": "pilot-drive-scope",
                          "document_id": "BPC-2021-report", "document_version": "drive-abc",
                          "page_count": 24, "ocr_quality_status": "needs_review",
                          "ocr_review_issue_count": 3}}
    handoff = build_handoff(Store(), ticker="BPC", year="2021", adapter_folder="folder",
                            adapter_marker=marker, b02=None, statements=None, crop_audit=None)
    assert handoff["adapter_artifacts"]["elements.jsonl"]["drive_file_id"] == "elements.jsonl"
    assert handoff["ingestion_status"] == "needs_review"
    assert handoff["knowledge_storage_published"] is False
    assert "B01_B03_CANDIDATES_UNAVAILABLE" in handoff["warnings"]
