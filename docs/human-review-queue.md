# Human Review Queue

Single queue for Hebrew→English translation: glossary and translation questions. Classification queue is future work (not wired).

## What goes in the queue

| Case | Enters queue | Status |
|------|-------------|--------|
| Domain term needs English | Yes — one question per term, ranked by `occurrences × blocked_docs` | `proposed` → `approved` or `keep_source` |
| Translation has `⟦he:term⟧` markers | Yes — via `unknown_terms` from LLM | `proposed` |
| Reviewer flags glossary inconsistency | Yes | `proposed` |
| Gate auto-reject (deterministic) | No — final, not queued | — |
| Stylistic polish | No — faithful/greppable only | — |
| Model ranking | No — blind side-by-side is separate | — |
| Classification (domain/type/tier) | Deferred — not in scope | — |

## The file

- **Source of truth:** `data/review_queue/review_queue.csv`
- Columns: `term_he,english,keep_source,notes,status,example_doc,context_snippets,occurrences,blocked_docs,question_id`
- Status enum: `approved | proposed | keep_source | pending`

## Workflow

```bash
# 1) Generate CSV + rendered markdown views (views are read-only)
python scripts/glossary_translate.py . --out data/domain_terms/glossary_proposed.csv
# Optionally merge into review queue or edit glossary.csv directly
python scripts/review_queue.py gen-packets data/review_queue/review_queue.csv

# 2) Edit the CSV in Excel/Sheets/LibreOffice
#    Set status to approved or keep_source, fix english column.

# 3) Validate (dry-run)
python scripts/check_glossary.py data/domain_terms/glossary.csv
python scripts/review_queue.py parse data/review_queue/review_queue.csv --dry-run

# 4) Apply — appends decided_by:human to ledger (data/translations/ledger.jsonl)
python scripts/review_queue.py parse data/review_queue/review_queue.csv

# 5) Retranslate blocked docs
python scripts/translate.py . --resume

# 6) Re-check
python scripts/translation_qa.py data/translations
python scripts/translation_reviewer.py data/translations --sample 0.2 --mock
```

## Markdown packets

`data/review_queue/batch-*.md` are generated from the CSV for reading. Do not edit them — edit the CSV.

## When to use vs not use

- **Use it** when a term blocks 1+ docs, or reviewer flags drift.
- **Answer the term once**, not each doc individually — a glossary bump unblocks every waiting doc.
- **Don't use it** for stylistic requests or classification (future Queue B, reserved but not wired).
- A glossary version bump (hash of `glossary.csv`) marks affected docs stale and schedules retranslation via ledger query.

## Expert time

~1 hour for initial glossary bootstrap is highest leverage; per-batch review trends down as coverage rises.

## Phase 0 — AI drafts, human verifies

AI (MiniMax M2.7 / Kimi K2.7) drafts 3–5 reference translations per genre; human verifies and fixes each, then approves. Approved references are committed and used to fit QA thresholds (residual Hebrew ratio, length bands). Unverified drafts stay `proposed`.
