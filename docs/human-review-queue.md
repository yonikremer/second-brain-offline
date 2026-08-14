# Human Review Queue

Single queue for Hebrew→English translation: glossary and translation questions. Classification queue is future work (not wired).

## Quickstart — mock, no keys (7 steps, offline)

```bash
# Requires: wordfreq==3.1.1, deps/yap/yap.exe (or install via docs — --mock only skips LLM, not extraction prereqs)
# Vault root ambiguity: POSITION 1 is always vault_root (defaults . = this repo). Other paths use flags.
python scripts/extract_domain_terms.py . --top-n 200
# Next: generate proposed glossary (contexts auto from raw_md/ else raw/)
python scripts/glossary_translate.py . --mock --out data/domain_terms/glossary_proposed.csv
# Alternative explicit input (same effect):
# python scripts/glossary_translate.py . --input data/domain_terms/translation_seed.csv --out data/domain_terms/glossary_proposed.csv --mock
# Edit glossary_proposed.csv in Excel/Sheets → save as data/domain_terms/glossary.csv, set status to approved
python scripts/check_glossary.py data/domain_terms/glossary.csv          # gate: fails while any row != approved
python scripts/translate.py . --mock --glossary data/domain_terms/glossary.csv --out data/translations/
python scripts/translation_qa.py data/translations --vault-root . --glossary data/domain_terms/glossary.csv
python scripts/translation_reviewer.py data/translations --sample 0.2 --mock --vault-root .
python scripts/review_queue.py gen-packets data/review_queue/review_queue.csv --out-dir data/review_queue --batch 20
python scripts/review_queue.py list data/review_queue/review_queue.csv
python scripts/review_queue.py parse data/review_queue/review_queue.csv --dry-run
python scripts/review_queue.py parse data/review_queue/review_queue.csv  # appends decided_by:human + ts + glossary_version
python scripts/review_queue.py clean data/review_queue/review_queue.csv
```

For a fresh clone `data/domain_terms/` is empty — `glossary.csv` template is shipped (see `#` comment line). Seed flow: `extract_domain_terms.py` → `translation_seed.csv` → `glossary_translate.py` → `glossary.csv` template edit → `check_glossary.py` gate before `translate.py`.

## What goes in the queue

| Case | Enters queue | Status |
|------|-------------|--------|
| Domain term needs English | Yes — one question per term, deduped (one question per term per campaign), ranked by `occurrences × blocked_docs`, ordered `blocked_docs desc → age` (~5% audit sample of gate decisions when mixed in) | `proposed` → `approved` or `keep_source` (`pending` is review-queue-only interim, blocked by glossary gate) |
| Translation has `⟦he:term⟧` markers | Yes — via `unknown_terms` from LLM | `proposed` → `approved` |
| Reviewer flags glossary inconsistency | Yes — inconsistent glossary sweep / AskQE | `proposed` |
| Gate auto-reject (deterministic) | No — final, not queued | — |
| Stylistic polish | No — faithful/greppable only | — |
| Model ranking | No — blind side-by-side is separate; queue never re-ranks models | — |
| Classification (domain/type/tier) | Deferred — not in scope | — |

Columns: `term_he,english,keep_source,notes,status,example_doc,context_snippets,occurrences,blocked_docs,question_id`

Status enum: `approved | proposed | keep_source | pending` — `pending` is review-queue-only interim (not valid in `data/domain_terms/glossary.csv`; `check_glossary.py` treats `pending` as unapproved and blocks translation).

**Parser rules (scripts/review_queue.py):** validates `VALID_STATUSES`, trims whitespace, `blocked_docs` ordered desc. **Not queued as ledger-char:** gate auto-rejects are never re-answered one-per-term; translation `blocked_on_term` is resolved once per term (answer term once, not per 50 docs).

## The file

- **Source of truth:** `data/review_queue/review_queue.csv`
- **Markdown packets:** `data/review_queue/batch-*.md` are rendered views from the CSV (`gen-packets` defaults `--batch 20`, `--out-dir data/review_queue`). Do not edit them — edit the CSV.
- **Ledger:** `parse` appends `question_answered` events with `ts` (ISO), `glossary_version` (sha256 of `glossary.csv`), `decided_by: human` to canonical `vault/data/translations/ledger.jsonl` (`get_ledger_path`, `parents[2]`-aware). `translate.py` also writes `ts` + `glossary_version` on `translation_completed/blocked_on_term`. `translate.py --resume` is alias for default skip-cache (re-scans all, skips cached).

## Workflow (detailed, mirrors quickstart above)

