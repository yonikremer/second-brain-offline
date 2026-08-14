# Plan: Hebrew→English Translation Pipeline with Glossary + Name Guard + Review

## Context

User has a large corpus of Hebrew markdown documents with heavy domain-specific terminology. The goal is a deterministic, offline-capable translation pipeline that: extracts domain terms, bootstraps a glossary, protects domain vocabulary and person names during translation, and gates vault entry on expert-approved assets. This repo is a **template** — no domain data is committed; the pipeline ships as framework scripts + specs.

Prior work: `feat/domain-terms-moshe` (PR #4 — 796-line `extract_domain_terms.py` on `main` + 4 fix commits; `feat/domain-term-extraction` variant stacked on pipeline). `feat/document-conversion-pipeline` has raw→raw_md with Hebrew fix + dedup (630-line `convert_to_md.py`) — to be merged in if needed. Stage 5 design at `docs/superpowers/specs/2026-08-03-stage5-translation-design.md` is spec-only. Model reality: MiniMax M2.7 (unlimited, weak in Hebrew, strong in English), **Kimi K2.7** (limited budget), Hebrew-specific open models (not generation). `data/person_names/` holds 593 curated + 107k full first-names and 818 surnames as offline allowlist.

---

## 1) Branch strategy

```bash
git fetch origin
git checkout origin/feat/domain-terms-moshe -b feat/hebrew-translation-pipeline
# origin/feat/domain-terms-moshe already has extraction (PR #4 — 4 commits on main) — no cherry-pick needed.
# If pipeline files (convert_to_md / hebrew_fix / convert_config.json) needed, merge origin/feat/document-conversion-pipeline after.
```
Reason: Start from `origin/feat/domain-terms-moshe` (yonikremer/second-brain-offline) per user — PR #4 head, not `main` or `feat/document-conversion-pipeline`.

## 2) Glossary bootstrap — glossary-first, not per-doc discovery

### 2a) Term extraction (already in PR #4, just land it)

`scripts/extract_domain_terms.py` → `data/domain_terms/` (waived, like `raw_md/`):

- `terms.csv`, `variants.json`, `translation_seed.csv` (lang in he/mixed, `suggested_en` hint), `code_words.txt/csv`, `subdomain_keywords.json`, `report.json`
- Fails hard if `wordfreq 3.1.1` or YAP (`deps/yap/yap.exe`) missing.
- Corpus resolver prefers `raw_md/*.md` (rglob), falls back to `raw/`.

No change to semantics; just ensure landed tests pass: `tests/test_domain_terms_*.py` (tokenization, scoring, e2e) + fixtures.

### 2b) Auto-translate of domain terms (the missing link the user described)

New script `scripts/glossary_translate.py` — **not** inline in extraction:

- **Input:** `data/domain_terms/translation_seed.csv` + for each term, 2–3 **real context sentences** mined from `raw_md/` (or `raw/` fallback) — not invented. Utility `scripts/extract_domain_terms.py` already emits `example_doc`; extend to harvest 2–3 snippets per term via `grep` window over markdown-stripped text.
- **Model:** MiniMax M2.7 (unlimited) proposes **all** terms (per user choice) — bulk proposal, expert corrects. Hebrew open models not used for generation. OpenAI-compatible APIs. New config keys in `convert_config.json` under `translation: { base_url, reviewer_base_url, api_key_env, model, reviewer_model, chunk_chars, review_sample, glossary_path, fix_rounds }` — env precedence `TRANSLATE_BASE_URL` → `QMD_OPENAI_BASE_URL`; reviewer inherits `base_url` unless `reviewer_base_url` set. Fail-fast if base_url missing when not `--mock` (api_key may be empty for no-auth gateways).
- **Prompt:** glossary-translate instruction: "Translate this Hebrew domain term to English. Context sentences: ... Keep person names in Hebrew. Output JSON {term_he, english, keep_source(bool), notes}." Enforce `keep_source` for terms that are internal names / part numbers / must-stay-Hebrew.
- **Output:** `data/domain_terms/glossary_proposed.csv` with columns `term_he | english | keep_source | notes | status=proposed | example_doc | context_snippets | lang | model`. This is the machine draft — every row has a MiniMax proposal; anchoring risk acknowledged but accepted for expert-time savings (expert overwrites freely).
- **CLI:** `python scripts/glossary_translate.py [vault_root] [--input PATH] [--out PATH] [--model ID] [--limit N] [--mock]` — first positional is `vault_root` (defaults `.`); contexts auto-resolve `raw_md/` else `raw/`.

### 2c) Force expert review — the "nice editable file" (CSV)

Deliverable `data/domain_terms/glossary.csv` — plain CSV, one row per term, editable in Excel/Sheets/LibreOffice. No markdown table. Template is committed (`data/domain_terms/*` is waived except `!glossary.csv`).

CSV schema: `term_he,english,keep_source,notes,status,example_doc` — status enum `approved|proposed|keep_source`. `pending` is rejected by the glossary gate and is only valid inside `data/review_queue/review_queue.csv`. Shipped with headers + one example row + `#` comment line (stripped before parsing).

- **Enforcement:** `scripts/check_glossary.py` fails (exit 1) if any row has `status != approved` or file missing; `scripts/translate.py --check` calls it and blocks translation. CI prints the path.
- **Editor:** CSV — native in Excel/Sheets, no markdown table quoting issues, trivial to parse deterministically (`csv` stdlib). Lines starting with `#` are ignored (template comments).
- **Versioning:** glossary version = hash of `glossary.csv`. Bump stored in `data/domain_terms/glossary_versions.jsonl` (`{version, hash, approved_count, timestamp}`). Other stages read version for cache invalidation.

## 3) Translation execution

### 3a) Script `scripts/translate.py`

- **Config:** `convert_config.json` `translation: { base_url, reviewer_base_url, api_key_env, model, reviewer_model, chunk_chars, review_sample, glossary_path, fix_rounds }` via deep-merge + `TRANSLATE_*` → `QMD_OPENAI_*` env fallback. `chunk_chars` defaults `6000`, injected into `chunk_markdown`; `glossary_path` defaults `data/domain_terms/glossary.csv`. No `enabled`/`blocked_on_term_dir` keys.
- **Structural chunking:** at heading boundaries; if section > chunk_chars, at paragraph boundaries. Never mid-sentence/mid-table/mid-code-block/mid-frontmatter. Fixed 4000-char windows explicitly rejected per stage-5 spec; chunking is qmd-heading-aware aligned.
- **Context header per chunk:** parent doc title, section path, glossary entries whose terms occur in this chunk — word-boundary (`(?<![א-ת])term(?![א-ת])` for Hebrew, token set for mixed), not substring — keeps prompts small. Previous chunk tail as context-only marker, not to be re-emitted.
- **Person-name guard (people only) — allowlist-based (per user choice):**
  - Primary: exact-match against `data/person_names/first_names.txt` (593 curated) + `last_names_ranked.txt` (818) — whole-token via `_mask_via_tokens` (Hebrew maqaf `־` aware), not substring. This is the "good compromise" the user chose: cheap, deterministic, offline, no heavy NER model required.
  - **Codename exclusion (runtime, domain-dictionary-driven):** org codenames that collide with common Hebrew given names (e.g., `ברק`, `דניאל`) have English equivalents and **must be translated, not masked**. The domain-specific dictionary `data/domain_terms/glossary.csv` (generated from the md files via `extract_domain_terms.py` → `glossary_translate.py`) is the source of truth — at translation time `load_person_names()` is called with `exclude={term_he | status in (approved, keep_source)}` so any codename that appears as a glossary term is automatically removed from the PERSON allowlist. No one-time edit of `first_names.txt` is needed; adding a codename to the glossary immediately changes masking behavior. An optional `data/person_names/codenames.txt` (if present) is also read as an extra exclude set for manual overrides, but the primary mechanism is the glossary.
  - Enhancement (optional, if Hebrew NER available in-gap): run DictaBERT/HeBERT `PERSON` tagger as secondary signal; union with allowlist (after codename exclusion). If NER not deployed, allowlist alone is the gate — documented as such.
  - Mechanics: token-boundary scan → mask spans to sentinels `⟦PERSON_0⟧` before LLM call; model instruction says "Do not translate ⟦PERSON_n⟧". Unmask after. Log any masked name not in allowlists → name-candidate queue (feeds back to `data/person_names/` via review packet).
  - Scope: people only — orgs/places translate normally (user confirmed). Codenames are treated as glossary terms, not people.
  - Failure mode: rare names absent from 592/818 → caught by optional NER or surfaced as `unknown_terms` for human queue; corpus-specific uncommon names get added to allowlist on first review (one-time cost). Codenames accidentally masked → glossary_retention would fail (English expected but Hebrew PERSON marker preserved) and QA would quarantine; with the glossary-driven exclusion the term translates via glossary as intended.
- **CLI:** `python scripts/translate.py [vault_root] [--input DIR] [--glossary PATH] [--out DIR] [--check] [--mock] [--force] [--resume] [--limit N] [--fix-rounds N]` — positional is `vault_root`; `--check` gates on `check_glossary.py`; `--mock` applies glossary then wraps remaining Hebrew as `⟦he:…⟧` (sentinel-safe); `--resume` is alias for default skip-cache.
- **Structured output:** `{"translation": str, "unknown_terms": [str], "notes": [str]}` via OpenAI-compatible `response_format=json_object` (both endpoints support it). No regex sentinel parsing.
- **Zero-guessing rule:** unknown/ambiguous term → `unknown_terms[]` + inline `⟦he:<term>⟧` marker in translation. doc is `blocked_on_term`, not silently guessed. Increments question queue; does not reach vault.
- **Output artifact:** content-addressed store `data/translations/<sha>/translation.md` + frontmatter `{source_doc_id, source_hash, model, policy_version, glossary_version, qa_results, marker_count}`. Hebrew source remains provenance anchor (frontmatter `source: raw/...`). Append-only ledger `data/translations/ledger.jsonl` (canonical `vault_root/data/translations/ledger.jsonl` with `ts` + `glossary_version` on each event). Events: `translation_started/completed`, `qa_result`, `blocked_on_term`, `question_answered` (review_queue), `retranslation_scheduled`.
- **Bounded retries + self-heal:** chunk translation retries fixed max 3 (call_llm internal), plus per-doc QA self-heal fix loop up to `translation.fix_rounds` (default 3, CLI --fix-rounds / env TRANSLATE_FIX_ROUNDS) that re-prompts the LLM with QA failures (chunked for large docs to avoid 12k truncation); logs fix_attempt/qa_result/qa_failed ledger events and exits 1 fail-closed on exhaustion — never `while True`.

### 3b) Deterministic QA gate (scripted, no LLM judge)

`scripts/translation_qa.py` — runs on every translation:

| Check | Threshold | Notes |
|-------|-----------|-------|
| `residual_hebrew_ratio` | `>0.02` quarantine | Person names from allowlist stripped before ratio; `#` comments in CSV ignored; vault-root aware via `--vault-root` (falls back to parent walk) |
| `untranslated_block` | 0 outside ⟦he:⟧ | — |
| `glossary_retention` | 100% approved | Verifies `english` present OR per-token `⟦he:tok⟧` marker for multi-word terms; `keep_source` checks Hebrew presence |
| `glossary_consistency` | — | Via reviewer only (marker-only; divergent-English TODO) — not in QA |
| `heading_fidelity` | exact count/nesting | Requires source; skipped if source not resolved (fragile `parents` heuristic now prefers `vault_root/src_doc`) |
| `structure_fidelity` | lists/tables/code fences counts | — |
| `numeric_fidelity` | numbers/units/part-number refs preserved | — |
| `length_ratio` | `[0.5, 2.5]` | Pre-calibration placeholders — to be fitted from 3–5 Phase-0 approved references (see §6); `docstring` notes this |
| `markup_integrity` | 0 broken fences/links | — |
| `marker_count` | reported; gates stage 6 | per-token, multi-word aware |

- **CLI:** `python scripts/translation_qa.py <store_dir> [--glossary PATH] [--vault-root PATH] [--json-out PATH]` — vault-root aware for source resolution; `--json-out` writes aggregated `[{file,meta,checks}]` JSON.
- Any failure → quarantine → review queue. Batch report: pass rates, marker counts, glossary growth, blocked counts.

### 3c) Self-heal fix loop (QA -> LLM repair)

Integrated into `scripts/translate.py` after each doc assembly (`full_translation = "\n\n".join(chunk_translations)`): run `translation_qa.run_all(source_path, trans_body, meta, glossary, vault_root)`, filter `status==fail`, build repair prompt via `build_fix_prompt` and re-call `call_llm`. Loop up to `fix_rounds` (default 3, config `translation.fix_rounds` / env `TRANSLATE_FIX_ROUNDS` / CLI `--fix-rounds`). For large docs (>12k src or trans) the fix is chunked via `_build_chunked_fix_prompts` (re-uses `chunk_markdown` boundaries, per-chunk glossary/invariants slice, one LLM call per chunk, then `"\n\n".join`) to avoid silent truncation — small docs use single whole-doc prompt. Each attempt logs `fix_attempt` (round, failures_before, chunked flag, src_len/trans_len) + `qa_result`; exhaustion writes `qa_failed` quarantine artifact (`fix_rounds_used`, `qa_failures[:5]`) and `sys.exit(1)` (fail-closed, stops token waste). Cached `qa_failed` artifacts also count toward exit 1 until `--force`. Mock path preserves invariants via `mock_translate`.

### 3d) Prompt / translation instruction

Lives in `data/translation_prompt.md` (editable, versioned with policy). Stage-5 "translation policy" document (`campaigns/<campaign>/translation-policy.md` for multi-campaign; `data/translation_policy.md` for single-corpus template mode):

- Which terms stay Hebrew / transliterated vs translated
- Acronym handling, heading treatment, units/standards, quoted English
- Org-internal names (explicitly NOT person names)
- Calibrated from Phase 0 references, hash versioned.

## 4) Second LLM reviewer — cheap, English-side, sampling

### 4a) Role

Not a re-translator. Three checks, sampling-based (not full corpus every run):

1. **Glossary consistency sweep** — same source term rendered differently across 5+ docs → flag.
2. **AskQE-style (reference-free)** — generate questions from Hebrew source, answer from English translation, compare → divergence = dropped/distorted content. Per stage-5 spec.
3. **Structural spot-check** — heading/table/code-fence drift the deterministic gate missed due to markdown normalization.

### 4b) Model allocation (budget-aware)

- **Reviewer = Kimi K2.7 (limited budget) — sampled 10–20% (per user choice)** — English-side QA is exactly where Kimi is strongest and per-call cost is lowest. Sampling keeps budget safe across thousands of docs. Unlimited bulk stays on MiniMax. Sampling is deterministic: `random.seed(args.seed)` + `random.sample`. Env override `TRANSLATE_REVIEWER_BASE_URL` → `TRANSLATE_BASE_URL` → `QMD_OPENAI_BASE_URL` → `translation.reviewer_base_url` → `translation.base_url`.
- **Translator = MiniMax M2.7 (unlimited)** — Hebrew→English chunk translation (content-aware, glossary-filtered). Bulk propose + bulk translate both on MiniMax.
- **Hebrew open models = offline allowlist support (no NER required in v1)** — `data/person_names/` as deterministic name guard. If HeBERT/DictaBERT is deployed in-gap later, it unions with allowlist as enhancement — not a hard dep. wordfreq stays for term ranking, YAP for lemma grouping.
- **Throughput math:** 10–20% sample → Kimi K2.7 budget covers a full campaign review; ledger records `reviewer_model`, `glossary_version`, `sample` per doc so coverage is auditable. If Kimi K2.7 unavailable, MiniMax as fallback reviewer (flagged as degraded).

### 4c) Output

`data/translations/review_report.json` (keys `sample, total_translations, sampled, reviewer_model, flags, flag_count`) + per-flag markdown cards (`data/translations/review_cards/flag-*.md`). Flags route to expert queue; findings are flags, not auto-rejections. **Limitation:** `glossary_consistency` is currently marker-only (flags leftover `⟦he:term⟧` for approved terms in ≥2 docs); detecting divergent English renderings of the same Hebrew term across docs is TODO. Reference regression: re-translating a reference source must stay within fitted bands or it signals model/policy/prompt drift.

## 5) Human review queue — single queue (glossary/translation only)

Single queue for this stage: glossary / translation questions. Classification queue is future work — not in scope now.

#### Queue: Glossary / translation questions

- When: `translation_seed` has `proposed` rows; `translate.py` emits `unknown_terms` / `⟦he:⟧` markers; reviewer flags a glossary inconsistency.
- Packet fields: term, English gloss candidates (MiniMax proposals), 2–3 real context sentences (different docs), occurrence count, answer box with `approved|keep_source|notes` (`pending` is an interim valid status inside the review queue only — blocked by glossary gate elsewhere).
- Deduplication: one question per term per campaign, ranked by `frequency × blocked_doc_count` (backed by `occurrences, blocked_docs` columns in `review_queue.csv`).
- Effect of answer: bumps `glossary_version` (hash of `glossary.csv`) → `retranslation_scheduled` for all waiting docs (ledger query identifies affected set). Expert never re-reviews unchanged docs.

#### Mechanics

- **Delivery:** CSV file `data/review_queue/review_queue.csv` (one row per open question) plus rendered plain-markdown packets `data/review_queue/<batch>.md` for human reading (~20 questions per packet, generated from the CSV). CSV is source of truth; markdown is view. `glossary_version` is computed (sha256 of `glossary.csv`) and attached to each `question_answered` ledger event.
- **Parser:** `scripts/review_queue.py {list, gen-packets, parse, clean}` — `list` prints pending count; `gen-packets` renders markdown views (`--out-dir`, `--batch N`); `parse` reads the edited CSV (validates `approved|proposed|keep_source|pending` enum, trims whitespace), writes `decided_by: human` + `ts` + `glossary_version` to ledger (canonical `vault_root/data/translations/ledger.jsonl` via `get_ledger_path`, `parents[2]`-aware), triggers retranslation where needed. Ledger also receives `ts` from `translate.py` side. `clean` reports `pending remain` vs `queue is clean`. Markdown packets are regenerated from CSV, not parsed back (avoids markdown-table parsing).
- **Ordering:** by `blocked_doc_count` desc → question age; `~5%` audit sample of deterministic gate decisions mixed in if needed.
- **When NOT to use the queue:** 
  - Never for `gate` auto-rejects (deterministic, final).
  - Never to re-rank models (blind side-by-side calibration is separate).
  - Never for stylistic polish — faithful/greppable, not publication prose.
  - For translation `blocked_on_term`, the human answers the **term** once, not the 50 docs individually.
  - Classification is deferred — do not use this queue for it.

#### Instructions (ship with pipeline)

Add `docs/human-review-queue.md` + section in `example_vault/instructions.md`:
- What goes in the queue vs what does not (table above).
- How to edit the CSV (approve column, save in Sheets/Excel).
- How to run `python scripts/review_queue.py parse data/review_queue/review_queue.csv --dry-run` and re-run `python scripts/translate.py --resume`.
- Expert time budget: ~1 hour for glossary bootstrap sitting is highest leverage; per-batch review trending down as glossary coverage rises.
- Note: classification queue (Queue B) is deferred — data shape is reserved in `review_queue.csv` but not wired now.

## 6) Phase 0 calibration — AI drafts, human verifies and fixes

Workflow: AI (MiniMax M2.7 / Kimi K2.7) produces **3–5 draft reference translations** spanning genres (specification, manufacturer doc, academic essay, team knowledge page, email thread). Expert then **verifies and fixes** each draft thoroughly — post-edits, not rubber-stamps — and approves. Approved references are committed as `campaigns/<campaign>/references/*.md` (or `data/references/` in single-corpus template mode). Used to **fit** QA thresholds (script ratios, length bands) and to derive policy style — NOT to blindly trust.

- Model id/endpoint/params recorded per translation in ledger.
- If expert does not fix, the draft never becomes a reference — unverified drafts stay `proposed`.
- If no expert availability, pipeline runs in degraded mode (glossary proposed, translations `blocked_on_term`) and QA bands fall back to conservative defaults.

## 7) What NOT to build (explicit non-goals)

- No classification logic — queued for stage 6 on English text.
- No security screening.
- No per-document expert translation (expert touches assets, not corpus).
- No stylistic LLM polish.
- No model self-report of RTL corruption — detection stays deterministic in stage 4; only `hebrew_fix.py` repair salvaged (already in `convert_to_md.py`).

## 8) File map (new / modified)

```
NEW  scripts/glossary_translate.py        # translation_seed → glossary_proposed.csv (MiniMax M2.7 proposes all; --mock for CI; harvests 2–3 context sentences)
NEW  scripts/check_glossary.py            # lint: fails if unapproved rows exist (strips # comments, enum approved|proposed|keep_source|pending but only approved passes)
MOD  scripts/translate.py                 # chunk → translate + self-heal QA fix loop (bounded fix_rounds, chunked for large docs, ledger fix_attempt/qa_failed, fail-closed) + allowlist
NEW  scripts/translation_qa.py            # deterministic QA battery (residual_hebrew vault-root-aware, multi-word markers, word-boundary glossary checks)
NEW  scripts/translation_reviewer.py      # Kimi K2.7 sampling reviewer (AskQE + marker-only glossary sweep; --sample/--seed deterministic)
NEW  scripts/review_queue.py              # list|gen-packets|parse|clean for single queue (CSV source of truth; parents[2] vault resolution; ts+glossary_version ledger)
NEW  data/domain_terms/glossary.csv       # editable glossary (committed template: # comment + headers + one example row; ! exception in .gitignore)
NEW  data/translation_policy.md           # editable policy (template)
NEW  data/translation_prompt.md           # prompt template (versioned)
MOD  convert_config.json                  # translation: {base_url, reviewer_base_url, api_key_env, model, reviewer_model, chunk_chars, review_sample, glossary_path, fix_rounds} + _comment (fix_rounds 3, env TRANSLATE_FIX_ROUNDS / --fix-rounds)
MOD  docs/superpowers/plans/document_conversion_pipeline.md # stage-5 row + translation config block
NEW  docs/human-review-queue.md           # queue instructions — single queue, ordered/grouped, when-not-to-use table
NEW  docs/superpowers/specs/stage5-translation-spec-addendum.md # reality corrigendum: MiniMax/Kimi/allowlist/CSV vs Dicta-LM/markdown
MOD  .gitignore                           # waive data/translations/, data/review_queue/, campaigns/; !glossary.csv; .idea; large person_names raw waives
TEST scripts/ (unit + fixtures)           # masking, chunking, QA, ledger; 121 passed
```

All LLM-calling scripts: `requires: TRANSLATE_BASE_URL / TRANSLATE_API_KEY`; `sys.exit(1)` if missing, no silent fallback. Glossary is CSV-only — no markdown table, no `glossary_parse.py` round-trip needed.

## 9) Repo template hygiene

- No domain terms committed — `data/domain_terms/`, `data/translations/`, `data/review_queue/` are waived (`.gitignore`).
- Shipped glossary/policy are **empty templates** with headers + one example row and instructions.
- `data/person_names/` (curated 592 + 818) ships as offline bundle (small, non-sensitive).

## 10) Verification

```bash
# 0) Extract domain terms (prereq: data/domain_terms/translation_seed.csv)
python scripts/extract_domain_terms.py . --top-n 200

# 1) Branch
git rev-parse --abbrev-ref HEAD  # expect feat/hebrew-translation-pipeline
python -m pytest -q               # 121 passed

# 2) Glossary proposal (mock, no keys — contexts auto from raw_md/raw)
python scripts/glossary_translate.py . --mock --out data/domain_terms/glossary_proposed.csv
python scripts/glossary_translate.py . --input data/domain_terms/translation_seed.csv --out data/domain_terms/glossary_proposed.csv --mock  # explicit input variant

# 3) Editable glossary (CSV) — approve in Sheets/Excel
cat data/domain_terms/glossary_proposed.csv | head
# edit status column to approved for rows, then
python scripts/check_glossary.py data/domain_terms/glossary.csv  # exits 1 while unapproved remain
# after approving all:
python scripts/check_glossary.py data/domain_terms/glossary.csv  # exits 0

# 4) Translation (mocked LLM for CI) — positional vault_root, --input names glossary/corpus overrides
python scripts/translate.py . --mock --glossary data/domain_terms/glossary.csv --out data/translations/  # name-mask test: person names stay Hebrew (allowlist 593+818)
python scripts/translation_qa.py data/translations --vault-root . --glossary data/domain_terms/glossary.csv  # seeded-error fixtures: expect catches
python scripts/translation_qa.py data/translations --json-out qa.json --vault-root .  # aggregated [{file,meta,checks}] per doc

# 5) Reviewer sampling (Kimi K2.7, mock)
python scripts/translation_reviewer.py data/translations --sample 0.2 --mock --vault-root .
cat data/translations/review_report.json

# 6) Queue round-trip (single CSV queue — classification deferred)
python scripts/review_queue.py list data/review_queue/review_queue.csv | head
python scripts/review_queue.py gen-packets data/review_queue/review_queue.csv --out-dir data/review_queue --batch 20
# edit CSV (tick approved), then
python scripts/review_queue.py parse data/review_queue/review_queue.csv --dry-run
python scripts/review_queue.py parse data/review_queue/review_queue.csv  # appends question_answered + ts + glossary_version
python scripts/review_queue.py clean data/review_queue/review_queue.csv
```

Note: `check_glossary.py` and all loaders strip leading `#` comment lines and empty lines before CSV parsing. `translation_qa.py` residual excludes allowlist person names; multi-word glossary terms match per-token `⟦he:tok⟧` markers.

Seeded-error fixtures: one doc per QA failure mode (untranslated paragraph, dropped table, glossary violation, truncation, broken fence). Reference regression: re-translating reference source stays within fitted bands — references are AI-drafted + human-fixed (Phase 0). Determinism: same source + same asset versions + same params → same QA verdicts + sampling via `seed`.

Example vault: `python scripts/convert_to_md.py example_vault` then `python scripts/extract_domain_terms.py example_vault --top-n 50` before glossary steps.

## 11) Risks & mitigations

| Risk | Mitigation |
|------|-----------|
| YAP `deps/yap/yap.exe` is Windows-only — Linux CI fast-fails | Document gate; vendor Linux binary or feature-flag YAP with deterministic fallback warning |
| MiniMax proposing all glossary rows anchors expert | Expert overwrites freely; show context snippets so correction is faster than de-novo; track edit rate to detect anchoring |
| MiniMax weak in Hebrew → hallucinates glossary | Hebrew-context snippets bound invention; zero-guessing + markers; sampled Kimi reviewer catches English-side drift |
| Kimi budget exhaustion mid-campaign | Sampling 10–20% (user chose), fallback reviewer = MiniMax, ledger records which docs got which reviewer |
| Person-name allowlist misses rare names (593 curated) | 107k full list available for recall mode; NER as optional union; unknown names surface via review queue → one-time add to allowlist |
| Approved references anchor model choice | Use references only for policy style + QA fitting, never for pairwise model ranking; use blind side-by-side instead |
| Overwritable outputs | Content-addressed `data/translations/<sha>/` + append-only `ledger.jsonl`; never overwrite |
| Unbounded retries | Fixed max + quarantine |
| Template repo cannot evaluate translation quality without domain data | Ship fixtures in `tests/fixtures/translation/` (2 synthetic Hebrew docs → expected glossary/translation/qa outcomes) |

## 12) Sequencing

1. Branch from `yonikremer/feat/domain-terms-moshe` (already has extraction; merge pipeline files if needed).
2. `glossary_translate.py` (MiniMax M2.7 proposes all) + `check_glossary.py` + `glossary.csv` template (unblocks expert).
3. `translate.py` chunking + allowlist name masking + structured output + ledger (core).
4. `translation_qa.py` deterministic gate (must exist before any vault entry).
5. `translation_reviewer.py` (Kimi K2.7 sampling 10–20%).
6. `review_queue.py` (single queue, CSV source of truth) + instructions.
7. Phase 0: AI drafts 3–5 reference translations → human verifies/fixes → fit QA bands + seeded-error fixtures + CI mocks.
