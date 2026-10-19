"""Small synthetic adapter snapshots; no PNG downloads or real laws required."""
from collections import Counter
import json
from pathlib import Path
import pytest

from financial_multiagent.ingestion.financial_normalization import NormalizationContext, normalize_financial_table
from financial_multiagent.validation.c1 import C1Input


@pytest.fixture
def normalized_input(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    common = dict(document_id="DOC-A", document_version="v1", access_scope="tenant-a", page=1,
                  page_image_ref="Financial_OCR/AAA/2025/page_1.png",
                  ocr_json_ref="Financial_Report_OCR/AAA/2025/ocr_json/page_1.json", ocr_confidence=None)
    records = []
    for key, text, box in (("date", "Cho năm tài chính kết thúc ngày 31 tháng 12 năm 2025", [0, 1, 950, 10]),
                           ("unit", "Đơn vị: VND", [0, 20, 950, 30])):
        records.append(dict(common, kind="ocr_block", element_id=key, source_ref=f"DOC-A/v1/p1/{key}", text=text, bbox=box))
    records.append(dict(common, kind="table", element_id="table_1", source_ref="DOC-A/v1/p1/table_1",
        name="BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH", text=None, bbox=[0, 50, 999, 900], cell_count=16))
    rows = [["CHỈ TIÊU", "Mã số", "Năm nay", "Năm trước"],
            ["Doanh thu bán hàng", "01", "1.000", "1.000"],
            ["Các khoản giảm trừ doanh thu", "02", "100", "100"],
            ["Doanh thu thuần", "10", "900", "900"]]
    for row, values in enumerate(rows):
        for col, text in enumerate(values):
            cell = f"cell_{row}_{col}"
            records.append(dict(common, kind="table_cell", table_id="table_1", cell_id=cell,
                row=row, col=col, text=text, bbox=[1 + col * 200, 51 + row * 100, 190 + col * 200, 100 + row * 100],
                source_ref=f"DOC-A/v1/p1/table_1/{cell}"))
    manifest = dict(document_id="DOC-A", document_version="v1", access_scope="tenant-a", symbol="AAA",
        source_bundle_sha256="a" * 64, record_counts=dict(Counter(r["kind"] for r in records)), issues=[],
        pages=[dict(page_number=1, width=1000, height=1000, page_image_ref=common["page_image_ref"], ocr_json_ref=common["ocr_json_ref"])])
    (bundle / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    (bundle / "elements.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
    context = NormalizationContext(company_id="AAA", company_evidence="Synthetic company", consolidation_scope="standalone",
        scope_evidence="Synthetic scope", accounting_basis="VAS", basis_evidence="Synthetic basis",
        period_overrides={2: dict(start="2025-01-01", end="2025-12-31", evidence="Synthetic calendar"),
                          3: dict(start="2024-01-01", end="2024-12-31", evidence="Synthetic calendar")})
    result = normalize_financial_table(bundle, page=1, table_id="table_1", access_scope="tenant-a", context=context)
    return C1Input(bundle, Path(result["output_dir"]))
