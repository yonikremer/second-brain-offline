# Translation Self-Heal Fix Rounds — Intent

**Goal:** Translation should be scripted-QA-gated. Each translated document is automatically validated and, if it fails deterministic checks, the LLM is asked to repair only the reported failures — bounded, logged, and fail-closed.

**Why:** Without a gate, corrupted output (dropped headings, tables, fences; glossary violations; numeric drift) silently reaches the vault. The reviewer should see only flagged docs, not re-check every doc.

## Approach

* **Config** — `translation.fix_rounds` in `convert_config.json` (default 3, CLI `--fix-rounds N`, env `TRANSLATE_FIX_ROUNDS` overrides, `0` = disable). Resolution order: CLI > env > config > default.
* **Per-doc loop** — after assembling `full_translation`:
  1. Run `translation_qa.run_all(source, translation, glossary)` → collect `status==fail` checks.
  2. If failures and rounds remain, build a repair prompt (source cap 12k + previous translation cap 12k + failures JSON + glossary slice + invariants to preserve verbatim) and call `call_llm` once.
  3. Re-run QA. Repeat until pass or rounds exhausted.
  * Large docs (>12k) use chunked fix prompts (re-use `chunk_markdown` boundaries, per-chunk glossary slice) to avoid truncation; small docs use one prompt.
* **Quarantine & halt** — exhaustion writes `qa_failed` artifact (`fix_rounds_used`, top 5 failures) and the script exits 1 (fail-closed, stops token burn). Cached `qa_failed` also exits 1 until `--force`. In `--mock` mode the fix is simulated deterministically.
* **Ledger** — each attempt logs `fix_attempt` (round, failures_before, chunked flag) and final `qa_result`/`qa_failed` to `data/translations/ledger.jsonl` with `glossary_version` + `ts` for audit.

Existing `verify_all_preserved` (invariants in order) stays as first check; QA battery is the authoritative gate.

## File Map

```
MOD  convert_config.json         # translation.fix_rounds = 3
MOD  scripts/translate.py        # resolve_fix_rounds, build_fix_prompt, run_qa_for_doc, translate_one_doc_with_fix, main halt + ledger
MOD  scripts/translation_qa.py   # importable run_all (already)
MOD  tests/test_translation_pipeline.py
DOC  docs/human-review-queue.md  # add fix-rounds note
```

All LLM paths `sys.exit(1)` if `base_url` missing and not `--mock`.

## Constraints

* Never `while True` — fixed max only.
* Max one `_comment` line in `convert_config.json` (JSONC-style).
* Pure stdlib, no new deps. Deterministic sampling via `seed` where relevant.

## Verification

```bash
python -m unittest discover -s tests -v
python scripts/translate.py example_vault --mock --limit 2 --fix-rounds 2  # prints Fix rounds: 2
python scripts/translation_qa.py /tmp/out --vault-root example_vault --json-out /tmp/qa.json
# Inject a heading drop → first QA fail, second call fixes → pass; exhaust 2 rounds → exit 1 + qa_failed artifact
```

## What NOT to do

* Don't use the fix loop for `blocked_on_term` (`⟦he:⟧` markers) alone — that's glossary coverage, not QA corruption.
* Don't auto-retry on API errors infinitely — those are logged as `fix_attempt{error}` and break.
