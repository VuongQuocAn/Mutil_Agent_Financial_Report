# Agreed architecture decisions (preserve unless user changes)

| ID | Decision | Rationale |
|---|---|---|
| D01 | Keep the latest full **C1–C5** architecture, including compliance validation | User explicitly wants no simplification or removal |
| D02 | Organize Markdown **top-down Level 0 → Level 1 → Level 2** | Internal group understanding and implementation |
| D03 | Offline and online are separate but connected paths | Bulk pre-processing vs interactive queries |
| D04 | Preserve original PDFs, legal documents, metadata, hashes/versions and ACL | Reprocess, provenance, audit and updates |
| D05 | Separate OCR quality check from deterministic accounting validation | Extraction quality != financial consistency |
| D06 | Normalize locale numbers, currencies/units, fiscal periods, consolidation scopes, restatements and VAS/IFRS metric mappings | Correct ratios and comparisons |
| D07 | Store raw docs, structured facts, vector chunks, rule registry and shared evidence graph | Each serves different evidence/retrieval use cases |
| D08 | Source evidence exists at ingestion; runtime claims/findings verified separately | Avoid mixing provenance and agent inference |
| D09 | Retrieval/query/tools enforce permissions, not just raw PDF store | Avoid cross-user leakage |
| D10 | C1 checks regulatory applicability, exceptions and effective dates | Avoid false compliance violations |
| D11 | C2 is an **executable** and provenance-aware graph, not only semantic links | Reproducible calculations/rule validations |
| D12 | C3 supervisor chooses and coordinates specialized agents adaptively, with bounded retries | Flexible workflow and cost control |
| D13 | Tool-backed deterministic calculations and validators; optional Text2SQL fallback | Correctness and transparency |
| D14 | C4 makes decisions ACCEPT / REVISE / INVESTIGATE / ABSTAIN at claim level | Safe handling of uncertainty |
| D15 | Outputs are separated into compliance, interactive QA, and financial reports | Distinct use cases |
| D16 | Outputs respect information firewall: verified ledger only | Prevent unsupported statements |
| D17 | C5 uses evaluation, logs, user feedback and ablations of C1–C4 | Measurable contribution |
| D18 | Colab-first prototype; exact technical choices not yet finalized | Do not invent resource commitments |

## Non-negotiable distinction

`Financial insight` ≠ `Risk warning` ≠ `Compliance finding`. A risk warning needs financial support; a compliance finding additionally needs a specifically applicable cited regulation and evidence of the actual rule violation or unknown status.

## Things to flag as TBD

What counts as a regulation source, legal domain scope, upload access policy, quantitative thresholds, verification rubric, chosen OCR engine, model/VRAM, graph execution approach and interface.
