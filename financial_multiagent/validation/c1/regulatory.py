"""Curated rule evaluation with explicit applicability, exceptions and provenance.

No statutes or obligations are embedded here. The caller supplies reviewed
versioned rules and stored legal text; absent/ambiguous evidence stays unknown.
"""
from collections import defaultdict

from financial_multiagent.common.artifacts import sha256_bytes
from .contracts import C1InputError, exact_number, fact_ready_reason, iso_date, make_result

FIELDS = {"company_id", "consolidation_scope", "accounting_basis", "entity_type", "industry"}


def validate_registry(registry, scope):
    if not isinstance(registry, dict) or registry.get("access_scope") != scope:
        raise PermissionError("Registry outside caller scope")
    if not isinstance(registry.get("rules"), list) or not isinstance(registry.get("sources"), list):
        raise C1InputError("Registry requires rules and sources arrays")
    sources, seen = {}, set()
    for source in registry["sources"]:
        if not isinstance(source, dict) or source.get("access_scope") != scope:
            raise PermissionError("Legal source outside caller scope")
        for key in ("document_id", "document_version", "article_ref", "source_ref", "text", "text_sha256", "source_uri"):
            if not isinstance(source.get(key), str) or not source[key].strip():
                raise C1InputError("Legal source requires versioned text and source identity")
        expected = f"{source['document_id']}/{source['document_version']}/{source['article_ref']}"
        if (source["source_ref"] != expected or source["source_ref"] in sources
                or any(p in {"", ".", ".."} for p in source["source_ref"].split("/"))
                or "\\" in source["source_ref"]
                or sha256_bytes(source["text"].encode("utf-8")) != source["text_sha256"]):
            raise C1InputError("Legal source hash/ref mismatch")
        sources[source["source_ref"]] = source
    for rule in registry["rules"]:
        if not isinstance(rule, dict) or rule.get("access_scope") != scope:
            raise PermissionError("Rule outside caller scope")
        if any(not isinstance(rule.get(k), str) or not rule[k].strip() for k in ("rule_id", "rule_version")):
            raise C1InputError("Rule requires an ID and version")
        key = rule["rule_id"], rule["rule_version"]
        if key in seen:
            raise C1InputError("Duplicate rule version")
        seen.add(key)
    return sources


def _conditions(conditions, metadata):
    if not isinstance(conditions, list):
        return None
    results = []
    for condition in conditions:
        if (not isinstance(condition, dict) or condition.get("field") not in FIELDS
                or not isinstance(condition.get("values"), list) or not condition["values"]
                or not all(isinstance(v, str) for v in condition["values"])):
            return None
        actual = metadata.get(condition["field"])
        results.append(None if actual is None else actual in condition["values"])
    if False in results:
        return False
    return None if None in results else True


def applicability(rule, metadata):
    if not all(metadata.get(k) for k in ("company_id", "period_start", "period_end", "consolidation_scope")):
        return "unknown", "INCOMPLETE_VALIDATION_CONTEXT"
    if rule.get("review_state") != "accepted":
        return "unknown", "RULE_NOT_REVIEWED"
    try:
        start, end = iso_date(metadata["period_start"]), iso_date(metadata["period_end"])
        effective_from = iso_date(rule.get("effective_from"))
        effective_to = iso_date(rule["effective_to"]) if rule.get("effective_to") is not None else None
    except C1InputError:
        return "unknown", "UNKNOWN_EFFECTIVE_DATES"
    if start > end:
        return "unknown", "INVALID_VALIDATION_PERIOD"
    if effective_to is None and rule.get("open_ended") is not True:
        return "unknown", "UNKNOWN_EFFECTIVE_END"
    if (effective_to and effective_from > effective_to) or (effective_to and rule.get("open_ended") is True):
        return "unknown", "CONFLICTING_EFFECTIVE_DATES"
    if end < effective_from or (effective_to and start > effective_to):
        return "not_applicable", "OUTSIDE_EFFECTIVE_PERIOD"
    if start < effective_from or (effective_to and end > effective_to):
        return "unknown", "RULE_CHANGES_WITHIN_PERIOD"
    matched = _conditions(rule.get("applicability"), metadata)
    if matched is False:
        return "not_applicable", "APPLICABILITY_MISMATCH"
    if matched is None:
        return "unknown", "APPLICABILITY_UNDETERMINED"
    if not isinstance(rule.get("exceptions"), list):
        return "unknown", "EXCEPTIONS_UNSPECIFIED"
    exceptions = [_conditions(e.get("conditions"), metadata) if isinstance(e, dict) else None
                  for e in rule["exceptions"]]
    if True in exceptions:
        return "not_applicable", "RULE_EXCEPTION_APPLIES"
    if None in exceptions:
        return "unknown", "EXCEPTION_UNDETERMINED"
    return "applicable", "APPLICABILITY_CONFIRMED"


