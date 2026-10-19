"""Pure C1 orchestration; no filesystem writes or model calls."""
from collections import Counter, defaultdict

from .accounting import FinancialConsistencyValidator, cross_document_checks
from .contracts import C1InputError, C1Profile, fact_ready_reason, make_result, validate_financial_sources
from .regulatory import regulatory_checks, validate_registry


class C1Validator:
    def validate(self, facts, *, access_scope, financial_sources, registry=None, context=None, profile=None):
        if not isinstance(access_scope, str) or not access_scope.strip():
            raise PermissionError("Explicit C1 access scope required")
        if not facts:
            raise C1InputError("C1 requires facts")
        profile = profile or C1Profile()
        registry = registry if registry is not None else dict(access_scope=access_scope, rules=[], sources=[])
        context = context if context is not None else dict(access_scope=access_scope, entity_attributes={}, disclosures=[])
        if not isinstance(context, dict) or context.get("access_scope") != access_scope:
            raise PermissionError("Validation context outside caller scope")
        if not isinstance(context.get("entity_attributes", {}), dict) or not isinstance(context.get("disclosures", []), list):
            raise C1InputError("Invalid validation context")
        if any(not isinstance(v, dict) for v in context.get("entity_attributes", {}).values()):
            raise C1InputError("Entity attributes must be objects")
        if any(k not in {"entity_type", "industry"} or not isinstance(value, str) or not value.strip()
               for attributes in context.get("entity_attributes", {}).values() for k, value in attributes.items()):
            raise C1InputError("Only explicit text entity_type/industry attributes are supported")
        if any(not isinstance(s, dict) or s.get("access_scope") != access_scope for s in financial_sources.values()):
            raise PermissionError("Source catalog outside caller scope")
        if any(not isinstance(o, dict) or o.get("access_scope") != access_scope for o in context.get("disclosures", [])):
            raise PermissionError("Disclosure observation outside caller scope")
        sources = validate_registry(registry, access_scope)
        validate_financial_sources(facts, financial_sources, access_scope)
        results = []
        documents = defaultdict(list)
        for fact in facts:
            documents[fact["document_id"], fact["document_version"]].append(fact)
        for doc, members in sorted(documents.items()):
            incomplete = Counter(why for f in members if (why := fact_ready_reason(f)))
            results.append(make_result("structural", "normalized_input_context", "unknown" if incomplete else "pass",
                "INCOMPLETE_OR_REVIEW_INPUT" if incomplete else "NORMALIZED_CONTEXT_PRESENT", members,
                details=dict(document_id=doc[0], document_version=doc[1], incomplete_reasons=dict(incomplete))))
        results.extend(FinancialConsistencyValidator().check(facts, profile))
        cross = cross_document_checks(facts, profile)
        results.extend(cross or [make_result("cross_document", "coverage", "unknown", "NO_COMPARABLE_DOCUMENTS")])
        legal = regulatory_checks(facts, registry, sources, financial_sources, profile, context)
        results.extend(legal)
        if not any(r["category"] == "disclosure" for r in legal):
            results.append(make_result("disclosure", "coverage", "unknown", "NO_EXECUTABLE_DISCLOSURE_RULE"))
        metrics = {f.get("metric_code") for f in facts}
        for family, trigger in (("balance_sheet", "assets_total"), ("cash_flow", "cash_closing")):
            if trigger not in metrics:
                results.append(make_result("coverage", family, "unknown", "STATEMENT_NOT_AVAILABLE_IN_SELECTED_INPUT"))
        return dict(results=results, outcome_counts=dict(sorted(Counter(r["outcome"] for r in results).items())),
            quality_status="needs_review" if any(r["outcome"] in {"unknown", "warning", "violation"} for r in results) else "checks_completed",
            legal_status="unknown" if any(r["category"] in {"regulatory", "disclosure"} and r["outcome"] == "unknown" for r in results)
                         else "findings_present" if any(r["outcome"] == "violation" for r in results) else "evaluated_supplied_rules")
