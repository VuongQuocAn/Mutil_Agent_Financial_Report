from copy import deepcopy
import pytest

from financial_multiagent.validation.c1 import C1Validator
from financial_multiagent.validation.c1.contracts import C1InputError, C1Profile, make_result


def selected(result, category, name=None):
    return [r for r in result["results"] if r["category"] == category and (name is None or r["name"] == name)]


def rule_result(case):
    return selected(C1Validator().validate(**case), "regulatory", "TEST-ONLY")[0]


def test_all_accounting_identities_and_synthetic_rule_pass(c1_case):
    result = C1Validator().validate(**c1_case)
    checks = selected(result, "accounting")
    assert len(checks) == 6 and all(r["outcome"] == "pass" for r in checks)
    assert rule_result(c1_case)["outcome"] == "pass"
    assert selected(result, "coverage")  # Absent balance/cash statements are explicit.


def test_accounting_discrepancy_is_warning_not_legal_violation(c1_case):
    fact = c1_case["facts"][2]
    fact["value"] = fact["raw_value"] = "800"
    c1_case["financial_sources"][fact["source_ref"]]["text"] = "800"
    c1_case["registry"]["rules"] = []
    result = C1Validator().validate(**c1_case)
    assert selected(result, "accounting", "revenue_net")[0]["outcome"] == "warning"
    assert all(r["outcome"] != "violation" for r in result["results"])
    assert result["legal_status"] == "unknown"


@pytest.mark.parametrize("field,value", [("period_start", None), ("consolidation_scope", None),
    ("company_id", None), ("value", None), ("quality_state", "needs_review"),
    ("normalization_issues", ["INCOMPLETE_PERIOD"]), ("unit", None)])
def test_missing_or_unreviewed_input_never_passes_accounting(c1_case, field, value):
    for fact in c1_case["facts"]:
        fact[field] = value
    result = C1Validator().validate(**c1_case)
    assert all(r["outcome"] == "unknown" for r in selected(result, "accounting"))
    assert rule_result(c1_case)["outcome"] == "unknown"


def test_missing_tax_is_not_implicitly_zero(c1_case):
    c1_case["facts"] = [f for f in c1_case["facts"] if f["metric_code"] != "income_tax_deferred"]
    result = selected(C1Validator().validate(**c1_case), "accounting", "profit_after_tax")[0]
    assert result["outcome"] == "unknown" and "income_tax_deferred" in result["details"]["missing_metrics"]


def test_scope_mixing_does_not_complete_an_equation(c1_case):
    c1_case["facts"][0]["consolidation_scope"] = "consolidated"
    result = selected(C1Validator().validate(**c1_case), "accounting", "revenue_net")
    assert all(r["outcome"] == "unknown" for r in result)


@pytest.mark.parametrize("field", ["company_id", "period_end", "unit", "ontology_version"])
def test_other_context_boundaries_do_not_complete_equation(c1_case, field):
    c1_case["facts"][0][field] = {"company_id": "BBB", "period_end": "2026-12-31", "unit": "USD", "ontology_version": "other"}[field]
    assert all(r["outcome"] == "unknown" for r in selected(C1Validator().validate(**c1_case), "accounting", "revenue_net"))


def test_duplicate_metric_is_unknown(c1_case):
    duplicate = dict(c1_case["facts"][0], fact_id="duplicate")
    c1_case["facts"].append(duplicate)
    assert selected(C1Validator().validate(**c1_case), "accounting", "revenue_net")[0]["outcome"] == "unknown"


def test_dual_provenance_is_required_for_violation(c1_case):
    c1_case["registry"]["rules"][0]["predicate"]["threshold"] = "1000"
    result = rule_result(c1_case)
    assert result["outcome"] == "violation"
    assert result["financial_source_refs"] and result["legal_source_refs"]
    assert result["rule_version"] == "1" and result["applicability"] == "applicable"
    c1_case["registry"]["rules"][0]["legal_source_refs"] = []
    assert rule_result(c1_case)["outcome"] == "unknown"
    with pytest.raises(C1InputError):
        make_result("accounting", "bad", "violation", "not legal")


