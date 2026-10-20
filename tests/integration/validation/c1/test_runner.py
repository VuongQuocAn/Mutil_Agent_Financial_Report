import json
from pathlib import Path
import pytest

from financial_multiagent.validation.c1 import C1Input, run_c1
from financial_multiagent.validation.c1.contracts import C1InputError
from financial_multiagent.ingestion.financial_normalization import NORMALIZER_VERSION


def test_complete_run_replays_sources_and_saves_registry_snapshot(normalized_input):
    first = run_c1([normalized_input], access_scope="tenant-a")
    second = run_c1([normalized_input], access_scope="tenant-a")
    assert first["execution_status"] == "completed" and first["legal_status"] == "unknown"
    assert first["ingestion_status_action"] == "unchanged"
    results = [json.loads(s) for s in (Path(first["output_dir"]) / "validation_results.jsonl").read_text(encoding="utf-8").splitlines()]
    net = [r for r in results if r["category"] == "accounting" and r["name"] == "revenue_net"]
    assert len(net) == 2 and all(r["outcome"] == "pass" for r in net)
    assert first["inputs"] == second["inputs"]
    assert first["output_dir"] != second["output_dir"]
    assert (Path(first["output_dir"]) / "registry.snapshot.json").is_file()
    assert (Path(first["output_dir"]) / "validation_results.jsonl").read_bytes() == (Path(second["output_dir"]) / "validation_results.jsonl").read_bytes()


@pytest.mark.parametrize("mutation", ["fact_value", "fact_state", "source_snapshot", "context", "version"])
def test_changed_or_stale_artifacts_fail_before_publish(normalized_input, mutation):
    bundle = Path(normalized_input.bundle_dir)
    run = Path(normalized_input.normalization_dir)
    if mutation.startswith("fact_"):
        path = run / "financial_facts.jsonl"
        facts = [json.loads(s) for s in path.read_text(encoding="utf-8").splitlines()]
        facts[0]["value" if mutation == "fact_value" else "quality_state"] = "999" if mutation == "fact_value" else "needs_review"
        path.write_text("".join(json.dumps(f) + "\n" for f in facts), encoding="utf-8")
    elif mutation == "source_snapshot":
        with (bundle / "elements.jsonl").open("a", encoding="utf-8") as stream:
            stream.write("\n")
    else:
        path = run / "normalization_report.json"
        report = json.loads(path.read_text(encoding="utf-8"))
        if mutation == "context":
            report["context"]["company_id"] = "BBB"
        else:
            report["normalizer_version"] = "future-version"
        path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(C1InputError):
        run_c1([normalized_input], access_scope="tenant-a")
    assert not (bundle / "c1").exists()


def test_wrong_scope_denied_before_reading_facts(normalized_input, monkeypatch):
    original = Path.read_bytes
    def guarded(path):
        assert path.name not in {"financial_facts.jsonl", "elements.jsonl"}
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", guarded)
    with pytest.raises(PermissionError):
        run_c1([normalized_input], access_scope="tenant-b")


def test_same_normalization_run_is_not_counted_twice(normalized_input):
    with pytest.raises(C1InputError, match="duplicate fact"):
        run_c1([normalized_input, normalized_input], access_scope="tenant-a")


def test_failed_write_leaves_no_completed_c1_run(normalized_input, monkeypatch):
    original = Path.write_text
    def failed(path, *args, **kwargs):
        if path.name == "c1_report.md":
            raise OSError("simulated Drive write failure")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "write_text", failed)
    with pytest.raises(OSError):
        run_c1([normalized_input], access_scope="tenant-a")
    assert list((Path(normalized_input.bundle_dir) / "c1").iterdir()) == []


def test_bts_current_snapshot_is_unknown_without_context_and_rules(tmp_path):
    original = Path(__file__).resolve().parents[4] / "artifacts/module_01/BTS-2025"
    runs = sorted((original / "normalization").glob("*/normalization_report.json"))
    current_runs = [path for path in runs if json.loads(path.read_text(encoding="utf-8"))
                    .get("normalizer_version") == NORMALIZER_VERSION]
    if not current_runs:
        pytest.skip("Local BTS normalization for current version not available")
    result = run_c1([C1Input(original, current_runs[-1].parent)], access_scope="demo-scope", output_dir=tmp_path / "c1")
    assert result["execution_status"] == "completed" and result["legal_status"] == "unknown"
    assert result["outcome_counts"].get("violation", 0) == 0
    assert result["outcome_counts"].get("pass", 0) == 0
    rows = [json.loads(s) for s in (Path(result["output_dir"]) / "validation_results.jsonl").read_text(encoding="utf-8").splitlines()]
    net = [r for r in rows if r["category"] == "accounting" and r["name"] == "revenue_net"]
    assert all(r["details"]["numeric_diagnostic"]["residual"] == "0" for r in net)
    assert all(r["outcome"] == "unknown" for r in net)


def test_colab_c1_cells_use_current_api(normalized_input):
    notebook = Path(__file__).resolve().parents[4] / "colab/03_c1_validation.ipynb"
    cells = json.loads(notebook.read_text(encoding="utf-8"))["cells"]
    for cell in cells:
        if cell["cell_type"] == "code":
            compile("".join(cell["source"]), str(notebook), "exec")
    # Execute only the stage and result-view cells, not mount/Git operations.
    from financial_multiagent.validation.c1 import C1Input, run_c1
    env = dict(run_c1=run_c1, inputs=[normalized_input], ACCESS_SCOPE="tenant-a",
               REGISTRY_PATH=None, CONTEXT_PATH=None, Path=Path)
    stage = next(c for c in cells if c["cell_type"] == "code" and "report = run_c1(" in "".join(c["source"]))
    view = next(c for c in cells if c["cell_type"] == "code" and "results = [json.loads" in "".join(c["source"]))
    exec("".join(stage["source"]), env)
    exec("".join(view["source"]), env)
    assert env["report"]["legal_status"] == "unknown"
