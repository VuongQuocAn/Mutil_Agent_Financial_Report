from collections import Counter

from financial_multiagent.ingestion.financial_normalization import NormalizationContext
from financial_multiagent.ingestion.statement_candidates import extract_statement_candidates


def fixture(kind, *, external_header):
    prefix = "AAA/v1/p1"
    common = dict(document_id="AAA", document_version="v1", access_scope="tenant-a", page=1,
                  page_image_ref="Financial_Report/AAA/2025/page_1.png",
                  ocr_json_ref="Financial_Report_OCR/AAA/2025/ocr_json/page_1.json", ocr_confidence=None)
    records = []

    def block(element_id, text, box):
        records.append(dict(common, kind="ocr_block", element_id=element_id,
                            source_ref=f"{prefix}/{element_id}", text=text, bbox=box))

    def cell(row, col, text, box):
        cell_id = f"cell_{row}_{col}"
        records.append(dict(common, kind="table_cell", table_id="table_001", cell_id=cell_id,
                            row=row, col=col, text=text, bbox=box,
                            source_ref=f"{prefix}/table_001/{cell_id}"))

    if kind == "B01":
        block("title", "Tại ngày 31 tháng 12 năm 2025", [10, 10, 900, 30])
        block("unit", "Đơn vị: VND", [10, 35, 900, 50])
        block("current", "Số cuối năm", [600, 100, 700, 130])
        block("previous", "Số đầu năm", [800, 100, 900, 130])
        table_name = "BẢNG CÂN ĐỐI KẾ TOÁN"
        cell(0, 0, "TỔNG CỘNG TÀI SẢN", [10, 210, 290, 240])
        cell(0, 1, "270", [320, 210, 365, 240])
        cell(0, 2, "1.234", [590, 210, 710, 240])
        cell(0, 3, "1.000", [790, 210, 910, 240])
        cell(1, 0, "C. NỢ PHẢI TRẢ", [10, 260, 290, 290])
        cell(1, 1, "300", [320, 260, 365, 290])
        cell(1, 2, "12", [410, 260, 450, 290])  # Thuyết minh shifts numeric col IDs.
        cell(1, 3, "234", [590, 260, 710, 290])
        cell(1, 4, "200", [790, 260, 910, 290])
    else:
        block("title", "Cho năm tài chính kết thúc ngày 31 tháng 12 năm 2025", [10, 10, 900, 30])
        block("unit", "Đơn vị: VND", [10, 35, 900, 50])
        table_name = "BÁO CÁO LƯU CHUYỂN TIỀN TỆ"
        cell(0, 0, "CHỈ TIÊU", [10, 160, 290, 190])
        cell(0, 1, "Mã số", [320, 160, 365, 190])
        cell(0, 2, "Năm nay", [590, 160, 710, 190])
        cell(0, 3, "Năm trước", [790, 160, 910, 190])
        cell(1, 0, "Lưu chuyển tiền thuần trong năm", [10, 210, 290, 240])
        cell(1, 1, "50", [320, 210, 365, 240])
        cell(1, 2, "1.234", [590, 210, 710, 240])
        cell(1, 3, "(1.000)", [790, 210, 910, 240])
        cell(2, 0, "Tiền đầu năm", [10, 260, 290, 290])
        cell(2, 1, "60", [320, 260, 365, 290])
        cell(2, 2, "200", [590, 260, 710, 290])
    records.append(dict(common, kind="table", element_id="table_001",
                        source_ref=f"{prefix}/table_001", name=table_name, text=None,
                        bbox=[0, 150, 999, 900], cell_count=sum(r["kind"] == "table_cell" for r in records)))
    manifest = dict(document_id="AAA", document_version="v1", access_scope="tenant-a", symbol="AAA",
                    source_bundle_sha256="a" * 64,
                    pages=[dict(page_number=1, width=1000, height=1000,
                                page_image_ref="Financial_Report/AAA/2025/page_1.png",
                                ocr_json_ref="Financial_Report_OCR/AAA/2025/ocr_json/page_1.json")],
                    issues=[], record_counts=dict(Counter(r["kind"] for r in records)))
    return manifest, records


def test_b01_external_header_resolves_note_shift_without_guessing_prior_date():
    manifest, records = fixture("B01", external_header=True)
    result = extract_statement_candidates(manifest, records, page=1, table_id="table_001",
                                          context=NormalizationContext(company_id="AAA", company_evidence="test"))
    facts = {(f["statement_code"], f["column_role"]): f for f in result["facts"]}
    assert result["statement_kind"] == "B01"
    assert len(facts) == 4
    assert facts["270", "current"]["metric_code"] == "assets_total"
    assert facts["300", "current"]["value"] == "234"
    assert facts["300", "current"]["col"] == 3
    assert facts["270", "previous"]["period_end"] is None
    assert all(f["quality_state"] == "needs_review" for f in facts.values())


def test_b03_missing_comparison_is_issue_not_zero():
    manifest, records = fixture("B03", external_header=False)
    result = extract_statement_candidates(manifest, records, page=1, table_id="table_001",
                                          context=NormalizationContext(company_id="AAA", company_evidence="test"))
    facts = {(f["statement_code"], f["column_role"]): f for f in result["facts"]}
    assert len(facts) == 3
    assert facts["50", "previous"]["value"] == "-1000"
    assert ("60", "previous") not in facts
    assert any(issue["code"] == "MISSING_OR_AMBIGUOUS_VALUE_CELL" and issue["row"] == 2
               and issue["column_role"] == "previous" for issue in result["issues"])