def _source_reason(rule, sources, profile):
    refs = rule.get("legal_source_refs")
    if not isinstance(refs, list) or not refs or not all(isinstance(r, str) and r in sources for r in refs):
        return "MISSING_LEGAL_PROVENANCE"
    for ref in refs:
        source = sources[ref]
        if source.get("review_state") != "accepted":
            return "LEGAL_SOURCE_NOT_REVIEWED"
        if source.get("source_kind") == "synthetic":
            if not profile.allow_synthetic_rules:
                return "SYNTHETIC_RULE_DISABLED"
        elif source.get("source_kind") != "legal_text":
            return "UNSUPPORTED_LEGAL_SOURCE_KIND"
    return None


def regulatory_checks(facts, registry, sources, financial_sources, profile, context):
    if not registry["rules"]:
        return [make_result("regulatory", "rule_coverage", "unknown", "NO_REVIEWED_RULE_REGISTRY")]
    groups = defaultdict(list)
    for fact in facts:
        key = tuple(fact.get(k) for k in ("document_id", "document_version", "company_id",
                                         "period_start", "period_end", "consolidation_scope", "accounting_basis"))
        groups[key].append(fact)
    results = []
    for key, members in sorted(groups.items(), key=lambda item: str(item[0])):
        metadata = dict(zip(("document_id", "document_version", "company_id", "period_start", "period_end",
                            "consolidation_scope", "accounting_basis"), key))
        attributes = context.get("entity_attributes", {}).get(metadata["company_id"], {})
        # Caller entity attributes cannot overwrite document-derived validation context.
        metadata.update({k: attributes[k] for k in ("entity_type", "industry") if k in attributes})
        states = [applicability(rule, metadata) for rule in registry["rules"]]
        active = defaultdict(int)
        for rule, (state, _) in zip(registry["rules"], states):
            if state == "applicable":
                active[rule["rule_id"]] += 1
        for rule, (state, reason) in zip(registry["rules"], states):
            predicate = rule.get("predicate", {})
            predicate = predicate if isinstance(predicate, dict) else {}
            category = "disclosure" if predicate.get("kind") == "required_disclosure" else "regulatory"
            legal_refs = [r for r in rule.get("legal_source_refs", []) if isinstance(r, str) and r in sources] if isinstance(rule.get("legal_source_refs"), list) else []
            details = dict(validation_context=metadata, synthetic=any(sources[r].get("source_kind") == "synthetic" for r in legal_refs))
            source_reason = _source_reason(rule, sources, profile)
            if source_reason:
                state, reason = "unknown", source_reason
            elif state == "applicable" and active[rule["rule_id"]] > 1:
                state, reason = "unknown", "OVERLAPPING_RULE_VERSIONS"
            if state != "applicable":
                results.append(make_result(category, rule["rule_id"], state, reason, members,
                    rule=rule, legal_refs=legal_refs, details=details, applicability=state))
                continue
            if predicate.get("kind") == "metric_comparison":
                selected = [f for f in members if f.get("metric_code") == predicate.get("metric_code")]
                reason = "MISSING_OR_AMBIGUOUS_METRIC" if len(selected) != 1 else fact_ready_reason(selected[0])
                if not reason and (not rule.get("ontology_version") or selected[0].get("ontology_version") != rule["ontology_version"]):
                    reason = "ONTOLOGY_NOT_APPROVED_FOR_RULE"
                if not reason and selected[0].get("unit") != predicate.get("unit"):
                    reason = "UNIT_MISMATCH"
                operator = predicate.get("operator")
                if operator not in {"eq", "ge", "gt", "le", "lt"}:
                    reason = "UNSUPPORTED_PREDICATE_OPERATOR"
                try:
                    threshold = exact_number(predicate.get("threshold"))
                except C1InputError:
                    reason = "INVALID_RULE_THRESHOLD"
                if reason:
                    results.append(make_result(category, rule["rule_id"], "unknown", reason, selected,
                        rule=rule, legal_refs=legal_refs, details=details, applicability=state))
                    continue
                value = exact_number(selected[0]["value"])
                comparisons = {"eq": value == threshold, "ge": value >= threshold, "gt": value > threshold,
                               "le": value <= threshold, "lt": value < threshold}
                passed = comparisons[operator]
                details.update(actual=str(value), threshold=str(threshold), operator=operator, unit=predicate["unit"])
                results.append(make_result(category, rule["rule_id"], "pass" if passed else "violation",
                    "RULE_SATISFIED" if passed else "APPLICABLE_RULE_NOT_SATISFIED", selected,
                    rule=rule, legal_refs=legal_refs, details=details, applicability=state))
            elif predicate.get("kind") == "required_disclosure":
                if not isinstance(predicate.get("requirement_key"), str) or not predicate["requirement_key"].strip():
                    results.append(make_result(category, rule["rule_id"], "unknown", "INVALID_DISCLOSURE_KEY", members,
                        rule=rule, legal_refs=legal_refs, details=details, applicability=state))
                    continue
                observations = [o for o in context.get("disclosures", [])
                    if isinstance(o, dict) and o.get("requirement_key") == predicate.get("requirement_key")
                    and all(o.get(k) == metadata[k] for k in ("document_id", "document_version", "company_id",
                                                            "period_start", "period_end", "consolidation_scope"))]
                outcome, reason, refs = "unknown", "NO_COMPLETE_DISCLOSURE_EVIDENCE", []
                if len(observations) == 1:
                    obs = observations[0]
                    candidate_refs = obs.get("financial_source_refs", [])
                    valid_refs = (isinstance(candidate_refs, list) and bool(candidate_refs)
                        and all(isinstance(r, str) and r in financial_sources
                            and all(financial_sources[r].get(k) == metadata[k] for k in ("document_id", "document_version"))
                            for r in candidate_refs))
                    if (valid_refs and obs.get("review_state") == "accepted"
                            and isinstance(obs.get("reviewed_by"), str) and obs["reviewed_by"].strip()
                            and isinstance(obs.get("review_note"), str) and obs["review_note"].strip()
                            and obs.get("coverage") == "complete_reviewed"):
                        refs = candidate_refs
                        if obs.get("status") in {"present", "absent"}:
                            outcome = "pass" if obs["status"] == "present" else "violation"
                            reason = "DISCLOSURE_PRESENT" if outcome == "pass" else "REVIEWED_DISCLOSURE_ABSENCE"
                results.append(make_result(category, rule["rule_id"], outcome, reason,
                    rule=rule, legal_refs=legal_refs, financial_refs=refs, details=details, applicability=state))
            else:
                results.append(make_result(category, rule["rule_id"], "unknown", "UNSUPPORTED_RULE_PREDICATE", members,
                    rule=rule, legal_refs=legal_refs, details=details, applicability=state))
    return results
