# Project Context — Vietnamese Financial Multi-Agent System

## 1. What we're building

**Project title (target architecture):** Regulation- and Provenance-Constrained Adaptive Multi-Agent Framework for Vietnamese Financial Statement Validation, Interactive Q&A, and Grounded Report Generation.

A system for Vietnamese annual reports, financial statements, quarterly reports, notes to financial statements, and optionally legislation, circulars, accounting standards, disclosure regulations and internal/reference policies. A user can upload **multiple PDFs**, ask follow-up questions in Vietnamese across multiple tickers, years and documents, request grounded financial analysis, compliance findings, charts, or a report with page/article citations.

We currently have been preparing/structuring our own OCR dataset of Vietnamese company reports. **Do not assume system components are already coded.** This is the target design for a shared internal engineering reference, not an academic thesis document.

## 2. Why this design

Prevent OCR/numerical errors from propagating; normalize Vietnamese accounting values and labels across periods and companies; distinguish financial risk from legal noncompliance; trace claims to original PDF pages and applicable regulatory provisions; allow an answer to abstain when evidence is insufficient. Use a multi-agent supervisor to route only needed specialists with tool-backed calculation and evidence.

## 3. Target Level-0 architecture (see reference PNG)

Input: financial PDF uploads; optional regulatory text and validation profile; user questions and additional uploads.

**Offline flow:** document/regulatory management → Vietnamese OCR & document/table/layout parsing → OCR quality check, cleaning, accounting normalization, legal text normalization → financial structuring, rule extraction and indexing → **C1** contextual compliance validation → knowledge storage.

**Storage/shared state:** original PDF & legal text storage with version/ACL; structured financial DB; vector DB; rule registry with effective dates and exceptions; **C2** regulation-aware executable financial evidence graph linking facts, derived metrics, claims, rules, citations and verification state.

**Online flow:** user query/conversation resolution + evidence-need analysis → document/company/rule selection + permissions → retrieval and shared tool space → **C3** supervisor/planner coordinates specialized agents → shared state/evidence graph → **C4** claim-level risk-adaptive verification → verified claims/findings ledger → distinct outputs: compliance findings, interactive answers, grounded financial reports. Enforce information firewall: output uses verified claims/findings only.

**C5:** evaluation/ablation, logging, trace, monitoring, user feedback, rule/model/index maintenance.

## 4. Key components shown in agreed reference

- Ingestion: file validation, metadata, duplicate detection, version tracking, raw PDF persistence; legal clause parsing.
- Processing: page, chunk, table and cell source references; accounting normalization of unit / period / scope / sign; OCR quality and accounting consistency checks remain distinct.
- C1: structural validation, accounting consistency, disclosure compliance, cross-document consistency, violation detection, dual-provenance grounding.
- C2: financial facts, derived metrics, compliance rules, violations/findings, claims, citations, verification state; relation edges with original financial **and** regulatory references.
- Query: query understanding, intent, conversation state, evidence needs, task decomposition, scoped selection, planning/tool routing.
- Shared tools: typed metrics/fact retrieval, graph traversal, semantic note/table retrieval, deterministic calculator/validators, optional Text2SQL.
- C3: evidence/risk/compliance-aware supervisor; structured data, regulatory compliance, narrative analysis, calculation, profitability, liquidity/leverage, cash-flow/earnings-quality, evidence resolution and optional agents; shared context/memory, bounded iterations and re-investigation.
- C4: risk scoring, evidence/source checking, numerical checks, legal applicability, financial logic critique, claim support; ACCEPT / REVISE / INVESTIGATE / ABSTAIN; verified finding/claim ledger.
- Outputs: compliance findings (violations/warnings/unknown), interactive QA with evidence, financial analysis report with tables/charts and citations; follow-up queries.
- C5: compliance QA/report benchmarks, controlled C1–C4 ablations, trace logs, performance and cost monitoring.

## 5. Important correctness rules

1. A financially risky condition (e.g. negative operating cash flow) is **not automatically a legal violation**.
2. Regulatory applicability is company-specific, rule-specific, period-specific, and exception-aware; always check effective dates.
3. Retrieval must enforce ACL/tenant permissions for chunks, SQL rows, graph nodes/edges, rule/doc metadata as applicable; raw-storage ACL alone is insufficient.
4. Raw PDF originals persist for re-OCR, audit, citation opening, updates and debugging. Reprocessing may invalidate downstream indices/claims.
5. Source evidence (PDF/page/table/cell, regulation/article) is created at ingestion; runtime claims and verification status are created during analysis, even if represented together in C2.
6. Use Python/deterministic math for ratios and consistency; LLMs may interpret, route and critique but should not be trusted as calculators.
7. `needs_review`, `unknown`, and `abstain` are legitimate states, not failures to hide.
8. Multi-document, multi-company and multi-period selection must avoid cross-contamination.
9. Separate the **offline preparation** from **online question answering**, but online uploads must route back through offline ingestion before documents become searchable.

## 6. Current development constraints and unresolved choices

- Primary prototyping environment: **Google Colab**, so resource limits and session resets matter.
- Potential choices, not final: Qwen LLM (version/model size TBD); SQLite structured database; FAISS/Chroma vector index; Google Drive or object storage for originals; NetworkX or suitable graph store for C2; LangGraph or Python orchestrator; Python/Pandas/NumPy for computations.
- OCR pipeline already being developed independently; exact engine, image/table extraction formats and quality thresholds TBD.
- Exact GPU, runtime memory, inference mode, LLM access, number of PDFs/tickers, APIs/web UI, regulations in scope, data isolation, performance target, metric ontology and benchmark construction TBD.
- Fine-tuning is **not required for the initial design**; prioritize prompting, agent logic, skills/tools, retrieval and deterministic checks. Later fine-tuning may be evaluated if specific gaps persist.

## 7. Documentation deliverable

**Not** a Word document. Produce internal Markdown docs from overall to detailed. Suggested:

- `docs/00-overview.md` — Level 0 with separate offline/online flows and C1–C5 map.
- `docs/01-offline-ingestion.md` — Level 1 ingestion/OCR/normalization/C1.
- `docs/02-knowledge-and-graph.md` — storage, permissions, versioning and C2.
- `docs/03-query-and-retrieval.md` — query/conversation/router/retrieval/tools.
- `docs/04-multi-agent.md` — C3 supervisor, all agents, coordination, contracts.
- `docs/05-verification-and-output.md` — C4, ledger, firewall, outputs.
- `docs/06-evaluation-monitoring.md` — C5, metrics and ablation.
- `docs/07-implementation.md` — data schemas, repo modules, staged development, TBDs.

Style: technical, readable, concise, lots of whitespace; separate large diagrams rather than cramming everything into Level 0. The reference PNG is the **agreed source diagram**, not proof of implementations or results.