@pytest.mark.parametrize("mutation,expected", [
    ("expired", "not_applicable"), ("future", "not_applicable"), ("midyear", "unknown"),
    ("missing_start", "unknown"), ("unknown_end", "unknown"), ("exception", "not_applicable"),
    ("unknown_exception", "unknown"), ("other_company", "not_applicable"), ("unknown_entity", "unknown"),
    ("pending", "unknown"), ("pending_source", "unknown"), ("wrong_unit", "unknown"),
    ("wrong_ontology", "unknown"), ("invalid_threshold", "unknown"), ("invalid_end", "unknown"), ("reversed_period", "unknown"),
])
def test_rule_applicability_and_evidence(c1_case, mutation, expected):
    rule = c1_case["registry"]["rules"][0]
    rule["predicate"]["threshold"] = "1000"
    if mutation == "expired":
        rule.update(effective_to="2024-12-31", open_ended=False)
    elif mutation == "future":
        rule["effective_from"] = "2026-01-01"
    elif mutation == "midyear":
        rule["effective_from"] = "2025-06-01"
    elif mutation == "missing_start":
        rule["effective_from"] = None
    elif mutation == "unknown_end":
        rule["open_ended"] = False
    elif mutation == "exception":
        rule["exceptions"] = [dict(conditions=[dict(field="company_id", values=["AAA"])])]
    elif mutation == "unknown_exception":
        rule["exceptions"] = [dict(conditions=[dict(field="entity_type", values=["test"])])]
    elif mutation == "other_company":
        rule["applicability"] = [dict(field="company_id", values=["BBB"])]
    elif mutation == "unknown_entity":
        rule["applicability"] = [dict(field="entity_type", values=["test"])]
    elif mutation == "pending":
        rule["review_state"] = "needs_review"
    elif mutation == "pending_source":
        c1_case["registry"]["sources"][0]["review_state"] = "needs_review"
    elif mutation == "wrong_unit":
        rule["predicate"]["unit"] = "USD"
    elif mutation == "wrong_ontology":
        rule["ontology_version"] = "unknown"
    elif mutation == "invalid_threshold":
        rule["predicate"]["threshold"] = "NaN"
    elif mutation == "invalid_end":
        rule["effective_to"] = ""
    elif mutation == "reversed_period":
        for fact in c1_case["facts"]:
            fact["period_start"] = "2026-01-01"
    assert rule_result(c1_case)["outcome"] == expected


def test_synthetic_rule_disabled_by_default(c1_case):
    c1_case["profile"] = C1Profile()
    assert rule_result(c1_case)["reason"] == "SYNTHETIC_RULE_DISABLED"


def test_overlapping_rule_versions_are_unknown(c1_case):
    rule = deepcopy(c1_case["registry"]["rules"][0])
    rule["rule_version"] = "2"
    c1_case["registry"]["rules"].append(rule)
    assert all(r["reason"] == "OVERLAPPING_RULE_VERSIONS" for r in selected(C1Validator().validate(**c1_case), "regulatory", "TEST-ONLY"))


@pytest.mark.parametrize("status,coverage,expected", [("absent", "complete_reviewed", "violation"),
    ("present", "complete_reviewed", "pass"), ("absent", "partial", "unknown")])
def test_disclosure_requires_reviewed_complete_evidence(c1_case, status, coverage, expected):
    c1_case["registry"]["rules"][0]["predicate"] = dict(kind="required_disclosure", requirement_key="test-only-disclosure")
    fact = c1_case["facts"][0]
    observation = {k: fact[k] for k in ("document_id", "document_version", "company_id", "period_start", "period_end", "consolidation_scope", "access_scope")}
    observation.update(requirement_key="test-only-disclosure", status=status, coverage=coverage,
        review_state="accepted", reviewed_by="test-reviewer", review_note="Synthetic complete inventory only",
        financial_source_refs=[fact["source_ref"]])
    c1_case["context"]["disclosures"] = [observation]
    result = selected(C1Validator().validate(**c1_case), "disclosure", "TEST-ONLY")[0]
    assert result["outcome"] == expected
    if expected == "violation":
        assert result["financial_source_refs"] and result["legal_source_refs"]


