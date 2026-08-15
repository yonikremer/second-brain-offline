# Client Air-Gap Ingest Pipeline (Stages 4–8) — Design

**Date:** 2026-08-02
**Status:** Backbone approved (Approach C, revised). Per-stage deep dives (filtering,
translation, classification, ingest strategy, wiki schema) follow as separate specs.
**Context:** Scaling the vault to a real client team: a 3800+ page Hebrew Confluence
space (exported → PDF → Markdown) plus file-share documents, ingested into an
English-language air-gapped vault. Acquisition stages 1–3 (Confluence export, file-share
copy, docling conversion into `raw_md/`) are out of scope here and assumed working.

## Constraints (load-bearing)

- **In-gap models:** MiniMax M2.7 (general reasoning; "barely capable" — makes narrow,
  closed-choice decisions against rubrics, never free-forms) and **Dicta-LM 3.0 24B**, a
  Hebrew-specialized model available on the same OpenAI-compatible API as `qmd-api` and
  used for all Hebrew-side work. Scripts do all mechanical work. Per-task autonomy is
  set empirically by Phase-0 calibration.
- **Language:** Hebrew source → English vault. MiniMax is weak in Hebrew, strong in
  English. Pre-translation stages must lean on language-light cues; full-document
  reasoning happens only on English text.
- **Humans:** The domain expert is embedded (hours per day). Generous review gates are
  affordable and are the safety net for the weak model.
- **Locus:** The process is designed outside the gap (with Claude), executed inside by
  scripts + MiniMax + humans. Every artifact the process needs must ship in the gap build.
- **Corpus:** ~20 GB raw PDFs, ~2 GB as Markdown, doubling again after translation.
  Bulk content cannot live in git.

## Architecture — Approach C: ledger-driven domain campaigns

Corpus-wide work is limited to cheap, language-light filtering and coarse domain
routing. Everything expensive runs as per-domain **campaigns** in dependency order.
Per campaign the order is **translate → classify → ingest**: translation (with the
expert) builds the domain glossary; classification then runs on English text with the
glossary and a frozen taxonomy — MiniMax entirely in its strong language.

### The three axes become metadata

Every document carries the three corpus axes as frontmatter/ledger fields, written as
the pipeline learns them:

| Axis | Field(s) | Written at |
|------|----------|-----------|
| Domain | `domains:` (multi-valued) | Coarse at Phase 1, refined per campaign |
| Genre / trust | `doc_type:`, `trust:` (tier from campaign plan) | Classification |
| Knowledge level | `level:` (foundation → advanced) | Kickoff triage, refined at classification |

Plus provenance: per-check results, `doc_decision` + `decided_by` (rule / model /
human) on every doc, translation status, ingest status.

### Phase 0 — Calibration (once, at campaign-program start)

- Stratified gold sample (~100 docs) labeled by the expert: relevant?, domain,
  doc type, trust tier.
- MiniMax runs the same tasks; agreement rates set its **autonomy dial** per task
  (e.g., trusted to auto-score relevance, but classification always spot-checked).
- Also validates gist-translation quality (titles/excerpts) empirically.
- The gold sample becomes the pipeline's standing regression eval, extending the
  existing `eval/` pattern.

### Phase 1 — Corpus-wide filtering + coarse routing

Filtering is a curated, versioned **rule pack** (per team; extendable per campaign).
Each check is named and typed:

- **Deterministic:** regex/structural patterns — UUID-only case pages, empty/boilerplate
  pages, macro debris, navigation/link-farm pages, length floors, near-duplicate and
  version-copy collapse (content hashing / minhash), attachment stubs.
- **Reasoning:** MiniMax against a closed rubric on an excerpt or gist-translated
  snippet — e.g., a domain-scope check that catches "valid document type but out of
  domain," which no deterministic rule can.

Every check writes its individual result to the ledger. Checks run in three lanes:
**gate** (high-confidence deterministic auto-reject), **evidence** (deterministic
annotations that inform the judge and the reviewer but never decide alone), and
**judge** (binary MiniMax verdicts). Titles are known to be non-indicative — no
decision rests on titles alone.

