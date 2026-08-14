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

## Quoted / embedded English
- English already present in source is preserved as-is.

## How this policy was fitted
- Calibrated from 3–5 Phase-0 approved references (AI draft → human verify & fix). QA thresholds (residual_hebrew_ratio, length_ratio) are fitted from those references.
