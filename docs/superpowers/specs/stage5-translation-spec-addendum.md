# Stage-5 Translation Addendum (reality corrigendum — 2026-08-14)

> Addendum to `docs/superpowers/specs/2026-08-03-stage5-translation-design.md` (spec-only, then-speculative). This file records the as-built reality so the spec does not contradict the pipeline.

## What changed vs the 2026-08-03 spec

| Topic | Spec (then) | Reality (now) |
|-------|-------------|---------------|
| Translator model | Dicta-LM 3.0 24B (§5 “Model routing”, p.27ff) | **MiniMax M2.7** (unlimited) — translator; *Dicta-LM not deployed in this vault* |
| Reviewer model | MiniMax M2.7 or Dicta-LM (English-side QA) | **Kimi K2.7** (limited budget, sampled ~10–20%, deterministic seed) |
| Person-name guard | NER (DictaBERT/AlephBERT) | **Offline allowlist** `data/person_names/first_names.txt` (592) + `last_names_ranked.txt` (818) — NER is optional enhancement, not required |
| Glossary delivery | `campaigns/<campaign>/glossary.md` markdown table | **`data/domain_terms/glossary.csv`** (CSV, Excel/Sheets native; `review_queue.csv` is the single-queue source of truth; `batch-*.md` is a rendered view) |
| Config key | `QMD_OPENAI_*` only | **`translation: {base_url, reviewer_base_url, api_key_env, model, reviewer_model, chunk_chars, review_sample, glossary_path}`** in `convert_config.json`; env precedence `TRANSLATE_BASE_URL` primary → `QMD_OPENAI_BASE_URL` fallback → `TRANSLATE_REVIEWER_BASE_URL` override |

## What stayed

Deterministic QA battery (no LLM judge), zero-guessing `⟦he:⟧` markers + `blocked_on_term`, content-addressed store + `ledger.jsonl`, ledger-driven retranslation on glossary/policy bumps, structural heading→paragraph chunking (qmd-aligned), single queue for translation (classification → stage 6, deferred).

## Phase-0 calibration (as-built)

AI (MiniMax M2.7 / Kimi K2.7) drafts 3–5 reference translations per genre → human verifies & fixes each → approved references are committed and used to fit QA thresholds. `proposed` stays blocked until expert approves; degraded mode (`glossary_proposed.csv`) keeps translation gated.
