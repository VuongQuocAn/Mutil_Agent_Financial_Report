"""Extract provenance-bearing B01/B03 numeric candidates from OCR tables.

The code deliberately keeps uncertain metadata and unmapped rows in review.
It does not assert legal compliance or visual OCR accuracy.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal, localcontext
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any
from uuid import uuid4

from .financial_normalization import (
    NormalizationContext, NormalizationError, UNITS, _context_from_page,
    _fold, _hash, _json, parse_financial_number,
)
from .ocr_quality import OCRQualityValidator, VALIDATOR_VERSION as QUALITY_VALIDATOR_VERSION

VERSION = "0.1.0"
ONTOLOGY_VERSION = "b01b03-draft-1"
STATEMENTS = {"bang can doi ke toan": "B01", "bao cao luu chuyen tien te": "B03"}
METRICS = {
    "B01": {
        "100": ("assets_current", ("tai san ngan han",)),
        "200": ("assets_noncurrent", ("tai san dai han",)),
        "270": ("assets_total", ("tong cong tai san",)),
        "300": ("liabilities_total", ("no phai tra",)),
        "400": ("equity_total", ("von chu so huu",)),
        "440": ("liabilities_equity_total", ("tong cong nguon von",)),
    },
    "B03": {
        "20": ("cash_operating_net", ("luu chuyen tien thuan", "hoat dong kinh doanh")),
        "30": ("cash_investing_net", ("luu chuyen tien thuan", "hoat dong dau tu")),
        "40": ("cash_financing_net", ("luu chuyen tien thuan", "hoat dong tai chinh")),
        "50": ("cash_net_change", ("luu chuyen tien thuan",)),
        "60": ("cash_opening", ("tien dau nam",)),
        "61": ("cash_fx_effect", ("anh huong", "ty gia")),
        "70": ("cash_closing", ("tien cuoi nam",)),
    },
}


def statement_kind(name: Any) -> str | None:
    if not isinstance(name, str):
        return None
    folded = re.sub(r"\s*\(tiep(?: theo)?\)\s*$", "", _fold(name))
    return STATEMENTS.get(folded)


def _report_end(records: list[dict], table: dict) -> tuple[date | None, list[str]]:
    found = []
    for record in records:
        if record["kind"] != "ocr_block" or record["page"] != table["page"]:
            continue
        text = _fold(record["text"])
        match = re.search(r"(?:tai|ket thuc) ngay (\d{1,2}) thang (\d{1,2}) nam (\d{4})", text)
        if not match:
            match = re.search(r"(?:tai|ket thuc) ngay (\d{1,2})/(\d{1,2})/(\d{4})", text)
        if match:
            try:
                found.append((date(int(match[3]), int(match[2]), int(match[1])), record["source_ref"]))
            except ValueError:
                pass
    dates = {d for d, _ in found}
    return (next(iter(dates)) if len(dates) == 1 else None,
            [ref for _, ref in found])


def _header_role(text: str, kind: str, report_year: int | None) -> str | None:
    folded = _fold(text)
    if kind == "B01":
        if "so cuoi nam" in folded or re.search(r"tai (?:ngay )?31[/ ]12[/ ]20\d{2}", folded):
            return "current"
        if "so dau nam" in folded or re.search(r"tai (?:ngay )?0?1[/ ]0?1[/ ]20\d{2}", folded):
            return "previous"
    else:
        if folded == "nam nay":
            return "current"
        if folded == "nam truoc" or folded.startswith("nam truoc ("):
            return "previous"
        match = re.match(r"nam (20\d{2})(?:\s|$)", folded)
        if match and report_year is not None:
            return "current" if int(match[1]) == report_year else (
                "previous" if int(match[1]) == report_year - 1 else None)
    return None


def _headers(records: list[dict], table: dict, cells: list[dict], kind: str,
             report_year: int | None) -> tuple[dict[str, dict], int, str | None]:
    candidates = [cell for cell in cells if cell["row"] <= 3]
    candidates += [r for r in records if r["kind"] == "ocr_block" and r["page"] == table["page"]
                   and 0 <= table["bbox"][1] - r["bbox"][3] <= 230]
    by_role: dict[str, list[dict]] = defaultdict(list)
    for cell in candidates:
        role = _header_role(cell["text"], kind, report_year)
        if role:
            by_role[role].append(cell)
    if any(len(by_role[role]) != 1 for role in ("current", "previous")):
        raise NormalizationError(f"{kind} requires one current and previous header")
    headers = {role: by_role[role][0] for role in ("current", "previous")}
    if headers["current"]["bbox"][0] >= headers["previous"]["bbox"][0]:
        raise NormalizationError(f"{kind} value headers have ambiguous order")
    internal = [h["row"] for h in headers.values() if h["kind"] == "table_cell"]
    header_row = max(internal) if internal else -1
    return headers, header_row, "EXTERNAL_HEADER_LAYOUT_REQUIRES_REVIEW" if not internal else None


def _cell_at_header_x(row: list[dict], header: dict) -> dict | None:
    center = (header["bbox"][0] + header["bbox"][2]) / 2
    matches = [c for c in row if c["bbox"][0] <= center <= c["bbox"][2]]
    return matches[0] if len(matches) == 1 else None


def _row_identity(row: list[dict], kind: str, value_start_x: float) -> tuple[dict, dict] | None:
    left = [c for c in row if c["bbox"][2] < value_start_x]
    codes = [c for c in left if re.fullmatch(r"\d{3}" if kind == "B01" else r"\d{2}", c["text"].strip())]
    if len(codes) != 1:
        return None
    code = codes[0]
    labels = [c for c in left if c is not code and c["bbox"][0] < code["bbox"][0]
              and any(ch.isalpha() for ch in c["text"])]
    if len(labels) != 1:
        return None
    return labels[0], code


def _header_date(text: str) -> date | None:
    folded = _fold(text)
    match = re.search(r"(?:tai|ngay)\s+(\d{1,2})/(\d{1,2})/(20\d{2})", folded)
    if not match:
        match = re.search(r"(?:tai|ngay)\s+(\d{1,2})\s+thang\s+(\d{1,2})\s+nam\s+(20\d{2})", folded)
    if match:
        try:
            return date(int(match[3]), int(match[2]), int(match[1]))
        except ValueError:
            return None
    return None


def extract_statement_candidates(manifest: dict, records: list[dict], *, page: int,
                                 table_id: str, context: NormalizationContext) -> dict:
    quality = OCRQualityValidator().assess(manifest, records)
    if quality.execution_status != "completed":
        raise NormalizationError("Adapter artifact contract failed quality validation")
    tables = [r for r in records if r["page"] == page and r["kind"] == "table"
              and r["element_id"] == table_id]
    if len(tables) != 1 or (kind := statement_kind(tables[0].get("name"))) is None:
        raise NormalizationError("Expected one explicitly named B01/B03 statement table")
    table = tables[0]
    cells = [r for r in records if r["page"] == page and r["kind"] == "table_cell"
             and r["table_id"] == table_id]
    rows: dict[int, list[dict]] = defaultdict(list)
    for cell in cells:
        rows[cell["row"]].append(cell)
    report_end, period_refs = _report_end(records, table)
    folder_year = manifest.get("report_year")
    fallback_year = int(folder_year) if isinstance(folder_year, str) and re.fullmatch(r"20\d{2}", folder_year) else None
    report_year = report_end.year if report_end else fallback_year
    headers, header_row, layout_issue = _headers(records, table, cells, kind, report_year)
    inferred = _context_from_page(records, table)
    units = {u for u, _, _ in inferred["units"]}
    raw_unit = next(iter(units)) if len(units) == 1 and None not in units else None
    if context.unit_override and raw_unit and context.unit_override != raw_unit:
        raw_unit = None
    elif context.unit_override and not units:
        raw_unit = context.unit_override
    scopes = {s for s, _ in inferred["scopes"]}
    scope = next(iter(scopes)) if len(scopes) == 1 else None
    if context.consolidation_scope and (not scopes or scopes == {context.consolidation_scope}):
        scope = context.consolidation_scope
    upstream: dict[str, list[dict]] = defaultdict(list)
    for issue in quality.issues:
        if issue.source_ref and issue.review_required:
            upstream[issue.source_ref].append(issue.to_dict())
    facts, issues = [], []
    for row_number, row in sorted(rows.items()):
        if row_number <= header_row:
            continue
        identity = _row_identity(row, kind, headers["current"]["bbox"][0])
        if identity is None:
            continue
        current = _cell_at_header_x(row, headers["current"])
        previous = _cell_at_header_x(row, headers["previous"])
        label, code = identity
        metric = METRICS[kind].get(code["text"].strip())
        mapped = bool(metric and all(token in _fold(label["text"]) for token in metric[1]))
        for role, cell in (("current", current), ("previous", previous)):
            if cell is None or cell is (previous if role == "current" else current):
                issues.append({"code": "MISSING_OR_AMBIGUOUS_VALUE_CELL", "row": row_number,
                               "column_role": role, "table_source_ref": table["source_ref"],
                               "label_source_ref": label["source_ref"], "code_source_ref": code["source_ref"]})
                continue
            reasons = ["CANDIDATE_REQUIRES_SOURCE_REVIEW"]
            if report_end is None and fallback_year is not None:
                reasons.append("PERIOD_YEAR_FROM_FOLDER_REQUIRES_REVIEW")
            if layout_issue:
                reasons.append(layout_issue)
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
            if raw_unit is None:
                reasons.append("UNKNOWN_UNIT")
            end = report_end
            if kind == "B03" and role == "previous" and end is not None:
                try:
                    end = end.replace(year=end.year - 1)
                except ValueError:
                    end = None
            if kind == "B01":
                explicit = _header_date(headers[role]["text"])
                if explicit is not None:
                    if role == "current" and end is not None and explicit != end:
                        reasons.append("PERIOD_CONFLICT")
                        end = None
                    else:
                        end = explicit
                elif role == "previous":
                    end = None  # A fiscal-year start must be evidenced, not assumed.
            if end is None or kind == "B03":
                reasons.append("INCOMPLETE_PERIOD")
            if scope is None:
                reasons.append("UNKNOWN_SCOPE")
            if context.accounting_basis is None:
                reasons.append("UNKNOWN_ACCOUNTING_BASIS")
            scale = UNITS[raw_unit] if raw_unit else None
            value = None
            if parsed is not None and scale is not None:
                with localcontext() as decimal_context:
                    decimal_context.prec = max(50, len(parsed) + len(str(scale)) + 5)
                    value = format(Decimal(parsed) * Decimal(scale), "f")
            refs = {"label": [label["source_ref"]], "statement_code": [code["source_ref"]],
                    "column_header": [headers[role]["source_ref"]],
                    "unit": [ref for _, ref, _ in inferred["units"]], "period": period_refs,
                    "scope": [ref for _, ref in inferred["scopes"]]}
            if kind == "B01" and _header_date(headers[role]["text"]) is not None:
                refs["period"] = sorted(set(refs["period"] + [headers[role]["source_ref"]]))
            restated = any(marker in _fold(headers[role]["text"])
                           for marker in ("dieu chinh", "trinh bay lai", "hoi to"))
            if restated:
                reasons.append("RESTATEMENT_REQUIRES_REVIEW")
            affected = {cell["source_ref"], label["source_ref"], code["source_ref"],
                        headers[role]["source_ref"], table["source_ref"]}
            affected.update(ref for values in refs.values() for ref in values)
            quality_issues = [issue for ref in sorted(affected) for issue in upstream[ref]]
            if quality_issues:
                reasons.append("UPSTREAM_OCR_REVIEW")
            fact = dict(
                document_id=manifest["document_id"], document_version=manifest["document_version"],
                access_scope=manifest["access_scope"], company_id=context.company_id,
                source_symbol=manifest.get("symbol"), statement_kind=kind, page=page,
                table_id=table_id, row=row_number, col=cell["col"],
                metric_code=metric[0] if mapped else None, ontology_version=ONTOLOGY_VERSION,
                raw_label=label["text"], statement_code=code["text"], raw_value=cell["text"],
                parsed_value=parsed, raw_unit=raw_unit, unit="VND" if raw_unit else None,
                unit_multiplier=str(scale) if scale is not None else None, value=value,
                fiscal_period=(f"as-of-{end.isoformat()}" if kind == "B01" else
                               f"FY-ended-{end.isoformat()}") if end else None,
                period_start=None, period_end=end.isoformat() if end else None,
                period_kind="instant" if kind == "B01" else "duration", column_role=role,
                consolidation_scope=scope, accounting_basis=context.accounting_basis,
                restatement_status="needs_review" if restated else "not_assessed",
                source_ref=cell["source_ref"],
                page_image_ref=cell["page_image_ref"], bbox=cell["bbox"], context_refs=refs,
                metadata_evidence=context.__dict__, ocr_quality_issues=quality_issues,
                normalization_issues=sorted(set(reasons)), quality_state="needs_review",
            )
            fact["fact_id"] = _hash({"source_ref": cell["source_ref"], "source_hash": manifest["source_bundle_sha256"],
                                     "record_hash": _hash(cell), "normalizer": VERSION,
                                     "ontology": ONTOLOGY_VERSION})[:24]
            facts.append(fact)
            issues.extend({"code": reason, "source_ref": cell["source_ref"], "fact_id": fact["fact_id"]}
                          for reason in fact["normalization_issues"])
    if not facts:
        raise NormalizationError(f"{kind} table produced no unambiguous numeric candidates")
    return {"statement_kind": kind, "facts": facts, "issues": issues,
            "quality_status": "needs_review", "table_source_ref": table["source_ref"]}


def normalize_statement_table(bundle_dir: Path | str, *, page: int, table_id: str,
                              access_scope: str, context: NormalizationContext | None = None,
                              output_dir: Path | str | None = None) -> dict:
    bundle = Path(bundle_dir)
    raw_manifest = (bundle / "manifest.json").read_bytes()
    manifest = json.loads(raw_manifest)
    if not access_scope.strip() or manifest.get("access_scope") != access_scope:
        raise PermissionError("Manifest is outside supplied access scope")
    raw_records = (bundle / "elements.jsonl").read_bytes()
    records = [json.loads(s) for s in raw_records.decode("utf-8").splitlines() if s.strip()]
    context = context or NormalizationContext()
    result = extract_statement_candidates(manifest, records, page=page, table_id=table_id, context=context)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:12]
    summary = dict(schema_version="1", normalizer_version=VERSION, ontology_version=ONTOLOGY_VERSION,
                   quality_validator_version=QUALITY_VALIDATOR_VERSION, run_id=run_id,
                   execution_status="completed", quality_status=result["quality_status"],
                   document_id=manifest["document_id"], document_version=manifest["document_version"],
                   access_scope=access_scope, source_bundle_sha256=manifest["source_bundle_sha256"],
                   input_artifact_sha256={"manifest.json": hashlib.sha256(raw_manifest).hexdigest(),
                                          "elements.jsonl": hashlib.sha256(raw_records).hexdigest()},
                   statement_kind=result["statement_kind"], table_source_ref=result["table_source_ref"],
                   fact_count=len(result["facts"]), mapped_metric_count=sum(f["metric_code"] is not None for f in result["facts"]),
                   issue_counts=dict(Counter(i["code"] for i in result["issues"])),
                   facts_ref="financial_facts.jsonl", issues_ref="normalization_issues.jsonl",
                   ingestion_status_action="unchanged",
                   limitations=["B01/B03 numeric candidates require source review and complete metadata.",
                                "Unmapped rows have no approved ontology code; no legal compliance conclusion."])
    base = Path(output_dir) if output_dir is not None else bundle / "normalization"
    base.mkdir(parents=True, exist_ok=True)
    destination = base / run_id
    with tempfile.TemporaryDirectory(prefix=".statement-stage-", dir=base) as temporary:
        stage = Path(temporary) / "run"
        stage.mkdir()
        for filename, values in (("financial_facts.jsonl", result["facts"]),
                                 ("normalization_issues.jsonl", result["issues"])):
            (stage / filename).write_text("".join(_json(v) + "\n" for v in values), encoding="utf-8")
        (stage / "normalization_report.json").write_text(_json(summary) + "\n", encoding="utf-8")
        os.rename(stage, destination)
    return dict(summary, output_dir=str(destination))
