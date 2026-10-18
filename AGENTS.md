# Instructions for Codex

Read `context/PROJECT_CONTEXT.md`, `context/ARCHITECTURE_DECISIONS.md`, and inspect `context/architecture-reference.png` before creating or editing documentation.

Goal: collaboratively document, not silently redesign, the Vietnamese financial reporting multi-agent pipeline. Preserve the full C1–C5 architecture from the reference image, including compliance validation. Do not substitute a reduced MVP for the agreed target design. Distinguish the target architecture from implementation status and from possible staged rollout.

Write Vietnamese, retaining English technical names on first use when useful. Documentation is for internal developer collaboration, not a thesis, literature survey, or formal Word report. Organize top-down: Level 0 system, Level 1 subsystems, Level 2 components and data contracts. Begin each subsystem with flow and purpose, then input/output, processing, dependencies, failure paths, and tech options.

Prefer spacious, legible Mermaid diagrams in Markdown; split dense diagrams into Level 1/2. Use short node text and avoid a single giant Mermaid flowchart. Do not invent implemented features, measured results, model/GPU details, or regulatory rules. Mark unknown decisions as `TBD` and assumptions as `Giả định`. Include permission enforcement at retrieval and database layers, document/rule versioning, abstention, and provenance. Ask before changing architecture scope.

For new documents place output under `docs/`. Use numbered files. Maintain links between parent and child documents. When a diagram is ambiguous, prioritize `context/ARCHITECTURE_DECISIONS.md` and flag the mismatch instead of guessing.