```bash
# 1) Generate glossary + rendered markdown views (views are read-only)
python scripts/glossary_translate.py . --out data/domain_terms/glossary_proposed.csv --mock
# Optionally merge into review queue or edit glossary.csv directly (edit # comments are stripped before CSV parsing)
python scripts/review_queue.py gen-packets data/review_queue/review_queue.csv

# 2) Edit the CSV in Excel/Sheets/LibreOffice
#    Set status to approved or keep_source, fix english column. Empty status treated as error; lines starting with # ignored.

# 3) Validate (dry-run)
python scripts/check_glossary.py data/domain_terms/glossary.csv
python scripts/review_queue.py parse data/review_queue/review_queue.csv --dry-run   # shows approved/total

# 4) Apply — appends decided_by:human to ledger (data/translations/ledger.jsonl)
python scripts/review_queue.py parse data/review_queue/review_queue.csv
python scripts/review_queue.py parse data/review_queue/review_queue.csv --ledger data/translations/ledger.jsonl  # explicit ledger

# 5) Retranslate blocked docs (resume = default cached skip)
python scripts/translate.py . --resume --mock

# 6) Re-check
python scripts/translation_qa.py data/translations --vault-root . --glossary data/domain_terms/glossary.csv --json-out qa.json
python scripts/translation_reviewer.py data/translations --sample 0.2 --mock
```

Error examples:
- Invalid status: `row 4 term='שלום': invalid status 'apprvoed'`
- Empty glossary: `glossary has no rows`
- Ledger parent missing: queue `parse` suppresses ledger write if `vault/data/translations/` absent until you run `translate.py` first.

## When to use vs not use

- **Use it** when a term blocks 1+ docs, or reviewer flags drift.
- **Answer the term once**, not each doc individually — a glossary bump (hash of `glossary.csv`) unblocks every waiting doc and schedules `retranslation_scheduled`.
- **Don't use it** for:
  - `gate` auto-rejects — deterministic, final; no queue.
  - Model re-ranking — blind side-by-side is separate; queue never decides models.
  - Stylistic requests or classification (future Queue B, reserved but not wired) — faithful/greppable only.
- Ordering inside the queue: `blocked_docs desc → age`; dedup: one question per term per campaign (`frequency × blocked_doc_count`).

## Config & env (where translation block lives)

- File: `convert_config.json` `translation: {base_url, reviewer_base_url, api_key_env, model, reviewer_model, chunk_chars, review_sample, glossary_path, fix_rounds}`
- Defaults: `base_url ""`, `reviewer_base_url ""` (inherits `base_url`), `api_key_env "TRANSLATE_API_KEY"`, `model "minimax-m2.7"`, `reviewer_model "kimi-k2.7"`, `chunk_chars 6000`, `review_sample 0.2`, `glossary_path "data/domain_terms/glossary.csv"`, `fix_rounds 3`.
- Env precedence: `TRANSLATE_BASE_URL` primary → `QMD_OPENAI_BASE_URL` fallback; `TRANSLATE_REVIEWER_BASE_URL` → `TRANSLATE_BASE_URL` → `translation.reviewer_base_url` → `translation.base_url`. Same for `API_KEY` (`TRANSLATE_API_KEY` → `QMD_OPENAI_API_KEY`). Reviewer model: `TRANSLATE_REVIEWER_MODEL` → `translation.reviewer_model`. Fix rounds: `--fix-rounds N` CLI → `TRANSLATE_FIX_ROUNDS` env → `translation.fix_rounds` config → default 3 (0=disable). `--mock` bypasses base_url requirement (api_key may be empty for no-auth gateways).

## Auto-fix rounds

`translate.py` runs scripted QA after each doc and, if failures remain, asks the LLM to repair them for up to `translation.fix_rounds` (default 3, CLI `--fix-rounds N`, env `TRANSLATE_FIX_ROUNDS`). Each attempt is logged as `fix_attempt` (+ `qa_result`) in `data/translations/ledger.jsonl` with `round`, `failures_before`, and `fix_rounds_used` in the frontmatter. If still invalid after N rounds, the doc is written as `qa_failed` (quarantine) and the script exits 1 fail-closed — inspect `qa.json` or the ledger, fix policy/glossary/prompt, and retry with `--force` or `--fix-rounds N`. This stops token waste on systematically broken docs.

## Expert time

~1 hour for initial glossary bootstrap is highest leverage; per-batch review trends down as coverage rises.

## Phase 0 — AI drafts, human verifies

AI (MiniMax M2.7 / Kimi K2.7) drafts 3–5 reference translations per genre (specification, manufacturer doc, academic essay, team knowledge page, email/message thread) → human verifies & fixes each → approved references committed as `campaigns/<campaign>/references/*.md` (or `data/references/` single-corpus fallback). Used to fit QA thresholds (residual Hebrew ratio default `0.02` → fitted; `length_ratio [0.5,2.5]` → fitted). Model id/endpoint/params recorded per translation in ledger; if expert doesn't fix, draft stays `proposed` and never becomes a reference; degraded mode keeps `glossary_proposed.csv` gated.
