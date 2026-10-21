import copy
from collections import Counter
import json
from pathlib import Path

import pytest

from financial_multiagent.ingestion import financial_normalization as norm


def sample(tmp_path):
    folder = tmp_path / "bundle"
    folder.mkdir()
    prefix = "AAA/v1/p12"
    common = dict(document_id="AAA", document_version="v1", access_scope="tenant-a", page=12,
                  page_image_ref="Financial_OCR/AAA/2025/page_12.png",
                  ocr_json_ref="Financial_Report_OCR/AAA/2025/ocr_json/page_12.json", ocr_confidence=None)
    # Keep a contiguous 12-page manifest; only page 12 has records.
    pages = [dict(page_number=p, width=1000, height=1000,
                  page_image_ref=f"Financial_OCR/AAA/2025/page_{p}.png",
                  ocr_json_ref=f"Financial_Report_OCR/AAA/2025/ocr_json/page_{p}.json") for p in range(1, 13)]
    records = []
    for element_id, text, box in (
        ("title", "Cho năm tài chính kết thúc ngày 31 tháng 12 năm 2025", [10, 10, 900, 20]),
        ("unit", "Đơn vị: triệu VND", [10, 25, 900, 35]),
    ):
        records.append(dict(common, kind="ocr_block", element_id=element_id,
                            source_ref=f"{prefix}/{element_id}", text=text, bbox=box))
    records.append(dict(common, kind="table", element_id="table_001", source_ref=f"{prefix}/table_001",
                        name="BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH", text=None, bbox=[0, 50, 999, 900], cell_count=12))
    grid = [
        ["CHỈ TIÊU", "Mã số", "Năm nay", "Năm trước"],
        ["Doanh thu thuần về bán hàng và cung cấp dịch vụ", "10", "1.234", "(2.000)"],
        ["Lãi cơ bản trên cổ phiếu", "70", "1.000", "-"],
    ]
    for row, values in enumerate(grid):
        for col, text in enumerate(values):
            cell_id = f"cell_{row}_{col}"
            records.append(dict(common, kind="table_cell", table_id="table_001", cell_id=cell_id,
                                row=row, col=col, text=text, bbox=[col * 200 + 1, row * 100 + 51, col * 200 + 190, row * 100 + 100],
                                source_ref=f"{prefix}/table_001/{cell_id}"))
    manifest = dict(document_id="AAA", document_version="v1", access_scope="tenant-a", symbol="AAA",
                    source_bundle_sha256="a" * 64, pages=pages, issues=[],
                    record_counts=dict(Counter(r["kind"] for r in records)))
    save(folder, manifest, records)
    return folder, manifest, records


