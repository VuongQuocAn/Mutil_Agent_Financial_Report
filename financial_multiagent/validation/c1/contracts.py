"""Validated C1 contracts; result construction enforces dual provenance."""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from financial_multiagent.common.artifacts import content_hash

VERSION = "1.0.0"
OUTCOMES = {"pass", "warning", "unknown", "not_applicable", "violation"}


class C1InputError(ValueError):
    pass


def exact_number(value):
    if not isinstance(value, str):
        raise C1InputError("Numbers must be decimal strings")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise C1InputError("Invalid decimal") from exc
    if not result.is_finite():
        raise C1InputError("Nonfinite decimal")
    return result


def iso_date(value):
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise C1InputError("Invalid date") from exc


@dataclass(frozen=True)
class C1Profile:
    profile_id: str = "c1-default"
    profile_version: str = "1"
    absolute_tolerance: str = "0"
    allow_synthetic_rules: bool = False

    def __post_init__(self):
        if not self.profile_id or not self.profile_version or exact_number(self.absolute_tolerance) < 0:
            raise C1InputError("Invalid profile or tolerance")
        if type(self.allow_synthetic_rules) is not bool:
            raise C1InputError("Synthetic flag must be boolean")


def fact_ready_reason(fact):
    if fact.get("period_kind") not in {"duration", "instant"}:
        return "UNKNOWN_PERIOD_KIND"
    required = ("company_id", "period_end", "consolidation_scope", "unit", "metric_code")
    if fact["period_kind"] == "duration":
        required += ("period_start",)
    for key in required:
        if not fact.get(key):
            return "MISSING_" + key.upper()
    if fact.get("period_start") and iso_date(fact["period_start"]) > iso_date(fact["period_end"]):
        return "INVALID_PERIOD"
    if fact.get("value") is None:
        return "MISSING_VALUE"
    exact_number(fact["value"])
    if (fact.get("quality_state") not in {"normalized_candidate", "accepted"} or fact.get("normalization_issues")
            or fact.get("ocr_quality_issues")):
        return "FACT_NEEDS_REVIEW"
    if fact.get("restatement_status") not in {"not_assessed", "resolved"}:
        return "UNRESOLVED_RESTATEMENT"
    return None


def group_key(fact):
    # Never combine documents, companies, units, scopes, periods or ontologies.
    return tuple(fact.get(k) for k in ("document_id", "document_version", "company_id",
                 "period_start", "period_end", "consolidation_scope", "unit", "ontology_version", "accounting_basis", "period_kind"))


def make_result(category, name, outcome, reason, facts=(), *, rule=None, legal_refs=(),
                financial_refs=None, details=None, applicability=None):
    if outcome not in OUTCOMES:
        raise C1InputError("Unknown outcome")
    refs = sorted(set(financial_refs if financial_refs is not None else [f["source_ref"] for f in facts]))
    legal = sorted(set(legal_refs))
    if outcome == "violation" and (category not in {"regulatory", "disclosure"} or not refs or not legal
            or not rule or not rule.get("rule_id") or not rule.get("rule_version") or applicability != "applicable"):
        raise C1InputError("Violation requires an applicable versioned rule and both provenance sets")
    value: dict[str, Any] = dict(category=category, name=name, outcome=outcome, reason=reason,
        fact_ids=sorted(f["fact_id"] for f in facts), financial_source_refs=refs,
        legal_source_refs=legal, rule_id=rule.get("rule_id") if rule else None,
        rule_version=rule.get("rule_version") if rule else None, applicability=applicability,
        details=details or {}, verification_stage="offline_c1")
    value["check_id"] = content_hash(value)[:24]
    return value


def validate_financial_sources(facts, catalog, scope):
    seen = set()
    for fact in facts:
        if not isinstance(fact, dict) or fact.get("access_scope") != scope:
            raise PermissionError("Fact outside caller scope")
        if not isinstance(fact.get("fact_id"), str) or not fact["fact_id"].strip() or fact["fact_id"] in seen:
            raise C1InputError("Invalid or duplicate fact ID")
        for field in ("document_id", "document_version", "company_id", "period_start", "period_end",
                      "consolidation_scope", "unit", "metric_code", "ontology_version", "accounting_basis", "period_kind"):
            if fact.get(field) is not None and not isinstance(fact[field], str):
                raise C1InputError("Fact context fields must be text or null")
        seen.add(fact["fact_id"])
        ref = fact.get("source_ref")
        source = catalog.get(ref) if isinstance(ref, str) else None
        if not source or source.get("access_scope") != scope:
            raise C1InputError("Financial source ref is not in the authorized source catalog")
        if not isinstance(fact.get("raw_value"), str) or not isinstance(source.get("text"), str):
            raise C1InputError("Financial fact must resolve to actual text evidence")
        if (any(fact.get(k) != source.get(k) for k in ("document_id", "document_version"))
                or fact.get("raw_value") != source.get("text")):
            raise C1InputError("Fact disagrees with its source record")
        for key in ("period_start", "period_end"):
            if fact.get(key) is not None:
                iso_date(fact[key])
        if fact.get("value") is not None:
            exact_number(fact["value"])
