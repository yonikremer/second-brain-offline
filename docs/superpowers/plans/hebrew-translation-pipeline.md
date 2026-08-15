# Hebrew→English Translation Pipeline — Intent

**Goal:** Deterministic, offline-capable pipeline for a large Hebrew markdown corpus with heavy domain terminology. Protects glossary terms and person names, gates vault entry on expert-approved assets. Ships as framework scripts + specs — no domain data committed.

**Why separate from conversion:** `feat/document-conversion-pipeline` (raw→raw_md, PR #3) handles format conversion; this stage handles language translation. Broad scope is intentional — reviewer needs campaign-level intent, not per-function code.

## Approach

* **Glossary-first — not per-doc discovery.**
  * `extract_domain_terms.py` (PR #4) mines `raw_md/` → `data/domain_terms/translation_seed.csv` + 2–3 real context snippets per term.
  * `glossary_translate.py` (MiniMax M2.7, unlimited) bulk-proposes all terms → `glossary_proposed.csv` (`term_he, english, keep_source, notes, status=proposed`).
  * Expert edits **CSV** `glossary.csv` in Sheets/Excel (`approved|keep_source|proposed`). `check_glossary.py` blocks translation until every row is `approved`. Hash of `glossary.csv` = `glossary_version`.

* **Translation execution** (`translate.py`)
  * Chunks at headings/paragraphs (≤6k chars, qmd-aware), never mid-table/fence/frontmatter. Per-chunk prompt carries parent title, section path, and *only glossary entries that appear in the chunk* (word-boundary match).
  * **Person-name guard:** deterministic allowlist `data/person_names/first_names.txt` (593 curated) + `last_names_ranked.txt` (818), whole-token match (maqaf-aware). Codenames that collide with given names are excluded at runtime via `glossary.csv` — if a name is a glossary term it translates, not masks. Masked as `⟦PERSON_n⟧`, unmasked after.
  * Zero-guessing: unknown term → `⟦he:term⟧` + `unknown_terms[]` → `blocked_on_term`, never guessed. Content-addressed `data/translations/<sha>/translation.md` + append ledger.

* **Deterministic QA** (`translation_qa.py`) runs every doc: residual Hebrew, untranslated blocks, glossary retention, heading/structure/numeric/markup/marker counts. Fail → quarantine. Thresholds fitted from 3–5 Phase-0 references.

* **Reviewer sampling** — Kimi K2.7 (limited) on 10–20% English-side spot checks (glossary consistency, AskQE, structure); MiniMax bulk translation. Ledger records `reviewer_model`.

* **Single human queue** (`review_queue.py`): `review_queue.csv` (source of truth) + `gen-packets` markdown views. One question per term, ranked by `blocked_docs`. Answering bumps `glossary_version` → retranslation scheduled.

* **Phase 0:** AI drafts 3–5 reference translations → expert verifies & fixes → committed as `campaigns/<c>/references/`. Fits QA bands and policy style; never used for model ranking.

## File Map

```
NEW  scripts/glossary_translate.py, check_glossary.py, translate.py (core), translation_qa.py, translation_reviewer.py, review_queue.py
NEW  data/domain_terms/glossary.csv (template), data/translation_policy.md, docs/human-review-queue.md
MOD  convert_config.json (translation.* keys), .gitignore (waive translations/queue/campaigns, !glossary.csv)
```

All LLM callers fail-closed if `TRANSLATE_BASE_URL` missing and not `--mock`.

## Constraints

* Pure stdlib, offline; `wordfreq` + `deps/yap/yap.exe` hard requirements where used.
* No classification, no security screening in this stage.
* Glossary is CSV-only (no md table round-trip).

## Verification

```bash
python -m pytest -q
python scripts/glossary_translate.py . --mock --out data/domain_terms/glossary_proposed.csv
python scripts/check_glossary.py data/domain_terms/glossary.csv
python scripts/translate.py . --mock --glossary data/domain_terms/glossary.csv --out data/translations/
python scripts/translation_qa.py data/translations --vault-root . --json-out qa.json
python scripts/review_queue.py list data/review_queue/review_queue.csv
```

Seeded-error fixtures: one doc per QA failure mode; reference regression stays within fitted bands.

## What NOT to do

* Don't translate before glossary is approved. Don't per-doc propose terms.
* Don't translate person names that are codenames (glossary wins).
* Don't auto-reject on reviewer flags — flags, not rejections.