Decision policy: **binary, no composite score** (client decision 2026-08-02). Gates
auto-reject with a reason code and no human pre-approval. Judge checks return
in / out / can't-tell: confident verdicts stand, low-confidence and can't-tell route
to the expert review queue. A post-hoc audit sample (~5%) of all rejects goes to the
expert each batch. Rejected docs keep their ledger rows and artifacts forever
(analyzable, reversible).

Coarse domain routing (multi-label allowed) uses source path, structural cues, and
MiniMax on gist-translated title + opening lines, with the expert skimming the
borderline band.

### Campaign planning — questionnaire + dependency graph

A shipped artifact, `templates/ingest-pipeline/` (a step-per-folder questionnaire with an
index README and shared `domains.md` / `sources.md`), guides the expert +
infra supporter to produce the **campaign plan**: guiding questions covering
domain/subdomain enumeration, source→domain mapping, overlap pairs and **which campaign
owns each overlap first**, trust tier per source category, foundational→advanced
ordering, in/out-of-scope examples (these seed the filter rule pack), expected document
types, and per-campaign priority + success criteria.

A campaign is a **ledger query** (a source, a data type, or any slice), not a folder.
Campaigns form an explicit dependency graph. Two standing rules govern overlap:

1. **Order rule:** for overlapping content, the higher-trust / more foundational
   campaign runs first and creates the concept notes.
