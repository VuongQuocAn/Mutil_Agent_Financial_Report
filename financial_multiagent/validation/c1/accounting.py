"""Deterministic accounting and cross-document checks, without legal findings."""
from collections import defaultdict
from decimal import Decimal, localcontext
from importlib.resources import files
import json

from .contracts import exact_number, fact_ready_reason, group_key, make_result


def accounting_definitions():
    return json.loads(files("financial_multiagent.validation.c1").joinpath("profiles/accounting_checks.json").read_text(encoding="utf-8"))


def _precision(facts):
    return max([50] + [len(str(f.get("value") or "")) * 2 + 20 for f in facts])


class FinancialConsistencyValidator:
    def check(self, facts, profile):
        groups = defaultdict(list)
        for fact in facts:
            groups[group_key(fact)].append(fact)
        results = []
        for group, members in sorted(groups.items(), key=lambda item: str(item[0])):
            by_metric = defaultdict(list)
            for fact in members:
                by_metric[fact.get("metric_code")].append(fact)
            for definition in accounting_definitions()["checks"]:
                metrics = [definition["lhs"], *definition["terms"]]
                # Coverage for absent statement families is reported by the engine.
                if not any(m in by_metric for m in metrics):
                    continue
                selected = [f for m in metrics for f in by_metric[m]]
                missing = [m for m in metrics if not by_metric[m]]
                duplicates = [m for m in metrics if len(by_metric[m]) > 1]
                reason = ("MISSING_METRICS" if missing else "AMBIGUOUS_METRICS" if duplicates
                          else next((why for f in selected if (why := fact_ready_reason(f))), None))
                details = dict(group=list(group), missing_metrics=missing, duplicate_metrics=duplicates,
                               lhs=definition["lhs"], terms=definition["terms"], tolerance=profile.absolute_tolerance)
                if reason:
                    if not missing and not duplicates and group[6] and all(f.get("value") is not None for f in selected):
                        with localcontext() as ctx:
                            ctx.prec = _precision(selected)
                            actual = exact_number(by_metric[definition["lhs"]][0]["value"])
                            expected = sum((exact_number(by_metric[m][0]["value"]) * exact_number(c)
                                            for m, c in definition["terms"].items()), Decimal(0))
                            residual = actual - expected
                        details["numeric_diagnostic"] = dict(actual=format(actual, "f"), expected=format(expected, "f"),
                            residual=format(residual, "f"), trusted_for_validation=False)
                    results.append(make_result("accounting", definition["check_id"], "unknown", reason, selected, details=details))
                    continue
                with localcontext() as ctx:
                    ctx.prec = _precision(selected)
                    left = exact_number(by_metric[definition["lhs"]][0]["value"])
                    right = sum((exact_number(by_metric[m][0]["value"]) * exact_number(c)
                                 for m, c in definition["terms"].items()), Decimal(0))
                    residual = left - right
                    matched = abs(residual) <= exact_number(profile.absolute_tolerance)
                details.update(actual=format(left, "f"), expected=format(right, "f"), residual=format(residual, "f"))
                results.append(make_result("accounting", definition["check_id"], "pass" if matched else "warning",
                    "ACCOUNTING_IDENTITY_MATCH" if matched else "ACCOUNTING_DISCREPANCY", selected, details=details))
        return results


def cross_document_checks(facts, profile):
    groups = defaultdict(list)
    for fact in facts:
        key = tuple(fact.get(k) for k in ("company_id", "period_start", "period_end", "consolidation_scope",
                                         "unit", "ontology_version", "accounting_basis", "metric_code", "period_kind"))
        groups[key].append(fact)
    results = []
    for key, members in sorted(groups.items(), key=lambda item: str(item[0])):
        documents = {f["document_id"] for f in members}
        if len(documents) < 2:
            continue
        reason = next((why for f in members if (why := fact_ready_reason(f))), None)
        if not key[6]:
            reason = "UNKNOWN_ACCOUNTING_BASIS"
        if len(members) != len(documents):
            reason = "AMBIGUOUS_DOCUMENT_VERSION_OR_FACT"
        if reason:
            results.append(make_result("cross_document", "same_metric", "unknown", reason, members))
            continue
        with localcontext() as ctx:
            ctx.prec = _precision(members)
            values = [exact_number(f["value"]) for f in members]
            spread = max(values) - min(values)
            matched = spread <= exact_number(profile.absolute_tolerance)
        results.append(make_result("cross_document", "same_metric", "pass" if matched else "warning",
            "CROSS_DOCUMENT_MATCH" if matched else "CROSS_DOCUMENT_DISCREPANCY", members,
            details=dict(group=list(key), spread=format(spread, "f"), tolerance=profile.absolute_tolerance)))
    return results
