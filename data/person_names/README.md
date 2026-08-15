# Israeli Person Names - for translation keep-in-Hebrew

## Files
- `first_names.txt` (593) - curated common Hebrew first names for LLM guard (keep in Hebrew). Sources: faker-js he/person/first_name.ts (539, https://github.com/faker-js/faker) + manual Israeli common names + Arab-Israeli names (מוחמד, אחמד...). Sorted alphabetically, UTF-8.
- `first_names_full.txt` (107,793, gitignored) - full pure-Hebrew registry from data.gov.il dataset `firs-name` (116,673 records, 2020-09-14). Keep for recall, use curated for precision.
- `last_names_ranked.txt` (818) - frequency-ranked surnames (Wikipedia Israel top 30: Cohen 1.93%, Levi 1.12%, Mizrachi 0.33% … then faker-js he/person/last_name.ts + FamilySearch/BehindTheName + CBS top surnames).

## Usage in translation pipeline
```python
first = set(open('data/person_names/first_names.txt', encoding='utf-8').read().splitlines())
last = set(open('data/person_names/last_names_ranked.txt', encoding='utf-8').read().splitlines())
# 1) NER pass (dictabert) -> mask PERSON spans -> translate -> unmask (primary, keeps Hebrew)
# 2) Exact-match fallback: if token in first or token in last -> mask even if NER missed
# 3) Log masked name not in lists -> human review queue ("new name candidate")
```
## Attribution

- `first_names.txt` includes 539 names from [faker-js](https://github.com/faker-js/faker) (`faker/src/locales/he/person/first_name.ts`, MIT) + manual Israeli common + Arab-Israeli additions (מוחמד, אחמד...).
- `last_names_ranked.txt` includes frequency-ranked surnames from Wikipedia Israel top 30 + faker-js `he/person/last_name.ts` (MIT) + FamilySearch/BehindTheName + CBS.
- `first_names_full.txt` (107,793, gitignored) is the full allowlist from data.gov.il `firs-name` (116,673 records, 2020-09-14) — kept for recall, not committed.

All committed lists (`first_names.txt`, `last_names_ranked.txt`, `codenames.txt`) are part of the offline bundle. The large raw `first_names_full.txt` and intermediate `first_names_raw.*` / `faker_first.json` remain gitignored (see `.gitignore`).

