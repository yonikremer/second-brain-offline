# Markdown Mask + Table Preservation — Intent

**Goal:** Ensure the translation pipeline never corrupts Markdown structure. GFM tables are the highest-risk construct and must be handled cell-by-cell with hard guarantees.

**Why:** LLM translation mangles pipes, separators, fences, code, LaTeX, HTML, and wikilinks if sent as plain text. The existing `verify_all_preserved` check is a second layer, not a primary guard.

## Approach

* **Placeholder masking layer** — new stdlib-only module `scripts/md_mask.py` that replaces non-translatable structure with opaque sentinels before the LLM call and restores verbatim after:
  * Block level: frontmatter, fenced code (quote-aware), HTML comments, LaTeX blocks.
  * Inline level: inline code, LaTeX inline (currency-safe), HTML tags, Obsidian `[[wikilinks]]`/`![[embeds]]`, image/link markup (keep alt/text), headings, lists, blockquotes.
  * **Tables cell-by-cell:** detect GFM header+separator blocks, mask separator rows as `TABLE`, extract each cell as `TABLE_CELL`, re-assemble with canonical `| cell | cell |` spacing. Escaped `\|` and `code|pipe` inside cells must not split columns.
* **Segmentation** — masked lines are split into translatable text segments vs placeholders; only text segments are sent to the LLM. Merge + fixed-point restore reinserts all placeholders (including nested HTML→CODE).
* **Wiring** — `scripts/translate.py` masks each chunk before `build_prompt`/`call_llm` and restores after; `--no-mask` disables for debugging. Existing invariant verification stays as second layer.
* **QA hardening** — `scripts/translation_qa.py` gets a strict `table_fidelity` check (table/row/column counts, separator alignment, column drift) that quarantines any mismatch.

**Reference:** port of `rockbenben/md-translator` `markdown.ts` / `pipeline.ts` protection order to pure Python `re` + line scan (no deps).

## File Map

```
NEW  scripts/md_mask.py              # vocabulary, filter_markdown_lines, split/merge/restore, table cell handling
MOD  scripts/translate.py            # mask → translate → restore
MOD  scripts/translation_qa.py       # table_fidelity invariant
NEW  tests/test_md_mask.py + fixtures/md_mask/  # unit + golden-file roundtrips
```

No payload change (`manifest.json` untouched).

## Constraints

* Pure stdlib (air-gap). No runtime deps.
* Fail closed — any table drift quarantines the doc.
* Table detection only when next line is `|---|---|` separator; prose `a | b` stays as text.

## Verification

```bash
python -m unittest discover -s tests -v          # mask roundtrips + QA still pass
python scripts/translate.py --mock --limit 5     # tables preserve pipes/separators
python scripts/translation_qa.py data/translations --vault-root . --json-out /tmp/qa.json
```

Intentional table corruption (inject stray `|`) must yield `table_fidelity: fail`.

## What NOT to do

* Don't mask whole tables as one blob — cell text must be individually translatable.
* Don't relax separator checks (`:---:` alignment matters).