def save(folder, manifest, records):
    (folder / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    (folder / "elements.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")


def context(**kwargs):
    base = dict(company_id="AAA", company_evidence="Test company binding", consolidation_scope="standalone",
                scope_evidence="Synthetic scope confirmed", period_overrides={
                    2: dict(start="2025-01-01", end="2025-12-31", evidence="Synthetic fiscal calendar"),
                    3: dict(start="2024-01-01", end="2024-12-31", evidence="Synthetic comparative calendar")})
    base.update(kwargs)
    return norm.NormalizationContext(**base)


@pytest.mark.parametrize("raw,expected", [
    ("2.818.277.552.061", "2818277552061"), ("(7.501.350.091)", "-7501350091"),
    ("1.234,50", "1234.50"), ("-1.234", "-1234"), ("1\u00a0234", "1234"),
    ("0", "0"), ("(0)", "0"), ("", None), ("_", None), ("—", None), ("-", None),
    ("1234567890123456789012345678901234567890", "1234567890123456789012345678901234567890"),
])
def test_exact_vietnamese_numbers(raw, expected):
    assert norm.parse_financial_number(raw) == expected


@pytest.mark.parametrize("raw", ["1O0", "1.23", "12 34", "1.234*", "NaN", "Infinity", "(-100)", "1,2,3", "12%"])
def test_invalid_numbers_are_not_repaired(raw):
    with pytest.raises(norm.NormalizationError):
        norm.parse_financial_number(raw)


def test_explicit_locale_changes_separator_meaning():
    assert norm.parse_financial_number("1,234", locale="vi_VN") == "1.234"
    assert norm.parse_financial_number("1,234", locale="en_US") == "1234"


def test_normalized_values_preserve_source_and_eps_dimension(tmp_path):
    _, manifest, records = sample(tmp_path)
    before = copy.deepcopy(records)
    result = norm.FinancialNormalizer().normalize(manifest, records, page=12, table_id="table_001", context=context())
    current, previous, eps, missing = result["facts"]
    assert current["metric_code"] == "revenue_net"
    assert current["raw_value"] == "1.234"
    assert current["parsed_value"] == "1234"
    assert current["value"] == "1234000000"
    assert current["raw_unit"] == "million_VND" and current["unit"] == "VND"
    assert current["quality_state"] == "normalized_candidate"
    assert current["period_start"] == "2025-01-01"
    assert previous["value"] == "-2000000000" and previous["period_end"] == "2024-12-31"
    assert eps["parsed_value"] == "1000" and eps["value"] is None
    assert "EPS_UNIT_REQUIRES_CONFIRMATION" in eps["normalization_issues"]
    assert missing["value"] is None and "MISSING_VALUE" in missing["normalization_issues"]
    assert before == records
    assert current["context_refs"]["unit"] == ["AAA/v1/p12/unit"]


def test_explicit_eps_unit_never_inherits_million_multiplier(tmp_path):
    _, manifest, records = sample(tmp_path)
    result = norm.FinancialNormalizer().normalize(manifest, records, page=12, table_id="table_001",
        context=context(eps_unit="VND/share", eps_unit_evidence="Synthetic EPS note"))
    eps = result["facts"][2]
    assert eps["value"] == "1000" and eps["unit"] == "VND/share"


def test_missing_scope_period_and_company_do_not_use_folder_metadata(tmp_path):
    _, manifest, records = sample(tmp_path)
    fact = norm.FinancialNormalizer().normalize(manifest, records, page=12, table_id="table_001")["facts"][0]
    assert fact["company_id"] is None
    assert fact["consolidation_scope"] is None
    assert fact["period_end"] == "2025-12-31" and fact["period_start"] is None
    assert fact["quality_state"] == "needs_review"
    assert {"UNKNOWN_SCOPE", "INCOMPLETE_PERIOD", "UNKNOWN_COMPANY"} <= set(fact["normalization_issues"])


@pytest.mark.parametrize("mutation,code", [
    ("unit_missing", "UNKNOWN_UNIT"), ("unit_conflict", "UNIT_CONFLICT"),
    ("period_conflict", "PERIOD_CONFLICT"), ("scope_conflict", "SCOPE_CONFLICT"),
    ("unknown_metric", "UNMAPPED_OR_CONFLICTING_METRIC"),
    ("invalid_number", "INVALID_NUMBER"), ("ocr_issue", "UPSTREAM_OCR_REVIEW"),
])
def test_ambiguity_stays_reviewable(tmp_path, mutation, code):
    _, manifest, records = sample(tmp_path)
    options = {}
    if mutation == "unit_missing":
        records[1]["text"] = "Không có đơn vị tiền"
    elif mutation == "unit_conflict":
        options.update(unit_override="VND", unit_evidence="Conflicting metadata")
    elif mutation == "period_conflict":
        options["period_overrides"] = {2: dict(start="2023-01-01", end="2023-12-31", evidence="Conflicting year")}
    elif mutation == "scope_conflict":
        records[0]["text"] += " BÁO CÁO hợp nhất"
    elif mutation == "unknown_metric":
        records[7]["text"] = "Chỉ tiêu không tương ứng mã 10"
    elif mutation == "invalid_number":
        records[9]["text"] = "1O0"
    elif mutation == "ocr_issue":
        records[9]["bbox"] = [0, 10, 190, 150]
    result = norm.FinancialNormalizer().normalize(manifest, records, page=12, table_id="table_001", context=context(**options))
    fact = result["facts"][0]
    assert code in fact["normalization_issues"]
    assert fact["quality_state"] == "needs_review"


def test_row_code_alone_cannot_map_financial_metric(tmp_path):
    _, manifest, records = sample(tmp_path)
    records[7]["text"] = "Không phải doanh thu"
    fact = norm.FinancialNormalizer().normalize(manifest, records, page=12, table_id="table_001", context=context())["facts"][0]
    assert fact["statement_code"] == "10" and fact["metric_code"] is None and fact["value"] is None


def test_duplicate_metric_rows_are_not_silently_accepted(tmp_path):
    _, manifest, records = sample(tmp_path)
    for cell in copy.deepcopy(records[7:11]):
        cell.update(row=3, cell_id=cell["cell_id"] + "_copy", source_ref=cell["source_ref"] + "_copy")
        records.append(cell)
    records[2]["cell_count"] += 4
    manifest["record_counts"]["table_cell"] += 4
    facts = norm.FinancialNormalizer().normalize(manifest, records, page=12, table_id="table_001", context=context())["facts"]
    revenue = [f for f in facts if f["metric_code"] == "revenue_net"]
    assert len(revenue) == 4
    assert all("DUPLICATE_METRIC_FOR_PERIOD" in f["normalization_issues"] for f in revenue)


def test_explicit_metadata_needs_evidence():
    with pytest.raises(norm.NormalizationError):
        norm.NormalizationContext(consolidation_scope="standalone")
    with pytest.raises(norm.NormalizationError):
        norm.NormalizationContext(period_overrides={2: dict(start="2025-12-31", end="2024-01-01", evidence="bad")})


def test_missing_period_title_cannot_use_manifest_year(tmp_path):
    _, manifest, records = sample(tmp_path)
    records[0]["text"] = "Báo cáo tài chính"
    manifest["report_year"] = "2025"
    fact = norm.FinancialNormalizer().normalize(manifest, records, page=12, table_id="table_001")["facts"][0]
    assert fact["period_end"] is None and fact["fiscal_period"] is None


@pytest.mark.parametrize("title", ["Cho năm tài chính kết thúc ngày 31/12/2025",
                                   "năm kết thúc ngày 31/12/2025",
                                   "năm tài chính kết thúc ngày 31/12/2025"])
def test_explicit_year_and_vnd_table_headers(tmp_path, title):
    _, manifest, records = sample(tmp_path)
    records[0]["text"] = title
    records[1]["text"] = "Báo cáo tài chính"
    for cell in records:
        if cell["kind"] == "table_cell" and cell["row"] == 0:
            cell["text"] = {0: "Chỉ tiêu", 1: "Mã\nsố", 2: "Năm 2025\nVND",
                            3: "Năm 2024\nVND"}[cell["col"]]
    result = norm.FinancialNormalizer().normalize(manifest, records, page=12, table_id="table_001")
    current, previous = result["facts"][:2]
    assert current["metric_code"] == "revenue_net"
    assert current["raw_unit"] == "VND" and current["value"] == "1234"
    assert current["period_end"] == "2025-12-31"
    assert current["context_refs"]["unit"] == current["context_refs"]["column_header"]
    assert previous["period_end"] == "2024-12-31"


def test_external_header_layout_keeps_review_and_provenance(tmp_path):
    _, manifest, records = sample(tmp_path)
    records[:] = records[:3]
    records[0]["text"] = "Cho năm tài chính kết thúc ngày 31/12/2025"
    records[1]["text"] = "Đơn vị tính: VND"
    records[2]["bbox"] = [200, 760, 2300, 1900]
    header_blocks = [
        # The report title also says "Năm 2025" but is farther from the table
        # than the actual column header, as in BTW/2023.
        ("year_title", "Năm 2025", [990, 555, 1190, 590]),
        ("label_header", "CHỈ TIÊU", [367, 658, 531, 693]),
        ("code_header", "Mã Thuyết", [1261, 637, 1492, 669]),
        ("note_header", "số minh", [1256, 679, 1460, 711]),
        ("current_header", "Năm 2025", [1694, 676, 1877, 708]),
        ("previous_header", "Năm 2024", [2101, 676, 2285, 708]),
    ]
    common = {k: records[0][k] for k in ("document_id", "document_version", "access_scope",
                                           "page", "page_image_ref", "ocr_json_ref", "ocr_confidence")}
    for element_id, text, bbox in header_blocks:
        records.append(dict(common, kind="ocr_block", element_id=element_id,
                            source_ref=f"AAA/v1/p12/{element_id}", text=text, bbox=bbox))
    grid = [
        ["1.", "Doanh thu bán hàng và cung cấp dịch vụ", "01", "17", "267.402.963.954", "268.905.178.795"],
        ["2.", "Doanh thu thuần về bán hàng và cung cấp dịch vụ", "10", "17", "267.402.963.954", "268.891.407.690"],
        ["3.", "Giá vốn hàng bán", "11", "18", "237.846.985.781", "244.369.939.617"],
    ]
    bounds = [(270, 303), (355, 1187), (1266, 1326), (1410, 1460), (1559, 1877), (1944, 2282)]
    for row, values in enumerate(grid):
        for col, value in enumerate(values):
            cell_id = f"external_{row}_{col}"
            records.append(dict(common, kind="table_cell", table_id="table_001", cell_id=cell_id,
                                row=row, col=col, text=value,
                                bbox=[bounds[col][0], 778 + row * 100, bounds[col][1], 820 + row * 100],
                                source_ref=f"AAA/v1/p12/table_001/{cell_id}"))
    records[2]["cell_count"] = 18
    manifest["record_counts"] = dict(Counter(r["kind"] for r in records))
    for page in manifest["pages"]:
        page.update(width=2500, height=3505)
    result = norm.FinancialNormalizer().normalize(manifest, records, page=12, table_id="table_001")
    assert len(result["facts"]) == 6
    first = result["facts"][0]
    assert first["metric_code"] == "revenue_gross" and first["value"] == "267402963954"
    assert first["context_refs"]["column_header"] == ["AAA/v1/p12/current_header"]
    assert "AAA/v1/p12/code_header" in first["context_refs"]["layout"]
    assert all("EXTERNAL_HEADER_LAYOUT_REQUIRES_REVIEW" in f["normalization_issues"]
               for f in result["facts"])


def test_table_wide_cell_bboxes_require_header_and_metric_corroboration(tmp_path):
    _, manifest, records = sample(tmp_path)
    records[:] = records[:3]
    records[1]["text"] = "Báo cáo tài chính"
    table_box = records[2]["bbox"]
    common = {k: records[0][k] for k in ("document_id", "document_version", "access_scope",
                                           "page", "page_image_ref", "ocr_json_ref", "ocr_confidence")}
    grid = [
        ["Mã", "CHỈ TIÊU", "Thuyết", "Năm 2025", "Năm 2024"],
        ["số", None, "minh", "VND", "(Đã điều chỉnh)"],
        [None, None, None, None, "VND"],
        ["01", "Doanh thu bán hàng và cung cấp dịch vụ", "21", "468.755.201.520", "390.345.691.206"],
        ["10", "Doanh thu thuần về bán hàng và cung cấp dịch vụ", "21", "468.747.514.020", "389.679.505.086"],
        ["11", "Giá vốn hàng bán", "22", "304.430.065.852", "261.748.271.490"],
    ]
    for row, values in enumerate(grid):
        for col, value in enumerate(values):
            if value is None:
                continue
            cell_id = f"flat_{row}_{col}"
            records.append(dict(common, kind="table_cell", table_id="table_001", cell_id=cell_id,
                                row=row, col=col, text=value, bbox=table_box,
                                source_ref=f"AAA/v1/p12/table_001/{cell_id}"))
    records[2]["cell_count"] = len(records) - 3
    manifest["record_counts"] = dict(Counter(r["kind"] for r in records))
    result = norm.FinancialNormalizer().normalize(manifest, records, page=12, table_id="table_001")
    assert len(result["facts"]) == 6
    current, previous = result["facts"][:2]
    assert current["value"] == "468755201520" and current["col"] == 3
    assert previous["value"] == "390345691206" and previous["col"] == 4
    assert previous["restatement_status"] == "needs_review"
    assert all("DEGENERATE_BBOX_LAYOUT_REQUIRES_REVIEW" in f["normalization_issues"]
               for f in result["facts"])


def test_separate_row_ordinal_collision_retains_label_and_both_refs(tmp_path):
    _, manifest, records = sample(tmp_path)
    common = {k: records[0][k] for k in ("document_id", "document_version", "access_scope",
                                           "page", "page_image_ref", "ocr_json_ref", "ocr_confidence")}
    for row in (1, 2):
        label = next(r for r in records if r["kind"] == "table_cell" and r["row"] == row and r["col"] == 0)
        label["bbox"][0] = 80
        cell_id = f"ordinal_{row}"
        records.append(dict(common, kind="table_cell", table_id="table_001", cell_id=cell_id,
                            row=row, col=0, text=f"{row}.", bbox=[5, label["bbox"][1], 50, label["bbox"][3]],
                            source_ref=f"AAA/v1/p12/table_001/{cell_id}"))
    records[2]["cell_count"] += 2
    manifest["record_counts"] = dict(Counter(r["kind"] for r in records))
    result = norm.FinancialNormalizer().normalize(manifest, records, page=12, table_id="table_001")
    assert result["facts"][0]["metric_code"] == "revenue_net"
    assert result["facts"][0]["context_refs"]["row_ordinal"] == ["AAA/v1/p12/table_001/ordinal_1"]
    assert all("ROW_ORDINAL_COLLISION_REQUIRES_REVIEW" in f["normalization_issues"]
               for f in result["facts"])
    records[-1]["bbox"][2] = 120  # An overlapping duplicate no longer has an unambiguous label.
    with pytest.raises(norm.NormalizationError, match="Duplicate row/col"):
        norm.FinancialNormalizer().normalize(manifest, records, page=12, table_id="table_001")


def test_numbered_rows_align_header_by_geometry_and_keep_restatement(tmp_path):
    _, manifest, records = sample(tmp_path)
    records[:] = records[:3]
    records[2]["bbox"] = [200, 760, 2300, 1900]
    common = {k: records[0][k] for k in ("document_id", "document_version", "access_scope",
                                           "page", "page_image_ref", "ocr_json_ref", "ocr_confidence")}
    header = ["CHỈ TIÊU", "Mã số", "Thuyết\nminh", "Năm nay", "Năm trước\n(Trình bày lại)"]
    header_bounds = [(428, 606), (1175, 1262), (1311, 1428), (1678, 1848), (2044, 2281)]
    data = ["1.", "Doanh thu bán hàng và cung cấp dịch vụ", "01", "25",
            "3.231.367.429.906", "2.979.817.834.159"]
    data_bounds = [(341, 371), (428, 1126), (1190, 1244), (1338, 1383),
                   (1519, 1848), (1952, 2279)]
    for row, values, bounds in ((0, header, header_bounds), (1, data, data_bounds)):
        for col, value in enumerate(values):
            cell_id = f"shifted_{row}_{col}"
            records.append(dict(common, kind="table_cell", table_id="table_001", cell_id=cell_id,
                                row=row, col=col, text=value,
                                bbox=[bounds[col][0], 778 + row * 60, bounds[col][1], 820 + row * 60],
                                source_ref=f"AAA/v1/p12/table_001/{cell_id}"))
    records[2]["cell_count"] = 11
    manifest["record_counts"] = dict(Counter(r["kind"] for r in records))
    for page in manifest["pages"]:
        page.update(width=2500, height=3505)
    result = norm.FinancialNormalizer().normalize(manifest, records, page=12, table_id="table_001")
    current, previous = result["facts"]
    assert current["metric_code"] == "revenue_gross" and current["col"] == 4
    assert previous["col"] == 5 and previous["restatement_status"] == "needs_review"
    assert "RESTATEMENT_REQUIRES_REVIEW" in previous["normalization_issues"]


def test_split_external_headers_and_standalone_vnd(tmp_path):
    _, manifest, records = sample(tmp_path)
    records[:] = records[:3]
    records[0]["text"] = "năm tài chính kết thúc ngày 31/12/2023"
    records[1]["text"] = "Báo cáo tài chính"
    records[2]["bbox"] = [346, 740, 2300, 2000]
    common = {k: records[0][k] for k in ("document_id", "document_version", "access_scope",
                                           "page", "page_image_ref", "ocr_json_ref", "ocr_confidence")}
    blocks = [
        ("ma", "Mã", [391, 638, 443, 676]), ("so", "số", [396, 676, 438, 712]),
        ("label", "CHỈ TIÊU", [836, 638, 1002, 673]),
        ("current", "Năm 2023", [1621, 641, 1789, 673]),
        ("current_unit", "VND", [1732, 683, 1791, 715]),
        ("previous", "Năm 2022", [2088, 641, 2259, 673]),
        ("previous_unit", "VND", [2200, 683, 2259, 715]),
    ]
    for element_id, text, bbox in blocks:
        records.append(dict(common, kind="ocr_block", element_id=element_id,
                            source_ref=f"AAA/v1/p12/{element_id}", text=text, bbox=bbox))
    grid = [
        ["01", "1.", "Doanh thu bán hàng và cung cấp dịch vụ", "19", "529.806.369.062", "468.755.201.520"],
        ["10", "2.", "Doanh thu thuần về bán hàng và cung cấp dịch vụ", "19", "529.806.369.062", "468.747.514.020"],
        ["11", "3.", "Giá vốn hàng bán", "19", "300.000.000.000", "290.000.000.000"],
    ]
    bounds = [(381, 428), (470, 497), (539, 1153), (1371, 1413), (1524, 1789), (1987, 2259)]
    for row, values in enumerate(grid):
        for col, value in enumerate(values):
            cell_id = f"split_{row}_{col}"
            records.append(dict(common, kind="table_cell", table_id="table_001", cell_id=cell_id,
                                row=row, col=col, text=value,
                                bbox=[bounds[col][0], 810 + row * 150, bounds[col][1], 842 + row * 150],
                                source_ref=f"AAA/v1/p12/table_001/{cell_id}"))
    records[2]["cell_count"] = 18
    manifest["record_counts"] = dict(Counter(r["kind"] for r in records))
    for page in manifest["pages"]:
        page.update(width=2500, height=3505)
    facts = norm.FinancialNormalizer().normalize(manifest, records, page=12, table_id="table_001")["facts"]
    assert len(facts) == 6
    assert facts[0]["period_end"] == "2023-12-31" and facts[0]["raw_unit"] == "VND"
    assert facts[0]["context_refs"]["unit"] == ["AAA/v1/p12/current_unit"]
    assert "AAA/v1/p12/so" in facts[0]["context_refs"]["layout"]
    assert facts[1]["context_refs"]["unit"] == ["AAA/v1/p12/previous_unit"]
    assert all("EXTERNAL_HEADER_LAYOUT_REQUIRES_REVIEW" in f["normalization_issues"] for f in facts)


def test_multiline_table_header_preserves_restatement_and_fragment_refs(tmp_path):
    _, manifest, records = sample(tmp_path)
    records[:] = records[:3]
    records[0]["text"] = "Cho năm tài chính kết thúc ngày 31/12/2024"
    records[1]["text"] = "Báo cáo tài chính"
    records[2]["bbox"] = [353, 663, 2300, 1900]
    common = {k: records[0][k] for k in ("document_id", "document_version", "access_scope",
                                           "page", "page_image_ref", "ocr_json_ref", "ocr_confidence")}
    rows = [
        (0, ["Chỉ tiêu", "Mã", "Thuyết", "Năm 2024", "Năm 2023"],
         [(358, 506), (1257, 1319), (1373, 1480), (1662, 1796), (2068, 2201)]),
        (1, [None, "số", "minh", "VND", "(Trình bày lại)"],
         [None, (1265, 1312), (1376, 1460), (1732, 1801), (1976, 2270)]),
        (2, [None, None, None, None, "VND"],
         [None, None, None, None, (2194, 2265)]),
        (3, ["Doanh thu bán hàng và cung cấp dịch vụ", "01", "24",
             "561.464.712.365", "529.806.369.062"],
         [(358, 1181), (1265, 1312), (1388, 1440), (1554, 1868), (1954, 2265)]),
    ]
    for row, values, bounds in rows:
        for col, value in enumerate(values):
            if value is None:
                continue
            cell_id = f"multiline_{row}_{col}"
            y = {0: 667, 1: 741, 2: 808, 3: 882}[row]
            records.append(dict(common, kind="table_cell", table_id="table_001", cell_id=cell_id,
                                row=row, col=col, text=value,
                                bbox=[bounds[col][0], y, bounds[col][1], y + 35],
                                source_ref=f"AAA/v1/p12/table_001/{cell_id}"))
    records[2]["cell_count"] = len(records) - 3
    manifest["record_counts"] = dict(Counter(r["kind"] for r in records))
    for page in manifest["pages"]:
        page.update(width=2500, height=3505)
    current, previous = norm.FinancialNormalizer().normalize(
        manifest, records, page=12, table_id="table_001")["facts"]
    assert current["metric_code"] == "revenue_gross" and current["raw_unit"] == "VND"
    assert current["context_refs"]["unit"] == ["AAA/v1/p12/table_001/multiline_1_3"]
    assert len(previous["context_refs"]["column_header"]) == 3
    assert previous["restatement_status"] == "needs_review"
    assert "SPLIT_HEADER_LAYOUT_REQUIRES_REVIEW" in previous["normalization_issues"]


def test_combined_code_note_header_uses_corroborated_code_column(tmp_path):
    _, manifest, records = sample(tmp_path)
    records[:] = records[:3]
    records[0]["text"] = "Cho năm tài chính kết thúc ngày 31/12/2021"
    records[1]["text"] = "Đơn vị tính: VND"
    records[1]["bbox"] = [1997, 424, 2297, 463]
    records[2]["bbox"] = [330, 459, 2300, 1900]
    common = {k: records[0][k] for k in ("document_id", "document_version", "access_scope",
                                           "page", "page_image_ref", "ocr_json_ref", "ocr_confidence")}
    rows = [
        (0, ["CHỈ TIÊU", "Mã Thuyết", "Năm 2021", "Năm 2020"],
         [(421, 573), (1315, 1517), (1711, 1891), (2096, 2275)]),
        (1, [None, "số minh"], [None, (1322, 1497)]),
        (2, ["Doanh thu bán hàng và cung cấp dịch vụ", "01", "18",
             "264.024.739.528", "249.367.794.420"],
         [(337, 1278), (1322, 1371), (1455, 1502), (1591, 1913), (1975, 2297)]),
        (3, ["Doanh thu thuần về bán hàng và cung cấp dịch vụ", "10", "18",
             "264.024.739.528", "249.367.794.420"],
         [(337, 1278), (1322, 1371), (1455, 1502), (1591, 1913), (1975, 2297)]),
        (4, ["Giá vốn hàng bán", "11", "19", "200.000.000.000", "190.000.000.000"],
         [(337, 1278), (1322, 1371), (1455, 1502), (1591, 1913), (1975, 2297)]),
    ]
    for row, values, bounds in rows:
        for col, value in enumerate(values):
            if value is None:
                continue
            cell_id = f"combined_{row}_{col}"
            y = {0: 480, 1: 536, 2: 645, 3: 745, 4: 845}[row]
            records.append(dict(common, kind="table_cell", table_id="table_001", cell_id=cell_id,
                                row=row, col=col, text=value,
                                bbox=[bounds[col][0], y, bounds[col][1], y + 35],
                                source_ref=f"AAA/v1/p12/table_001/{cell_id}"))
    records[2]["cell_count"] = len(records) - 3
    manifest["record_counts"] = dict(Counter(r["kind"] for r in records))
    for page in manifest["pages"]:
        page.update(width=2500, height=3505)
    facts = norm.FinancialNormalizer().normalize(manifest, records, page=12, table_id="table_001")["facts"]
    assert len(facts) == 6 and facts[0]["statement_code"] == "01"
    assert facts[0]["raw_unit"] == "VND" and facts[0]["col"] == 3
    assert facts[0]["context_refs"]["statement_code"] == ["AAA/v1/p12/table_001/combined_2_1"]
    assert all("SPLIT_HEADER_LAYOUT_REQUIRES_REVIEW" in f["normalization_issues"] for f in facts)


def test_large_values_scale_without_decimal_rounding(tmp_path):
    _, manifest, records = sample(tmp_path)
    raw = "1234567890123456789012345678901234567890"
    records[9]["text"] = raw
    fact = norm.FinancialNormalizer().normalize(manifest, records, page=12, table_id="table_001", context=context())["facts"][0]
    assert fact["value"] == raw + "000000"


def test_ambiguous_unit_cannot_be_silently_overridden(tmp_path):
    _, manifest, records = sample(tmp_path)
    records[1]["text"] = "Đơn vị: trăm VND"
    fact = norm.FinancialNormalizer().normalize(manifest, records, page=12, table_id="table_001",
        context=context(unit_override="VND", unit_evidence="Unverified assumption"))["facts"][0]
    assert fact["value"] is None
    assert "AMBIGUOUS_UNIT_DECLARATION" in fact["normalization_issues"]


def test_writer_is_versioned_and_preserves_artifacts(tmp_path):
    folder, _, _ = sample(tmp_path)
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    first = norm.normalize_financial_table(folder, page=12, table_id="table_001", access_scope="tenant-a", context=context())
    second = norm.normalize_financial_table(folder, page=12, table_id="table_001", access_scope="tenant-a", context=context())
    assert first["output_dir"] != second["output_dir"]
    assert first["fact_count"] == 4 and first["input_artifact_sha256"] == second["input_artifact_sha256"]
    facts1 = (Path(first["output_dir"]) / "financial_facts.jsonl").read_bytes()
    assert facts1 == (Path(second["output_dir"]) / "financial_facts.jsonl").read_bytes()
    for filename, raw in before.items():
        assert (folder / filename).read_bytes() == raw


def test_scope_denied_before_records_access(tmp_path, monkeypatch):
    folder, _, _ = sample(tmp_path)
    original = Path.read_bytes
    def guard(path):
        assert path.name != "elements.jsonl"
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", guard)
    with pytest.raises(PermissionError):
        norm.normalize_financial_table(folder, page=12, table_id="table_001", access_scope="tenant-b")
    assert not (folder / "normalization").exists()


def test_unsupported_or_duplicate_layout_does_not_publish(tmp_path):
    folder, manifest, records = sample(tmp_path)
    records[2]["name"] = "Thuyết minh khác"
    save(folder, manifest, records)
    with pytest.raises(norm.NormalizationError):
        norm.normalize_financial_table(folder, page=12, table_id="table_001", access_scope="tenant-a")
    assert not (folder / "normalization").exists()


def test_partial_write_does_not_publish_run(tmp_path, monkeypatch):
    folder, _, _ = sample(tmp_path)
    original = Path.write_text
    def fail(path, *args, **kwargs):
        if path.name == "normalization_report.json":
            raise OSError("simulated failure")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "write_text", fail)
    with pytest.raises(OSError):
        norm.normalize_financial_table(folder, page=12, table_id="table_001", access_scope="tenant-a")
    assert list((folder / "normalization").iterdir()) == []


