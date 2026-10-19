"""Deterministic checks on imported OCR artifacts; never rewrites source OCR."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import errno
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
import time
from typing import Any, Callable
import unicodedata
from uuid import uuid4

VALIDATOR_VERSION = "1.0.0"
CHECKS = ("contract", "pages", "text", "numeric", "tables", "confidence", "assets")
KINDS = {"ocr_block", "table", "table_cell", "visual_element"}
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


@dataclass(frozen=True)
class QualityProfile:
    profile_id: str = "legacy-ocr"
    profile_version: str = "1"
    enabled_checks: tuple[str, ...] = CHECKS
    required_checks: tuple[str, ...] = ("contract", "pages", "text", "numeric", "tables")
    numeric_candidate_pattern: str = r"(?=.*[0-9])(?=.*[OoIl])[0-9OoIl\s.,()+%\-]+"
    sparse_page_characters: int | None = None
    confidence_scale: tuple[float, float] | None = None
    minimum_confidence: float | None = None

    def __post_init__(self) -> None:
        if not self.profile_id.strip() or not self.profile_version.strip():
            raise ValueError("Profile ID and version are required")
        if set(self.enabled_checks + self.required_checks) - set(CHECKS):
            raise ValueError("Unknown check in profile")
        if "contract" not in self.enabled_checks:
            raise ValueError("Contract checks cannot be disabled")
        re.compile(self.numeric_candidate_pattern)
        if self.sparse_page_characters is not None and self.sparse_page_characters < 0:
            raise ValueError("Sparse-page threshold must be nonnegative")
        if self.minimum_confidence is not None:
            if self.confidence_scale is None:
                raise ValueError("Low-confidence check requires an explicit scale")
            low, high = self.confidence_scale
            if not low <= self.minimum_confidence <= high:
                raise ValueError("Confidence threshold is outside scale")
        if self.confidence_scale is not None:
            low, high = self.confidence_scale
            if not all(math.isfinite(x) for x in (low, high)) or low >= high:
                raise ValueError("Invalid confidence scale")


@dataclass
class OCRQualityIssue:
    check_id: str
    code: str
    severity: str
    review_required: bool
    page: int | None
    source_ref: str | None
    reason: str
    locator: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["issue_id"] = hashlib.sha256(_json(value).encode()).hexdigest()[:24]
        return value


@dataclass
class OCRQualityReport:
    document_id: str | None
    document_version: str | None
    access_scope: str
    source_bundle_sha256: str | None
    profile: QualityProfile
    execution_status: str = "completed"
    quality_status: str = "checks_passed"
    metrics: dict[str, Any] = field(default_factory=dict)
    checks: dict[str, Any] = field(default_factory=dict)
    issues: list[OCRQualityIssue] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        del value["issues"]
        value.update(
            schema_version="1", validator_version=VALIDATOR_VERSION,
            visual_accuracy="not_measured", source_verification="from_adapter_manifest",
            ingestion_status_action="unchanged",
            limitations=["No image bytes reread; source hashes inherited from adapter.",
                         "Checks do not measure transcription accuracy or accounting consistency."],
            issue_counts=dict(sorted(Counter(i.severity for i in self.issues).items())),
            review_issue_count=sum(i.review_required for i in self.issues),
            issues_ref="quality_issues.jsonl",
        )
        return value


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _safe_ref(value: Any) -> bool:
    return (isinstance(value, str) and bool(value) and "\\" not in value
            and ":" not in value and not PurePosixPath(value).is_absolute()
            and all(p not in {"", ".", ".."} for p in value.split("/")))


def _box(value: Any) -> bool:
    return (isinstance(value, list) and len(value) == 4
            and all(isinstance(x, (int, float)) and not isinstance(x, bool)
                    and math.isfinite(x) for x in value)
            and 0 <= value[0] < value[2] and 0 <= value[1] < value[3])


def _identity(manifest: dict[str, Any]) -> dict[str, str | None]:
    return {k: manifest.get(k) if isinstance(manifest.get(k), str) else None
            for k in ("document_id", "document_version", "source_bundle_sha256")}


class OCRQualityValidator:
    def assess(self, manifest: dict[str, Any], records: list[dict[str, Any]],
               profile: QualityProfile | None = None) -> OCRQualityReport:
        profile = profile or QualityProfile()
        scope = manifest.get("access_scope")
        if not isinstance(scope, str) or not scope.strip():
            raise PermissionError("Manifest requires an access scope")
        # Do not echo or persist records belonging to another scope.
        if any(isinstance(r, dict) and r.get("access_scope") != scope for r in records):
            raise PermissionError("Record access scope differs from manifest")
        report = OCRQualityReport(**_identity(manifest), access_scope=scope, profile=profile)
        report.checks = {c: {"status": "skipped", "evaluated": 0, "reason": "Disabled by profile"}
                         for c in CHECKS}

        def add(check: str, code: str, reason: str, record: dict | None = None,
                *, severity: str = "warning", review: bool = True,
                evidence: dict | None = None, locator: str | None = None) -> None:
            record = record or {}
            report.issues.append(OCRQualityIssue(check, code, severity, review,
                                record.get("page"), record.get("source_ref"), reason,
                                locator, evidence or {}))

        pages = manifest.get("pages")
        valid_pages: dict[int, dict] = {}
        if not isinstance(pages, list) or not pages:
            add("contract", "INVALID_PAGES", "Manifest must contain pages", severity="error")
        else:
            for p in pages:
                if (not isinstance(p, dict) or type(p.get("page_number")) is not int
                        or p["page_number"] < 1 or p["page_number"] in valid_pages
                        or any(type(p.get(k)) is not int or p[k] <= 0 for k in ("width", "height"))
                        or any(not _safe_ref(p.get(k)) for k in ("page_image_ref", "ocr_json_ref"))):
                    add("contract", "INVALID_PAGE", "Invalid page metadata or source locator", severity="error")
                else:
                    valid_pages[p["page_number"]] = p
            if sorted(valid_pages) != list(range(1, len(valid_pages) + 1)):
                add("contract", "PAGE_SEQUENCE", "Manifest pages must be contiguous from 1", severity="error")
        if (any(not isinstance(manifest.get(k), str) or not IDENTIFIER.fullmatch(manifest[k])
                for k in ("document_id", "document_version"))
                or not isinstance(manifest.get("source_bundle_sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", manifest.get("source_bundle_sha256", ""))):
            add("contract", "INVALID_MANIFEST", "Missing or invalid document identity/source hash", severity="error")
        adapter_issues = manifest.get("issues", [])
        if not isinstance(adapter_issues, list) or any(
            not isinstance(i, dict) or any(k in i and not isinstance(i[k], str)
                                           for k in ("code", "location", "field"))
            for i in adapter_issues
        ):
            add("contract", "INVALID_ADAPTER_ISSUES", "Adapter issues have an invalid shape", severity="error")
        seen: set[str] = set()
        tables: dict[tuple[int, str], dict] = {}
        for n, r in enumerate(records):
            location = f"elements.jsonl:{n + 1}"
            if not isinstance(r, dict):
                add("contract", "INVALID_RECORD", "Record must be an object", severity="error", locator=location)
                continue
            page = valid_pages.get(r.get("page")) if type(r.get("page")) is int else None
            kind = r.get("kind") if isinstance(r.get("kind"), str) else None
            identity_ok = all(r.get(k) == manifest.get(k) for k in ("document_id", "document_version"))
            id_fields = ("table_id", "cell_id") if kind == "table_cell" else ("element_id",)
            ids_ok = all(isinstance(r.get(k), str) and IDENTIFIER.fullmatch(r[k]) for k in id_fields)
            expected = (f"{manifest.get('document_id')}/{manifest.get('document_version')}/p{r.get('page')}/"
                        + "/".join(str(r.get(k, "")) for k in id_fields))
            ref = r.get("source_ref")
            valid = (identity_ok and kind in KINDS and page is not None and ids_ok
                     and ref == expected and ref not in seen and _box(r.get("bbox")))
            if page:
                valid = valid and all(r.get(k) == page[k] for k in ("page_image_ref", "ocr_json_ref"))
                if _box(r.get("bbox")):
                    valid = valid and r["bbox"][2] <= page["width"] and r["bbox"][3] <= page["height"]
            if kind in {"ocr_block", "table_cell"} and not isinstance(r.get("text"), str):
                valid = False
            if kind == "table_cell" and any(type(r.get(k)) is not int or r[k] < 0 for k in ("row", "col")):
                valid = False
            if kind == "table" and (type(r.get("cell_count")) is not int or r["cell_count"] < 0):
                valid = False
            if "asset_refs" in r and (not isinstance(r["asset_refs"], dict)
                                       or any(not _safe_ref(v) for v in r["asset_refs"].values())):
                valid = False
            if not valid:
                # An invalid ref must never masquerade as resolvable evidence.
                add("contract", "INVALID_RECORD", "Identity, reference, text, bbox or table contract mismatch",
                    severity="error", locator=location)
                continue
            seen.add(ref)
            if kind == "table":
                tables[r["page"], r["element_id"]] = r
        if not any(i.check_id == "contract" for i in report.issues):
            counts = dict(sorted(Counter(r["kind"] for r in records).items()))
            if counts != manifest.get("record_counts"):
                add("contract", "COUNT_MISMATCH", "Record counts differ from manifest", severity="error")
            cell_counts = Counter((r["page"], r["table_id"]) for r in records if r["kind"] == "table_cell")
            for key, table in tables.items():
                if cell_counts[key] != table["cell_count"]:
                    add("contract", "TABLE_COUNT_MISMATCH", "Table cell count mismatch", table, severity="error")
            for r in records:
                if r["kind"] == "table_cell" and (r["page"], r["table_id"]) not in tables:
                    add("contract", "ORPHAN_CELL", "Cell has no parent table", r, severity="error")
        if report.issues:
            report.execution_status, report.quality_status = "failed", "not_assessed"
            report.checks["contract"] = {"status": "error", "evaluated": len(records), "reason": "Invalid artifact contract"}
            for c in CHECKS[1:]:
                report.checks[c]["reason"] = "Input contract failed"
            return report
        report.checks["contract"] = {"status": "passed", "evaluated": len(records), "reason": None}

        per_page = {p: {"page": p, "record_counts": Counter(), "characters": 0,
                        "empty_cells": 0, "confidence_present": 0, "confidence_total": 0}
                    for p in valid_pages}
        numeric_pattern = re.compile(profile.numeric_candidate_pattern)
        positions: set[tuple[int, str, int, int]] = set()
        evaluated = Counter()
        for r in records:
            stats = per_page[r["page"]]
            stats["record_counts"][r["kind"]] += 1
            is_text = r["kind"] in {"ocr_block", "table_cell"}
            text = r.get("text") if is_text else ""
            if is_text:
                stats["characters"] += len(text)
                stats["empty_cells"] += int(r["kind"] == "table_cell" and not text.strip())
                stats["confidence_total"] += 1
                stats["confidence_present"] += int(r.get("ocr_confidence") is not None)
            if is_text and "text" in profile.enabled_checks:
                evaluated["text"] += 1
                if "\ufffd" in text or any(unicodedata.category(c) == "Cc" and c not in "\t\r\n" for c in text):
                    add("text", "SUSPICIOUS_CHARACTERS", "Replacement or unusual control characters", r,
                        evidence={"text": text})
                if r["kind"] == "ocr_block" and not text.strip():
                    add("text", "EMPTY_TEXT_BLOCK", "Text block is empty", r)
            if r["kind"] == "table_cell":
                if "numeric" in profile.enabled_checks:
                    evaluated["numeric"] += 1
                    if numeric_pattern.fullmatch(text.strip()):
                        add("numeric", "SUSPICIOUS_NUMERIC_TEXT", "Possible character confusion in numeric candidate", r,
                            evidence={"text": text})
                if "tables" in profile.enabled_checks:
                    evaluated["tables"] += 1
                    parent = tables[r["page"], r["table_id"]]
                    a, b = r["bbox"], parent["bbox"]
                    if not (b[0] <= a[0] < a[2] <= b[2] and b[1] <= a[1] < a[3] <= b[3]):
                        add("tables", "CELL_OUTSIDE_TABLE", "Cell bbox extends beyond parent table", r)
                    position = (r["page"], r["table_id"], r["row"], r["col"])
                    if position in positions:
                        add("tables", "DUPLICATE_CELL_POSITION", "Repeated row/col; inspect merged-cell representation", r)
                    positions.add(position)
            if is_text and "confidence" in profile.enabled_checks and r.get("ocr_confidence") is not None:
                score = r["ocr_confidence"]
                if not isinstance(score, (int, float)) or isinstance(score, bool) or not math.isfinite(score):
                    add("confidence", "INVALID_CONFIDENCE", "Confidence must be a finite number when supplied", r)
                    evaluated["confidence"] += 1
                elif profile.confidence_scale is not None:
                    evaluated["confidence"] += 1
                    low, high = profile.confidence_scale
                    if not low <= score <= high:
                        add("confidence", "CONFIDENCE_OUT_OF_RANGE", "Confidence outside configured scale", r)
                    elif profile.minimum_confidence is not None and score < profile.minimum_confidence:
                        add("confidence", "LOW_CONFIDENCE", "Confidence below configured threshold", r)
        if "pages" in profile.enabled_checks:
            for p, stats in per_page.items():
                evaluated["pages"] += 1
                location = {"page": p}
                if not stats["characters"]:
                    add("pages", "PAGE_WITHOUT_TEXT", "No text characters; may be a cover or visual page", location,
                        severity="info", review=False, locator=f"p{p}")
                elif profile.sparse_page_characters is not None and stats["characters"] < profile.sparse_page_characters:
                    add("pages", "SPARSE_PAGE", "Below configured character threshold", location, locator=f"p{p}")
        if "assets" in profile.enabled_checks:
            refs = {f"p{r['page']}/{r['element_id']}": r for r in records if "element_id" in r}
            dedup = set()
            for issue in manifest.get("issues", []):
                if not isinstance(issue, dict) or issue.get("code") not in {"MISSING_CROP", "UNMAPPABLE_CROP"}:
                    continue
                key = (issue.get("code"), issue.get("location"), issue.get("field"))
                if key in dedup:
                    continue
                dedup.add(key)
                evaluated["assets"] += 1
                add("assets", issue["code"], "Optional crop issue inherited from adapter",
                    refs.get(issue.get("location")), severity="info", review=False,
                    evidence={"field": issue.get("field")})
        for check in CHECKS[1:]:
            if check not in profile.enabled_checks:
                continue
            flagged = sum(i.check_id == check for i in report.issues)
            if check == "confidence" and not evaluated[check]:
                reason = "Confidence absent or scale not configured"
                report.checks[check] = {"status": "skipped", "evaluated": 0, "reason": reason}
            else:
                report.checks[check] = {"status": "flagged" if flagged else "passed",
                                        "evaluated": evaluated[check], "reason": None}
        report.metrics = {
            "page_count": len(per_page), "record_count": len(records),
            "record_counts": dict(sorted(Counter(r["kind"] for r in records).items())),
            "pages": [dict(v, record_counts=dict(sorted(v["record_counts"].items())))
                      for _, v in sorted(per_page.items())],
            "confidence_present": sum(v["confidence_present"] for v in per_page.values()),
            "confidence_total": sum(v["confidence_total"] for v in per_page.values()),
        }
        if any(i.review_required for i in report.issues):
            report.quality_status = "needs_review"
        elif any(report.checks[c]["status"] == "skipped" for c in profile.required_checks):
            report.quality_status = "not_assessed"
        return report


TRANSIENT_ERRNOS = {errno.EIO, errno.EAGAIN, errno.ETIMEDOUT, errno.ECONNRESET}


def _retry(operation: Callable[[], Any], events: list[dict], label: str) -> Any:
    for attempt in range(1, 4):
        try:
            return operation()
        except OSError as exc:
            events.append({"operation": label, "attempt": attempt, "errno": exc.errno})
            if exc.errno not in TRANSIENT_ERRNOS or attempt == 3:
                raise
            time.sleep(0.1 * 2 ** (attempt - 1))


def write_quality_report(report: dict, issues: list[dict], output_dir: Path | str) -> Path:
    """Publish a complete immutable run directory; failures leave no final run."""
    base = Path(output_dir)
    base.mkdir(parents=True, exist_ok=True)
    destination = base / report["report_id"]
    with tempfile.TemporaryDirectory(prefix=".quality-stage-", dir=base) as temporary:
        stage = Path(temporary) / "run"
        stage.mkdir()
        (stage / "quality_issues.jsonl").write_text(
            "".join(_json(i) + "\n" for i in issues), encoding="utf-8")
        lines = ["# OCR Quality Report", "", f"Document: `{report.get('document_id')}`",
                 f"Execution: `{report['execution_status']}` · Quality: `{report['quality_status']}`",
                 f"Review issues: {report['review_issue_count']}", "",
                 "Visual accuracy: **not measured**. Source images were not reread.", "",
                 "Details: `quality_issues.jsonl`. No source OCR was modified.", ""]
        for check, result in report["checks"].items():
            lines.append(f"- {check}: {result['status']} ({result['evaluated']})")
        lines.extend(["", "## Issues (first 50)", ""])
        for issue in issues[:50]:
            lines.append(f"- {issue['code']} · `{issue.get('source_ref') or issue.get('locator') or 'manifest'}`: {issue['reason']}")
        (stage / "quality_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        (stage / "quality_report.json").write_text(_json(report) + "\n", encoding="utf-8")
        # The run directory is the completion boundary, not an individual file.
        os.rename(stage, destination)
    return destination


def assess_ocr_bundle(bundle_dir: Path | str, output_dir: Path | str | None = None, *,
                      access_scope: str, profile: QualityProfile | None = None) -> dict[str, Any]:
    """Read only adapter JSON artifacts; return a summary with the saved run path.

    A missing/unreadable manifest raises before scope can be established. Once
    scope is checked, malformed records produce a persisted failed assessment.
    """
    if not access_scope.strip():
        raise PermissionError("Explicit access scope required")
    profile = profile or QualityProfile()
    bundle = Path(bundle_dir)
    events: list[dict] = []
    raw_manifest = _retry(lambda: (bundle / "manifest.json").read_bytes(), events, "read_manifest")
    manifest = json.loads(raw_manifest)
    if not isinstance(manifest, dict) or manifest.get("access_scope") != access_scope:
        raise PermissionError("Manifest is outside supplied access scope")
    raw_records: bytes | None = None
    try:
        raw_records = _retry(lambda: (bundle / "elements.jsonl").read_bytes(), events, "read_records")
        records = [json.loads(line) for line in raw_records.decode("utf-8").splitlines() if line.strip()]
    except (ValueError, UnicodeError, FileNotFoundError):
        report = OCRQualityReport(**_identity(manifest), access_scope=access_scope, profile=profile,
                                  execution_status="failed", quality_status="not_assessed")
        report.issues = [OCRQualityIssue("contract", "UNREADABLE_RECORDS", "error", True, None, None,
                                         "elements.jsonl is missing or invalid", "elements.jsonl")]
        report.checks = {c: {"status": "error" if c == "contract" else "skipped",
                            "evaluated": 0, "reason": "Records unavailable"} for c in CHECKS}
    else:
        report = OCRQualityValidator().assess(manifest, records, profile)
    value = report.to_dict()
    value.update(
        report_id=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:12],
        created_at=datetime.now(timezone.utc).isoformat(),
        input_artifact_sha256={"manifest.json": hashlib.sha256(raw_manifest).hexdigest(),
                               "elements.jsonl": hashlib.sha256(raw_records).hexdigest() if raw_records is not None else None},
        trace=events,
    )
    issues = [i.to_dict() for i in report.issues]
    # Each write retry gets a fresh stage but publishes the same immutable run.
    destination = _retry(lambda: write_quality_report(value, issues, output_dir or bundle / "quality"),
                         events, "write_report")
    return dict(value, output_dir=str(destination))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--access-scope", required=True)
    args = parser.parse_args()
    report = assess_ocr_bundle(args.bundle_dir, args.output_dir, access_scope=args.access_scope)
    print(_json({k: report[k] for k in ("execution_status", "quality_status", "review_issue_count", "output_dir")}))
    if report["execution_status"] == "failed":
        parser.exit(2)


if __name__ == "__main__":
    main()