def test_absent_fact_does_not_prove_missing_disclosure(c1_case):
    c1_case["registry"]["rules"][0]["predicate"] = dict(kind="required_disclosure", requirement_key="test-only")
    assert selected(C1Validator().validate(**c1_case), "disclosure", "TEST-ONLY")[0]["outcome"] == "unknown"


@pytest.mark.parametrize("change", ["fact_scope", "source_scope", "rule_scope", "context_scope", "bad_source_hash", "bad_finance_ref", "duplicate_id", "invalid_entity", "raw_missing", "empty_id"])
def test_untrusted_inputs_are_rejected(c1_case, change):
    if change == "fact_scope":
        c1_case["facts"][0]["access_scope"] = "tenant-b"
    elif change == "source_scope":
        next(iter(c1_case["financial_sources"].values()))["access_scope"] = "tenant-b"
    elif change == "rule_scope":
        c1_case["registry"]["rules"][0]["access_scope"] = "tenant-b"
    elif change == "context_scope":
        c1_case["context"]["access_scope"] = "tenant-b"
    elif change == "bad_source_hash":
        c1_case["registry"]["sources"][0]["text"] = "Changed"
    elif change == "bad_finance_ref":
        c1_case["facts"][0]["source_ref"] = "made-up"
    elif change == "duplicate_id":
        c1_case["facts"].append(c1_case["facts"][0])
    elif change == "invalid_entity":
        c1_case["context"]["entity_attributes"] = {"AAA": {"industry": ["not text"]}}
    elif change == "raw_missing":
        fact = c1_case["facts"][0]
        fact["raw_value"] = None
        c1_case["financial_sources"][fact["source_ref"]]["text"] = None
    elif change == "empty_id":
        c1_case["facts"][0]["fact_id"] = ""
    with pytest.raises((PermissionError, C1InputError)):
        C1Validator().validate(**c1_case)


def test_results_are_deterministic(c1_case):
    assert C1Validator().validate(**c1_case) == C1Validator().validate(**c1_case)


def test_cross_document_comparison_and_accounting_basis(c1_case):
    original = deepcopy(c1_case["facts"][2])
    second = dict(original, document_id="DOC-B", source_ref="DOC-B/v1/p1/table/cell", fact_id="other", value="901", raw_value="901")
    c1_case["facts"].append(second)
    c1_case["financial_sources"][second["source_ref"]] = dict(document_id="DOC-B", document_version="v1", access_scope="tenant-a", text="901")
    result = selected(C1Validator().validate(**c1_case), "cross_document", "same_metric")
    assert result[0]["outcome"] == "warning"
    second["accounting_basis"] = original["accounting_basis"] = None
    c1_case["facts"][2]["accounting_basis"] = None
    assert selected(C1Validator().validate(**c1_case), "cross_document", "same_metric")[0]["outcome"] == "unknown"


def test_instant_balance_sheet_identity(c1_case):
    facts = []
    for index, (metric, value) in enumerate((("assets_total", "100"), ("liabilities_total", "60"), ("equity_total", "40"))):
        fact = dict(c1_case["facts"][index], metric_code=metric, value=value, raw_value=value,
                    period_kind="instant", period_start=None)
        c1_case["financial_sources"][fact["source_ref"]]["text"] = value
        facts.append(fact)
    c1_case["facts"] = facts
    assert selected(C1Validator().validate(**c1_case), "accounting", "balance_sheet")[0]["outcome"] == "pass"