def test_bts_imported_table_when_available(tmp_path):
    original = Path(__file__).resolve().parents[2] / "artifacts/module_01/BTS-2025"
    if not (original / "manifest.json").is_file():
        pytest.skip("Imported BTS bundle not available locally")
    folder = tmp_path / "bts"
    folder.mkdir()
    for filename in ("manifest.json", "elements.jsonl"):
        (folder / filename).write_bytes((original / filename).read_bytes())
    result = norm.normalize_financial_table(folder, page=12, table_id="table_001", access_scope="demo-scope",
        context=norm.NormalizationContext(company_id="BTS", company_evidence="Explicit sample selection"))
    facts = [json.loads(s) for s in (Path(result["output_dir"]) / "financial_facts.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(facts) == 36
    by_key = {(f["statement_code"], f["column_role"]): f for f in facts}
    assert by_key["01", "current"]["value"] == "2818277552061"
    assert by_key["10", "current"]["value"] == "2656580390998"
    assert by_key["20", "previous"]["value"] == "-7501350091"
    assert by_key["51", "current"]["value"] is None
    assert by_key["70", "current"]["value"] is None
    assert all(f["consolidation_scope"] is None for f in facts)


def test_colab_normalization_cells_use_current_api(tmp_path):
    folder, _, _ = sample(tmp_path)
    notebook = Path(__file__).resolve().parents[2] / "colab/02_financial_normalization.ipynb"
    cells = json.loads(notebook.read_text(encoding="utf-8"))["cells"]
    for cell in cells:
        if cell["cell_type"] == "code":
            compile("".join(cell["source"]), str(notebook), "exec")
    environment = dict(normalization=norm, bundle_dir=folder, PAGE=12, TABLE_ID="table_001",
                       ACCESS_SCOPE="tenant-a", context=context(), Path=Path)
    exec("".join(cells[9]["source"]), environment)
    exec("".join(cells[11]["source"]), environment)
    assert len(environment["facts"]) == 4
    assert environment["facts"][0]["value"] == "1234000000"
