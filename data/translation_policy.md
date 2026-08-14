# Translation Policy — editable template, versioned via hash

# This file records per-campaign translation policy. A version bump marks
# affected documents stale and schedules re-translation (see ledger
# glossary_version / policy_version).

## Terms
- `keep_source=true` entries in `data/domain_terms/glossary.csv` stay Hebrew / transliterated.
- Person names (data/person_names/ allowlist) always stay Hebrew (masked before translation).

## Acronyms & codes
- Acronyms in mixed tokens (e.g. הAPI → API) are lowercased after proclitic strip; keep canonical English case from glossary.

## Headings & structure
- Heading count and nesting preserved exactly. Lists, tables, code fences preserved.

## Units, standards, part numbers
- Numeric fidelity: preserve numbers, units, part numbers, standard refs verbatim.

## Tables (GFM) — hard guarantee via placeholder masking
- Table structure (`|`, separator `|---|---|`, alignment `:---:`) is NEVER translated — masked as `<<<TABLE_n>>>` before the LLM sees it (`scripts/md_mask.py`).
- Only cell text is translated, cell-by-cell via `<<<TABLE_CELL_n>>>`. A `|` inside cell text is forbidden unless escaped `\|` or inside `` `code` `` (which is masked first, so the pipe never splits a column).
- QA enforces `table_fidelity` (column count per row, row count per table, separator row count + alignment colon parity) — any mismatch quarantines the doc (`scripts/translation_qa.py:check_table_fidelity`).

## Quoted / embedded English
- English already present in source is preserved as-is at word/sentence/section granularity.
  `scripts/translate.py` masks contiguous Latin-script spans to ⟦EN_n⟧ sentinels before
  the LLM call and restores them after, so the model cannot alter English fragments.
  Documents that are entirely English (no Hebrew after stripping frontmatter/code) are
  skipped entirely (`ledger: skipped_english`) — they are already in the target language.

## How this policy was fitted
- Calibrated from 3–5 Phase-0 approved references (AI draft → human verify & fix). QA thresholds (residual_hebrew_ratio, length_ratio) are fitted from those references.
