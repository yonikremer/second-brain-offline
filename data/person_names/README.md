# Israeli Person Names - for translation keep-in-Hebrew

## Files
- `first_names.txt` (592) - curated common Hebrew first names for LLM guard (keep in Hebrew). Sources: faker-js he/person/first_name.ts (539, https://github.com/faker-js/faker) + manual Israeli common names + Arab-Israeli names (מוחמד, אחמד...). Sorted alphabetically, UTF-8.
- `first_names_full.txt` (107,793) - full pure-Hebrew registry from data.gov.il dataset `firs-name` (https://data.gov.il/dataset/firs-name, 116,673 records, updated 2020-09-14, via datastore API). Filtered to Hebrew letters+space only. Keep for recall, use curated for precision.
- `last_names.txt` (818, alphabetical) + `last_names_ranked.txt` (818, frequency-ranked - Wikipedia 30 first, then rest)
  Sources:
  - Wikipedia List_of_most_common_surnames_in_Asia - Israel section (30 ranked with %: Cohen 1.93%, Levi 1.12%, Mizrachi 0.33%...): https://en.wikipedia.org/wiki/List_of_most_common_surnames_in_Asia
  - faker-js he/person/last_name.ts (738, https://github.com/faker-js/faker/blob/master/src/locales/he/person/last_name.ts)
  - FamilySearch Israel Naming Customs: https://www.familysearch.org/en/wiki/Israel_Personal_Names
  - BehindTheName Hebrew usage: https://www.behindthename.com/names/usage/hebrew
  - CBS/MoI published top surnames (Cohen, Levi, Mizrahi, Peretz, Biton, Dahan, Avraham, Friedman...)

## Usage in translation pipeline
```python
first = set(open('data/person_names/first_names.txt', encoding='utf-8').read().splitlines())
last = set(open('data/person_names/last_names_ranked.txt', encoding='utf-8').read().splitlines())
# 1) NER pass (dictabert) -> mask PERSON spans -> translate -> unmask (primary, keeps Hebrew)
# 2) Exact-match fallback: if token in first or token in last -> mask even if NER missed
# 3) Log masked name not in lists -> human review queue ("new name candidate")
```
Keep `data/person_names/` gitignored? Currently under `data/` (waived). Add to pipeline as offline bundle.

