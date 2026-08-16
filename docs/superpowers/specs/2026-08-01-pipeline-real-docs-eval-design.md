# Pipeline Real-Docs Evaluation — Design

**Date:** 2026-08-01  
**Status:** Approved (Approach B + live monitor + sample forensics)  
**Context:** The document processing pipeline (`scripts/process.py`) is slow and runs against real files in `raw/`. Unit tests cover isolated logic, but they do not tell us whether the pipeline actually works on the user's real documents. This design adds a lightweight, repeatable evaluation layer that runs on the same real artifacts the pipeline already produces.

## Goals

1. **Coverage:** Confirm every file in `raw/` ends in a known terminal state with no silent drops.
2. **Output health:** Validate that `processed_md/` files are structurally sound and internally consistent.
3. **Review signal:** Measure how many files stop for human review and why.
4. **Live visibility:** Show progress while a long pipeline run is in flight.
5. **Forensics:** For any sampled failure, expose every stage version so the root cause can be traced without re-running the file.

## Non-goals

- This is **not** a unit-test replacement; it is an integration/observability harness.
- It does not judge semantic quality of translation or classification on its own; it flags candidates for human review.
- It does not modify the pipeline, the database, or any generated files.

---

## Components

### 1. `scripts/eval_pipeline.py` — post-run evaluator
A standalone, read-only script invoked after `python scripts/process.py` finishes.

**Inputs**
- `raw/` — source file list and hashes.
- `pipeline.db` — `files`, `stage_outputs`, and `review_queue` tables.
- `processed_md/` — generated Markdown + frontmatter.
- `instructions/subdomains.md` and `instructions/document_types.md` — allowed category lists.

**Outputs**
- `eval_reports/eval_report_YYYYMMDD_HHMMSS.md`
- Exit code: `0` if no critical invariants fail, `1` otherwise.

### 2. `scripts/watch_pipeline.py` — live monitor
A small read-only watcher run in a second terminal while the pipeline runs.

**Behavior**
- Polls `pipeline.db` every `N` seconds (default 5).
- Renders a live status board:
  - total raw files,
  - counts per status (`pending`, `processed`, `filtered`, `error`, `needs_review`, `skipped`),
  - currently active file (last updated `files` row),
  - pending review count,
  - error count,
  - elapsed time and rough ETA based on throughput.
- Exits when all files reach a terminal state or on Ctrl-C.

**Safety**
- Uses the same read-only DB access as the evaluator.
- Does not write to the database, filesystem, or stdout of the pipeline process.

---

## Invariant checks

### Coverage
- Every file under `raw/` has a row in the `files` table.
- The sum of terminal-status counts equals the number of raw files.
- No file remains `pending` after a run completes.
- Every `processed` file has a matching `processed_md/<rel_path>.md`.

### Output health (for `processed` files)
- Valid YAML frontmatter containing all required keys:
  `original_path`, `file_hash`, `subdomain`, `document_type`, `truthness_score`, `truthness_justification`, `language`, `model`.
- `subdomain` and `document_type` are in the allowed lists parsed from `instructions/subdomains.md` and `instructions/document_types.md`.
- `truthness_score` is numeric and within `0–10`.
- Body is non-empty after the frontmatter.
- `file_hash` in the frontmatter matches the current hash of the source file at `original_path` (detects stale outputs).

### Review-queue signal
- Count pending reviews by stage and trigger type.
- Compute review rate: `needs_review / total`.
- Flag trigger types that dominate unexpectedly (configurable threshold, default 25%).
- Report stale review items separately; they are expected when source files change but should not accumulate.

### Error consistency
- Every `error` status has a non-empty `error_message`.
- No unexpected exceptions are missing from the database.

---

## Sample forensics

For each sampled file the report includes a **File history** section:

1. Raw source path and current SHA-256 hash.
2. Per-stage outputs from `stage_outputs`:
   - `docling` — extracted Markdown,
   - `filtering` — `true` / `false`,
   - `translation` — translated text or skip note,
   - `subdomain` / `doc_type` — LLM classification result,
   - `truthness` — raw LLM response plus parsed `score` / `justification`.
3. Final `processed_md/` frontmatter and body.
4. Associated `review/*.md` file, if any.
5. Check failure annotations tied to the stage/version that caused them.

This makes regressions traceable to a specific stage (e.g. “translation dropped a paragraph” or “truthness parsed as score 0”) without re-running the slow pipeline.

---

## Sampling

- Random sample of `processed` files (default 5, configurable).
- Optional: include all `error` and `needs_review` files automatically.
- For each sampled file, emit raw path, processed path, and a link/anchor to its forensic section.

## Reporting

Each report contains:

1. **Executive summary** — total files, pass/fail counts, review rate, elapsed wall time.
2. **Coverage table** — counts per status.
3. **Check results** — one row per invariant; PASS / FAIL / WARN.
4. **Review queue breakdown** — pending items by stage and trigger.
5. **Sample spot-check list** — files selected for human review.
6. **Forensic details** — per-sample stage outputs and failure annotations.

## Exit codes

- `0` — all critical invariants pass; warnings may be present.
- `1` — at least one critical invariant failed (coverage mismatch, missing required frontmatter key, unknown category, score out of range, file-hash mismatch).

Warnings (high review rate, stale items, short empty body) are reported but do not change the exit code.

---

## Future extensions (out of scope for first version)

- Persist historical reports and trend review rate / error rate across runs.
- Add lightweight semantic checks (e.g. detect untranslated Hebrew in a file marked as translated).
- Compare golden-set outputs between pipeline versions.