2. **Collision rule:** when a later campaign reaches a concept that already has a note
   (the vault's search-before-create rule detects this), it is a triggered comparison
   with three outcomes — *agree* (append source), *extend* (add a layer), *conflict*
   (trust-axis rules decide; expert arbitrates).

Overlap zones thereby become the bridges that stitch domains together for joint
querying rather than a duplication risk.

### Phase 2 — Per-campaign execution (in dependency order)

1. **Kickoff triage:** rule-pack patterns + gist-translated excerpts; the expert sorts
   the campaign's docs into rough tiers (foundation → advanced) in one sitting. This
   human-ordered list is the translation curriculum.
2. **Translation (expert loop):** MiniMax drafts; unknown/uncertain terms are detected
   by script and queued for the expert; resolved terms accrete into a per-domain
   **glossary**, itself a first-class vault artifact reused by later docs and by
   classification.
3. **Doc-type discovery → taxonomy freeze:** sample the campaign, propose document
   types, expert approves; classification vocabulary is closed from then on.
4. **Classification:** on English text, closed vocabulary, glossary in context; MiniMax
   closed-choice with expert spot-check queue sized by the Phase-0 autonomy dial.
   Writes `doc_type`, `trust`, refined `level` and `domains`.
5. **Curriculum ingest:** ingest in (level asc, trust desc) order per the campaign's
   generated, expert-approved ingest strategy; collisions with existing notes follow
   the collision rule. Extends `vault-ingest` for batch + campaign context.
6. **Domain QA gate:** per-domain gold questions (T1-style), negative controls (T2),
   lint (T0) before the campaign is declared done.

### Phase 3 — Cross-domain (continuous)

Joint-query checks across finished campaigns, cross-domain linking passes,
write-back analyses per the existing query workflow.

## Data & state

### Ledger

- **Append-only JSONL event log:** `doc_id, stage, status, score(s), reason, method,
  timestamp`. Current state is a projection script over the log; the browsable folder
  tree is generated (symlink) views. History is never lost; "what happened to this doc
  and why" is always answerable.
- **Identity:** stable `doc_id` = f(source path, content hash) at entry. An upstream
  content change flags all downstream artifacts stale instead of silently serving old
  derivatives.

### Recovery conventions (integral, enforced by a stage-runner wrapper)

1. **Stages never mutate inputs.** Every stage writes new artifacts; `raw_md/` is as
   immutable as `raw/`. Recovery from any failure = re-run; the input still exists.
2. **Atomic writes.** Temp name → rename into place → only then update the ledger.
   A crash leaves either no artifact or a complete one + stale ledger row — both
   detectable, both re-runnable. No half-written state.
3. **Append-only ledger** (above) — decisions and history survive everything.
4. **Batch checkpoint commits** — each stage run ends with one git commit of the
   small-text layer (ledger, notes, plans), making any batch revertible.

### Storage split (git is too heavy for the bulk)

- **Git:** `wiki/`, `index/`, ledger, glossaries, campaign plans, rule packs, scripts —
  a few MB of high-value text. Batch commits stay cheap and meaningful.
- **Content-addressed store:** bulk artifacts (`raw_md`, translations, derived copies)
  in an append-only directory keyed by content hash (`store/ab/abcdef….md`), written
  once, never modified or deleted. Versioning collapses into ledger pointer history
  ("translation of X is H2, superseding H1") — and the ledger is in git, so reverting
  a batch reverts pointers while all artifact versions remain in the store.
- **Backup:** `rsync -a` of the store to a second disk — inherently incremental because
  files never change. No LFS, no DVC, no server.

## MiniMax usage policy

- Closed-choice decisions only: pick from a rubric, score against anchors, select from
  a frozen vocabulary. Never free-form synthesis into the wiki without the templates
  and blanks pattern already used by `vault.py`.
- Every model decision is recorded with `method: model` and is binary
  (in / out / can't-tell) — never a numeric score. Low-confidence and can't-tell
  verdicts route to the expert queue. Autonomy per task is set by Phase-0 agreement
  rates and revisited when the gold-sample regression eval is re-run.

## Out of scope / deferred to per-stage deep dives

- **Security / prompt-injection screening: deliberately none.** The corpus is trusted
  content from trusted authors on a permissioned platform inside an air gap; the
  client decided (2026-08-02) to spend nothing here. Do not reintroduce screening
  stages into the pipeline design.
- **Stage 4 deep dive:** done — `2026-08-02-stage4-filtering-design.md`.
- **Stage 5 deep dive:** done — `2026-08-03-stage5-translation-design.md`.
- **Stage 6 deep dive:** taxonomy/questionnaire final form, classification prompts and
  rubrics, spot-check sampling math.
- **Stage 7 deep dive:** ingest-strategy artifact format, batch `vault-ingest`
  extension, collision/conflict mechanics in note updates.
- **Stage 8 deep dive:** `wiki/` schema changes — domain namespacing, trust-aware
  `sources:`, layered concept notes, glossary note type, MOC-per-domain.
- Stages 1–3 hardening (separately noted: community consensus prefers Confluence
  API/HTML extraction over PDF export — revisit when stages 1–3 return to scope).

## Prior art — client repo (reference only)

`second-brain-offline` PR #1 ("staged pipeline, review queue, live monitor, forensic
eval harness") predates this design and contains working prototypes touching several
stages: a SQLite stage cache, a non-blocking markdown review queue, a translation stage
(language detection, glossary injection, RTL-corruption fix loop, unknown-term
clarification), closed-vocabulary classification with new-category review, extra
converters (Visio/OneNote/email), and a post-run eval harness.

**Policy (client decision, 2026-08-02):** it is an explicitly quick-and-dirty prototype
and is treated as *evidence about the corpus and the problem*, not as a codebase to
inherit. Each stage deep dive runs its own research pass first and designs on the
merits; the prototype is then assessed against that design, per stage, and adapted only
where it earns its place. Stage 4 has done this — see
`2026-08-02-stage4-filtering-design.md` for what it took and what it rejected.

## Success criteria (pilot)

- First campaign end-to-end: filtered, translated, classified, ingested, and passing
  its domain QA gate — demonstrably queryable by the client while later campaigns are
  still queued.
- Ledger can answer, for any doc: where it is, every decision made about it, by which
  method, and why.
- A failed stage run is recoverable by re-running with zero manual file surgery.
- Filter decision analysis (per-rule fire counts, decision/queue breakdowns) is
  possible retroactively from the ledger alone.
- Gold-sample regression eval is re-runnable at any time inside the gap.
