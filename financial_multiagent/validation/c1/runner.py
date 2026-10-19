"""Load authorized snapshots, replay normalization, and persist one C1 run."""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from financial_multiagent.common.artifacts import (
    canonical_json, content_hash, new_run_id, publish_run, read_jsonl, read_scoped_json,
)
from financial_multiagent.ingestion.financial_normalization import (
    FinancialNormalizer, NormalizationContext, NORMALIZER_VERSION, ONTOLOGY_VERSION,
)
from financial_multiagent.ingestion.ocr_quality import VALIDATOR_VERSION
from .accounting import accounting_definitions
from .contracts import C1InputError, C1Profile, VERSION
from .engine import C1Validator


@dataclass(frozen=True)
class C1Input:
    bundle_dir: Path | str
    normalization_dir: Path | str


def load_input(spec, scope):
    """A modified/stale fact file cannot become trusted merely by changing state."""
    bundle, run = Path(spec.bundle_dir), Path(spec.normalization_dir)
    summary, summary_hash = read_scoped_json(run / "normalization_report.json", scope)
    manifest, manifest_hash = read_scoped_json(bundle / "manifest.json", scope)
    if summary.get("execution_status") != "completed":
        raise C1InputError("Normalization run is incomplete")
    if (summary.get("normalizer_version") != NORMALIZER_VERSION or summary.get("ontology_version") != ONTOLOGY_VERSION
            or summary.get("quality_validator_version") != VALIDATOR_VERSION):
        raise C1InputError("Normalization version cannot be replayed by current code")
    if any(summary.get(k) != manifest.get(k) for k in ("document_id", "document_version", "source_bundle_sha256")):
        raise C1InputError("Normalization identity/source version mismatch")
    records, record_hash = read_jsonl(bundle / "elements.jsonl")
    expected_hashes = {"manifest.json": manifest_hash, "elements.jsonl": record_hash}
    if summary.get("input_artifact_sha256") != expected_hashes:
        raise C1InputError("Stale normalization: adapter snapshot has changed")
    context_data = dict(summary.get("context", {}))
    try:
        context_data["period_overrides"] = {int(k): v for k, v in context_data.get("period_overrides", {}).items()}
        context = NormalizationContext(**context_data)
    except (ValueError, TypeError, AttributeError) as exc:
        raise C1InputError("Invalid saved normalization context") from exc
    if content_hash(asdict(context)) != summary.get("context_sha256"):
        raise C1InputError("Normalization context hash mismatch")
    facts, facts_hash = read_jsonl(run / "financial_facts.jsonl")
    if any(not isinstance(f, dict) or f.get("access_scope") != scope for f in facts):
        raise PermissionError("Fact outside caller scope")
    ref = summary.get("table_source_ref")
    selected = [r for r in records if isinstance(r, dict) and r.get("source_ref") == ref and r.get("kind") == "table"]
    if len(selected) != 1:
        raise C1InputError("Normalization table source is missing or ambiguous")
    table = selected[0]
    replay = FinancialNormalizer().normalize(manifest, records, page=table["page"],
                                            table_id=table["element_id"], context=context)
    if canonical_json(facts) != canonical_json(replay["facts"]) or len(facts) != summary.get("fact_count"):
        raise C1InputError("Financial facts differ from deterministic normalization replay")
    sources = {r["source_ref"]: r for r in records}
    dependency = dict(document_id=manifest["document_id"], document_version=manifest["document_version"],
        normalization_run_id=summary.get("run_id"), source_bundle_sha256=manifest["source_bundle_sha256"],
        hashes=dict(expected_hashes, normalization_report=summary_hash, financial_facts=facts_hash))
    return facts, sources, dependency


def run_c1(inputs, *, access_scope, output_dir=None, registry_path=None, context_path=None, profile=None):
    if not isinstance(access_scope, str) or not access_scope.strip() or not inputs:
        raise C1InputError("C1 requires explicit scope and input runs")
    profile = profile or C1Profile()
    facts, sources, dependencies = [], {}, []
    for spec in inputs:
        new_facts, new_sources, dependency = load_input(spec, access_scope)
        for ref, source in new_sources.items():
            if ref in sources and canonical_json(sources[ref]) != canonical_json(source):
                raise C1InputError("Conflicting source snapshots for the same ref")
        facts.extend(new_facts)
        sources.update(new_sources)
        dependencies.append(dependency)
    registry = (read_scoped_json(registry_path, access_scope)[0] if registry_path else
                dict(schema_version="1", access_scope=access_scope, rules=[], sources=[]))
    context = (read_scoped_json(context_path, access_scope)[0] if context_path else
               dict(schema_version="1", access_scope=access_scope, entity_attributes={}, disclosures=[]))
    assessment = C1Validator().validate(facts, access_scope=access_scope, financial_sources=sources,
                                       registry=registry, context=context, profile=profile)
    run_id = new_run_id()
    results = assessment.pop("results")
    report = dict(schema_version="1", c1_version=VERSION, run_id=run_id,
        created_at=datetime.now(timezone.utc).isoformat(), access_scope=access_scope,
        execution_status="completed", **assessment, profile=asdict(profile),
        inputs=dependencies, input_fact_count=len(facts), result_count=len(results),
        registry_sha256=content_hash(registry), context_sha256=content_hash(context),
        accounting_definitions_sha256=content_hash(accounting_definitions()),
        results_ref="validation_results.jsonl", registry_ref="registry.snapshot.json",
        context_ref="context.snapshot.json", ingestion_status_action="unchanged",
        limitations=["Only supplied facts and supplied reviewed rules are evaluated; this is not a full legal audit.",
                     "Raw image/PDF bytes are not reread. Finance refs resolve in the adapter snapshot; legal refs in stored reviewed text.",
                     "Normalization is replayed. Missing metadata/review inputs remain unknown.",
                     "C2 publication and C4 runtime verification remain separate stages."])
    markdown = ["# C1 Validation", "", f"Execution: {report['execution_status']}",
                f"Quality: {report['quality_status']}", f"Legal status: {report['legal_status']}",
                f"Outcomes: {report['outcome_counts']}", "",
                "Accounting discrepancies are warnings, not legal violations.",
                "Coverage is limited to supplied facts and rules. Document ready state is unchanged.", ""]
    markdown += [f"- {r['category']} / {r['name']}: {r['outcome']} — {r['reason']}" for r in results]
    destination = publish_run(output_dir or Path(inputs[0].bundle_dir) / "c1", run_id, {
        "c1_report.json": canonical_json(report) + "\n",
        "validation_results.jsonl": "".join(canonical_json(r) + "\n" for r in results),
        "registry.snapshot.json": canonical_json(registry) + "\n",
        "context.snapshot.json": canonical_json(context) + "\n",
        "c1_report.md": "\n".join(markdown) + "\n",
    })
    return dict(report, output_dir=str(destination))
