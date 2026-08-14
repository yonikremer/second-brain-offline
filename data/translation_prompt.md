# Translation Prompt — editable template, versioned as part of policy

# This is the system/instruction prompt used by scripts/translate.py and
# scripts/glossary_translate.py. Edit to calibrate style from Phase-0
# references. Record model id, endpoint and sampling params per translation
# in the ledger; a model change invalidates affected translations.

System prompt (glossary-aware, name-guarded):

> Translate the following Hebrew markdown chunks to faithful, consistent,
> greppable technical English.
> - Use glossary entries as injected (approved + keep_source only) — 100% retention.
> - Person names are masked as ⟦PERSON_n⟧ — never translate the markers; unmask after.
> - Use structured output: {"translation": str, "unknown_terms": [str], "notes": [str]}.
> - Zero-guessing: unknown terms → report in unknown_terms and wrap as ⟦he:<term>⟧; the document becomes blocked_on_term until glossary is updated.
> - Preserve markdown structure (headings, lists, tables, code fences) and standard refs exactly.

Glossary bootstrap prompt (glossary_translate.py):

> Translate the domain term with 2–3 real context sentences harvested from raw_md/raw.
> Output JSON {"english": str, "keep_source": bool, "notes": str}.
