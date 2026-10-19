"""Synthetic accounting and rule fixtures. No real legal obligations."""
from copy import deepcopy
import pytest

from financial_multiagent.common.artifacts import sha256_bytes
from financial_multiagent.validation.c1.contracts import C1Profile


@pytest.fixture
def c1_case():
    values = dict(revenue_gross="1000", revenue_deductions="100", revenue_net="900", cost_of_sales="600",
        gross_profit="300", finance_income="10", finance_cost="20", selling_expense="30", administrative_expense="40",
        operating_profit="220", other_income="50", other_expense="20", other_profit="30", profit_before_tax="250",
        income_tax_current="50", income_tax_deferred="0", profit_after_tax="200")
    facts, sources = [], {}
    for index, (metric, value) in enumerate(values.items()):
        ref = f"DOC-A/v1/p1/table_1/cell_{index}"
        fact = dict(fact_id=f"fact-{index}", document_id="DOC-A", document_version="v1", access_scope="tenant-a",
            source_ref=ref, raw_value=value, value=value, metric_code=metric, company_id="AAA",
            period_start="2025-01-01", period_end="2025-12-31", period_kind="duration",
            unit="VND", consolidation_scope="standalone", ontology_version="b02dn-draft-1", accounting_basis="VAS",
            quality_state="normalized_candidate", normalization_issues=[], ocr_quality_issues=[], restatement_status="not_assessed")
        facts.append(fact)
        sources[ref] = dict(document_id="DOC-A", document_version="v1", access_scope="tenant-a", text=value)
    text = "SYNTHETIC TEST TEXT ONLY. Fictional numerical predicate, not a real regulation."
    source = dict(document_id="TEST-LAW", document_version="v1", article_ref="article-test",
        source_ref="TEST-LAW/v1/article-test", text=text, text_sha256=sha256_bytes(text.encode()),
        source_uri="urn:synthetic:test-only", source_kind="synthetic", review_state="accepted", access_scope="tenant-a")
    rule = dict(rule_id="TEST-ONLY", rule_version="1", access_scope="tenant-a", review_state="accepted",
        effective_from="2020-01-01", effective_to=None, open_ended=True,
        applicability=[dict(field="company_id", values=["AAA"])], exceptions=[],
        legal_source_refs=[source["source_ref"]], ontology_version="b02dn-draft-1",
        predicate=dict(kind="metric_comparison", metric_code="revenue_net", operator="ge", threshold="100", unit="VND"))
    return dict(facts=facts, financial_sources=sources, access_scope="tenant-a",
                registry=dict(access_scope="tenant-a", rules=[rule], sources=[source]),
                context=dict(access_scope="tenant-a", entity_attributes={}, disclosures=[]),
                profile=C1Profile(allow_synthetic_rules=True))
