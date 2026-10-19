"""Normalize one B02-style income statement into provenance-bearing candidates.

The mapping is a versioned internal draft, not a VAS/IFRS crosswalk or legal
validator. Ambiguous metadata and OCR warnings remain explicit on each fact.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, localcontext
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import unicodedata
from typing import Any
from uuid import uuid4

from .ocr_quality import OCRQualityValidator, VALIDATOR_VERSION as QUALITY_VALIDATOR_VERSION

NORMALIZER_VERSION = "1.0.0"
ONTOLOGY_VERSION = "b02dn-draft-1"
PLACEHOLDERS = {"", "-", "–", "—", "_", "…"}


class NormalizationError(ValueError):
    """Input contract or table layout is unsupported or ambiguous."""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _fold(value: str) -> str:
    text = unicodedata.normalize("NFD", value.lower().replace("đ", "d"))
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return " ".join(text.split())


def parse_financial_number(raw: str, *, locale: str = "vi_VN") -> str | None:
    """Return an exact decimal string; placeholders remain missing, never zero."""
    if locale not in {"vi_VN", "en_US"}:
        raise NormalizationError("Explicit supported number locale required")
    if not isinstance(raw, str):
        raise NormalizationError("Raw value must be text")
    text = raw.strip().replace("\u00a0", " ").replace("\u202f", " ")
    if text in PLACEHOLDERS:
        return None
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1].strip()
        if text.startswith(("-", "+")):
            raise NormalizationError("Conflicting numeric signs")
    elif text.startswith(("-", "+")):
        negative = text[0] == "-"
        text = text[1:]
    grouping, decimal = (".", ",") if locale == "vi_VN" else (",", ".")
    pieces = text.split(decimal)
    if len(pieces) > 2 or not pieces[0]:
        raise NormalizationError("Invalid numeric separators")
    whole = pieces[0]
    group = " " if " " in whole else grouping
    if " " in whole and grouping in whole:
        raise NormalizationError("Mixed grouping separators")
    if group in whole:
        if not re.fullmatch(r"[0-9]{1,3}(?:" + re.escape(group) + r"[0-9]{3})+", whole):
            raise NormalizationError("Invalid thousands grouping")
        whole = whole.replace(group, "")
    elif not re.fullmatch(r"[0-9]+", whole):
        raise NormalizationError("Unrecognized numeric characters")
    fraction = pieces[1] if len(pieces) == 2 else None
    if fraction is not None and not re.fullmatch(r"[0-9]+", fraction):
        raise NormalizationError("Invalid decimal fraction")
    exact = ("-" if negative else "") + whole + ("." + fraction if fraction is not None else "")
    result = Decimal(exact)
    return "0" if result == 0 else format(result, "f")


# Both statement code AND row label must corroborate a mapping.
# Per-share values require an explicit dimensional unit, not the table multiplier.
METRICS = {
    "01": ("revenue_gross", ("doanh thu", "ban hang"), "money"),
    "02": ("revenue_deductions", ("giam tru", "doanh thu"), "money"),
    "10": ("revenue_net", ("doanh thu thuan",), "money"),
    "11": ("cost_of_sales", ("gia von",), "money"),
    "20": ("gross_profit", ("gop",), "money"),
    "21": ("finance_income", ("doanh thu", "tai chinh"), "money"),
    "22": ("finance_cost", ("chi phi", "tai chinh"), "money"),
    "23": ("interest_expense", ("lai vay",), "money"),
    "25": ("selling_expense", ("chi phi ban hang",), "money"),
    "26": ("administrative_expense", ("quan ly doanh nghiep",), "money"),
    "30": ("operating_profit", ("thuan", "hoat dong kinh doanh"), "money"),
    "31": ("other_income", ("thu nhap khac",), "money"),
    "32": ("other_expense", ("chi phi khac",), "money"),
    "40": ("other_profit", ("loi nhuan khac",), "money"),
    "50": ("profit_before_tax", ("ke toan truoc thue",), "money"),
    "51": ("income_tax_current", ("thue thu nhap", "hien hanh"), "money"),
    "52": ("income_tax_deferred", ("thue thu nhap", "hoan lai"), "money"),
    "60": ("profit_after_tax", ("sau thue",), "money"),
    "70": ("earnings_per_share_basic", ("co ban", "co phieu"), "per_share"),
    "71": ("earnings_per_share_diluted", ("suy giam", "co phieu"), "per_share"),
}


@dataclass(frozen=True)
class NormalizationContext:
    company_id: str | None = None
    company_evidence: str | None = None
    consolidation_scope: str | None = None
    scope_evidence: str | None = None
    # Explicit user-confirmed column periods, keyed by zero-based column.
    # Each requires start, end and evidence. No January-1 assumption.
    period_overrides: dict[int, dict[str, str]] = field(default_factory=dict)
    # Values supported for money: VND, thousand_VND, million_VND, billion_VND.
    unit_override: str | None = None
    unit_evidence: str | None = None
    eps_unit: str | None = None
    eps_unit_evidence: str | None = None
    accounting_basis: str | None = None
    basis_evidence: str | None = None
    locale: str = "vi_VN"

    def __post_init__(self) -> None:
        if self.locale not in {"vi_VN", "en_US"}:
            raise NormalizationError("Unsupported number locale")
        if self.consolidation_scope not in {None, "consolidated", "standalone"}:
            raise NormalizationError("Unsupported consolidation scope")
        if self.accounting_basis not in {None, "VAS", "IFRS"}:
            raise NormalizationError("Unknown accounting basis")
        for value, evidence in ((self.company_id, self.company_evidence),
                                (self.consolidation_scope, self.scope_evidence),
                                (self.unit_override, self.unit_evidence),
                                (self.eps_unit, self.eps_unit_evidence),
                                (self.accounting_basis, self.basis_evidence)):
            if value is not None and (not isinstance(value, str) or not value.strip()
                                      or not isinstance(evidence, str) or not evidence.strip()):
                raise NormalizationError("Explicit metadata requires an evidence note")
        if self.unit_override is not None and self.unit_override not in UNITS:
            raise NormalizationError("Unsupported money unit")
        if self.eps_unit not in {None, "VND/share"}:
            raise NormalizationError("Unsupported EPS unit")
        for col, period in self.period_overrides.items():
            if type(col) is not int or col < 0 or not isinstance(period, dict):
                raise NormalizationError("Column period override must have integer column and object value")
            try:
                start, end = date.fromisoformat(period["start"]), date.fromisoformat(period["end"])
                if start > end or not period["evidence"].strip():
                    raise ValueError()
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                raise NormalizationError("Period override requires valid start/end and evidence") from exc


UNITS = {"VND": 1, "thousand_VND": 1000, "million_VND": 1000000, "billion_VND": 1000000000}


def _unit(text: str) -> str | None:
    folded = _fold(text)
    if not re.search(r"\bdon vi\b", folded) or not re.search(r"\b(vnd|dong)\b", folded):
        return None
    if re.search(r"\bty\b", folded):
        return "billion_VND"
    if re.search(r"\btrieu\b", folded):
        return "million_VND"
    if re.search(r"\b(nghin|ngan)\b", folded):
        return "thousand_VND"
    # Unknown multipliers must not silently become base VND.
    if re.fullmatch(r"don vi(?: tinh)?\s*:?\s*(?:vnd|dong)\s*", folded):
        return "VND"
    return None


def _context_from_page(records: list[dict], table: dict) -> dict:
    blocks = [r for r in records if r["page"] == table["page"] and r["kind"] == "ocr_block"
              and r["bbox"][3] <= table["bbox"][1]]
    units, end_dates, scopes, restatement_refs = [], [], [], []
    for r in blocks:
        text = _fold(r["text"])
        if "don vi" in text:
            units.append((_unit(r["text"]), r["source_ref"], r["text"]))
        match = re.search(r"cho nam tai chinh ket thuc ngay (\d{1,2}) thang (\d{1,2}) nam (\d{4})", text)
        if match:
            try:
                end = date(int(match[3]), int(match[2]), int(match[1]))
                end_dates.append((end, r["source_ref"]))
            except ValueError:
                pass
        if "bao cao" in text:
            if "hop nhat" in text:
                scopes.append(("consolidated", r["source_ref"]))
            if re.search(r"\brieng\b", text):
                scopes.append(("standalone", r["source_ref"]))
        if "dieu chinh" in text or "hoi to" in text:
            restatement_refs.append(r["source_ref"])
    return dict(units=units, end_dates=end_dates, scopes=scopes, restatement_refs=restatement_refs)


class FinancialNormalizer:
    def normalize(self, manifest: dict, records: list[dict], *, page: int, table_id: str,
                  context: NormalizationContext | None = None) -> dict[str, Any]:
        context = context or NormalizationContext()
        quality = OCRQualityValidator().assess(manifest, records)
        if quality.execution_status != "completed":
            raise NormalizationError("Adapter artifact contract failed quality validation")
        tables = [r for r in records if r["page"] == page and r["kind"] == "table"
                  and r["element_id"] == table_id]
        if len(tables) != 1:
            raise NormalizationError("Selected page/table must identify exactly one table")
        table = tables[0]
        name = table.get("name")
        if not isinstance(name, str) or "ket qua hoat dong kinh doanh" not in _fold(name):
            raise NormalizationError("Only an explicitly named income statement is supported in v1")
        cells = [r for r in records if r["page"] == page and r["kind"] == "table_cell"
                 and r["table_id"] == table_id]
        rows: dict[int, dict[int, dict]] = defaultdict(dict)
        for cell in cells:
            if cell["col"] in rows[cell["row"]]:
                raise NormalizationError("Duplicate row/col requires layout review before normalization")
            rows[cell["row"]][cell["col"]] = cell
        headers = []
        for row, entries in rows.items():
            roles = {}
            for col, c in entries.items():
                label = _fold(c["text"])
                role = {"chi tieu": "label", "ma so": "code", "nam nay": "current", "nam truoc": "previous"}.get(label)
                if role:
                    if role in roles:
                        raise NormalizationError("Ambiguous duplicated header role")
                    roles[role] = col
            if {"label", "code", "current", "previous"} <= roles.keys():
                headers.append((row, roles))
        if len(headers) != 1:
            raise NormalizationError("Expected one explicit CHI TIEU/MA SO/NAM NAY/NAM TRUOC header")
        header_row, roles = headers[0]
        if set(context.period_overrides) - {roles["current"], roles["previous"]}:
            raise NormalizationError("Period override targets a non-value column")
        inferred = _context_from_page(records, table)
        upstream = defaultdict(list)
        for issue in quality.issues:
            if issue.source_ref and issue.review_required:
                upstream[issue.source_ref].append(issue.to_dict())
        facts, issues = [], []
        for row in sorted(rows):
            if row <= header_row:
                continue
            entries = rows[row]
            label_cell, code_cell = entries.get(roles["label"]), entries.get(roles["code"])
            raw_label = label_cell["text"] if label_cell else ""
            raw_code = code_cell["text"] if code_cell else ""
            metric = METRICS.get(raw_code.strip())
            mapped = metric is not None and all(token in _fold(raw_label) for token in metric[1])
            for role in ("current", "previous"):
                column = roles[role]
                cell = entries.get(column)
                if cell is None:
                    issues.append(dict(code="MISSING_VALUE_CELL", source_ref=None,
                                       table_source_ref=table["source_ref"], row=row, col=column))
                    continue
                reasons: list[str] = []
                refs = dict(label=[label_cell["source_ref"]] if label_cell else [],
                            statement_code=[code_cell["source_ref"]] if code_cell else [],
                            column_header=[rows[header_row][column]["source_ref"]],
                            unit=[], period=[], scope=[])
                if not mapped:
                    reasons.append("UNMAPPED_OR_CONFLICTING_METRIC")
                try:
                    parsed = parse_financial_number(cell["text"], locale=context.locale)
                except NormalizationError:
                    parsed = None
                    reasons.append("INVALID_NUMBER")
                else:
                    if parsed is None:
                        reasons.append("MISSING_VALUE")
                units = {x[0] for x in inferred["units"]}
                raw_unit = next(iter(units)) if len(units) == 1 and None not in units else None
                refs["unit"] = [x[1] for x in inferred["units"]]
                if context.unit_override is not None:
                    if raw_unit is not None and raw_unit != context.unit_override:
                        reasons.append("UNIT_CONFLICT")
                        raw_unit = None
                    elif not inferred["units"]:
                        raw_unit = context.unit_override
                    elif raw_unit is None:
                        reasons.append("AMBIGUOUS_UNIT_DECLARATION")
                dimension = metric[2] if mapped else None
                unit, scale = ("VND", UNITS[raw_unit]) if raw_unit else (None, None)
                if dimension == "per_share":
                    raw_unit, unit, scale = context.eps_unit, context.eps_unit, 1 if context.eps_unit else None
                    if unit is None:
                        reasons.append("EPS_UNIT_REQUIRES_CONFIRMATION")
                elif unit is None:
                    reasons.append("UNKNOWN_UNIT")
                # Unknown dimensions must not inherit a monetary multiplier.
                if dimension is None:
                    unit, scale = None, None
                dates = {d for d, _ in inferred["end_dates"]}
                end = next(iter(dates)) if len(dates) == 1 else None
                refs["period"] = [ref for _, ref in inferred["end_dates"]]
                if role == "previous" and end:
                    try:
                        end = end.replace(year=end.year - 1)
                    except ValueError:
                        end = None
                start = None
                override = context.period_overrides.get(column)
                if override:
                    supplied_end = date.fromisoformat(override["end"])
                    if (end and supplied_end != end) or len(dates) > 1:
                        reasons.append("PERIOD_CONFLICT")
                        end = None
                    else:
                        start, end = date.fromisoformat(override["start"]), supplied_end
                if not start or not end:
                    reasons.append("INCOMPLETE_PERIOD")
                scopes = {s for s, _ in inferred["scopes"]}
                scope = next(iter(scopes)) if len(scopes) == 1 else None
                refs["scope"] = [ref for _, ref in inferred["scopes"]]
                if context.consolidation_scope:
                    if scopes and scopes != {context.consolidation_scope}:
                        scope = None
                        reasons.append("SCOPE_CONFLICT")
                    else:
                        scope = context.consolidation_scope
                if scope is None:
                    reasons.append("UNKNOWN_SCOPE")
                if context.company_id is None:
                    reasons.append("UNKNOWN_COMPANY")
                header_text = rows[header_row][column]["text"]
                restated = bool(inferred["restatement_refs"]) or "dieu chinh" in _fold(header_text)
                if restated:
                    reasons.append("RESTATEMENT_REQUIRES_REVIEW")
                # Row/header/unit/date evidence can also have OCR warnings.
                affected_refs = {cell["source_ref"], table["source_ref"]}
                affected_refs.update(ref for values in refs.values() for ref in values)
                quality_issues = [issue for ref in sorted(affected_refs) for issue in upstream[ref]]
                if quality_issues:
                    reasons.append("UPSTREAM_OCR_REVIEW")
                value = None
                if parsed is not None and scale is not None:
                    with localcontext() as decimal_context:
                        decimal_context.prec = max(50, len(parsed) + len(str(scale)) + 5)
                        value = format(Decimal(parsed) * Decimal(scale), "f")
                fact = dict(
                    document_id=manifest["document_id"], document_version=manifest["document_version"],
                    access_scope=manifest["access_scope"], company_id=context.company_id,
                    source_symbol=manifest.get("symbol"), page=page, table_id=table_id, row=row, col=column,
                    metric_code=metric[0] if mapped else None, ontology_version=ONTOLOGY_VERSION,
                    raw_label=raw_label, statement_code=raw_code, raw_value=cell["text"], parsed_value=parsed,
                    raw_unit=raw_unit, unit=unit, unit_multiplier=str(scale) if scale is not None else None,
                    value=value, fiscal_period=f"FY-ended-{end.isoformat()}" if end else None,
                    period_start=start.isoformat() if start else None, period_end=end.isoformat() if end else None,
                    period_kind="duration", column_role=role, consolidation_scope=scope,
                    accounting_basis=context.accounting_basis, restatement_status="needs_review" if restated else "not_assessed",
                    source_ref=cell["source_ref"], page_image_ref=cell["page_image_ref"], bbox=cell["bbox"],
                    context_refs=refs, metadata_evidence=asdict(context),
                    ocr_quality_issues=quality_issues, normalization_issues=sorted(set(reasons)),
                    quality_state="needs_review" if reasons else "normalized_candidate",
                )
                fact["fact_id"] = _hash(dict(source_ref=cell["source_ref"], context=asdict(context),
                                             source_hash=manifest["source_bundle_sha256"], record_hash=_hash(cell),
                                             normalizer=NORMALIZER_VERSION, ontology=ONTOLOGY_VERSION))[:24]
                facts.append(fact)
                issues.extend(dict(code=code, source_ref=cell["source_ref"], fact_id=fact["fact_id"])
                              for code in sorted(set(reasons)))
        repeated = Counter((f["metric_code"], f["column_role"]) for f in facts if f["metric_code"])
        for fact in facts:
            if fact["metric_code"] and repeated[fact["metric_code"], fact["column_role"]] > 1:
                code = "DUPLICATE_METRIC_FOR_PERIOD"
                fact["normalization_issues"] = sorted(set(fact["normalization_issues"] + [code]))
                fact["quality_state"] = "needs_review"
                issues.append(dict(code=code, source_ref=fact["source_ref"], fact_id=fact["fact_id"]))
        if not facts:
            raise NormalizationError("Selected table produced no value candidates")
        return dict(facts=facts, issues=issues, table_source_ref=table["source_ref"],
                    detected_context=dict(
                        units=[dict(unit=u, source_ref=r, raw_text=t) for u, r, t in inferred["units"]],
                        period_ends=[dict(end=d.isoformat(), source_ref=r) for d, r in inferred["end_dates"]],
                        scopes=[dict(scope=s, source_ref=r) for s, r in inferred["scopes"]]),
                    quality_status="needs_review" if issues else "normalized_candidates")


def normalize_financial_table(bundle_dir: Path | str, *, page: int, table_id: str,
                              access_scope: str, context: NormalizationContext | None = None,
                              output_dir: Path | str | None = None) -> dict:
    """Write a complete versioned run for ONE explicitly selected table."""
    bundle = Path(bundle_dir)
    raw_manifest = (bundle / "manifest.json").read_bytes()
    manifest = json.loads(raw_manifest)
    if not access_scope.strip() or not isinstance(manifest, dict) or manifest.get("access_scope") != access_scope:
        raise PermissionError("Manifest is outside supplied access scope")
    raw_records = (bundle / "elements.jsonl").read_bytes()
    records = [json.loads(s) for s in raw_records.decode("utf-8").splitlines() if s.strip()]
    context = context or NormalizationContext()
    result = FinancialNormalizer().normalize(manifest, records, page=page, table_id=table_id, context=context)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:12]
    summary = dict(
        schema_version="1", normalizer_version=NORMALIZER_VERSION, ontology_version=ONTOLOGY_VERSION,
        quality_validator_version=QUALITY_VALIDATOR_VERSION,
        run_id=run_id, created_at=datetime.now(timezone.utc).isoformat(), execution_status="completed",
        quality_status=result["quality_status"], document_id=manifest["document_id"],
        document_version=manifest["document_version"], access_scope=access_scope,
        source_bundle_sha256=manifest["source_bundle_sha256"],
        input_artifact_sha256={"manifest.json": hashlib.sha256(raw_manifest).hexdigest(),
                               "elements.jsonl": hashlib.sha256(raw_records).hexdigest()},
        context=asdict(context), context_sha256=_hash(asdict(context)),
        table_source_ref=result["table_source_ref"], detected_context=result["detected_context"],
        fact_count=len(result["facts"]), fact_state_counts=dict(Counter(f["quality_state"] for f in result["facts"])),
        parsed_value_count=sum(f["parsed_value"] is not None for f in result["facts"]),
        canonical_value_count=sum(f["value"] is not None for f in result["facts"]),
        mapped_metric_count=sum(f["metric_code"] is not None for f in result["facts"]),
        issue_counts=dict(sorted(Counter(i["code"] for i in result["issues"]).items())),
        facts_ref="financial_facts.jsonl", issues_ref="normalization_issues.jsonl",
        source_verification="from_adapter_manifest", ingestion_status_action="unchanged",
        limitations=["One selected B02-style income statement; internal draft ontology only.",
                     "No VAS/IFRS crosswalk, accounting consistency, C1 or source-image accuracy verification.",
                     "OCR review flags are preserved; normalized candidates are not verified claims."],
    )
    base = Path(output_dir) if output_dir is not None else bundle / "normalization"
    base.mkdir(parents=True, exist_ok=True)
    destination = base / run_id
    with tempfile.TemporaryDirectory(prefix=".normalization-stage-", dir=base) as temporary:
        stage = Path(temporary) / "run"
        stage.mkdir()
        for filename, values in (("financial_facts.jsonl", result["facts"]),
                                 ("normalization_issues.jsonl", result["issues"])):
            (stage / filename).write_text("".join(_json(v) + "\n" for v in values), encoding="utf-8")
        (stage / "normalization_report.json").write_text(_json(summary) + "\n", encoding="utf-8")
        os.rename(stage, destination)
    return dict(summary, output_dir=str(destination))
