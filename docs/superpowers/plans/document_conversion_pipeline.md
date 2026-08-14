# Document Conversion Pipeline — Plan (revised 2026-08-13)

> Synthesized from original `doctor-mid-nite-static-captain-atom` plan + owner review comments. This file is the authoritative spec for the pipeline going forward.

**Goal:** Batch-convert a vault's `raw/` tree into mirrored markdown in `raw_md/` via an internal HTTP API where needed, with Hebrew OCR-reversal auto-fix driven by a persistent dictionary + `wordfreq`, YAML frontmatter on every output, large-PDF splitting, and dataset-wide deduplication.

---

## 1. Formats

- **In scope:** `txt`, `msg`, `eml`, `docx`, `html`, `htm`, `pptx`, `pdf`
- **Explicitly out of scope:** `xlsx`, `csv` (and any other extension → skipped, reported, not retried)

## 2. Routing table (one converter per extension, no cross-library retry)

| Extension | Converter | Notes |
|-----------|-----------|-------|
| `pdf`, `docx`, `pptx` | docling HTTP API | PDFs must always use docling |
| `html`, `htm` | **pandoc** (`pandoc -f html -t gfm --wrap=none`) | *Change from original plan which routed html via docling* |
| `txt` | `markitdown` (required — fail if missing) | |
| `msg` | `extract_msg` (required) | builds `Subject:/From:/To:/Date:` header block + body (pattern from `worktree stages.py`) |
| `eml` | stdlib `email` | same header block + body |

Failures are reported in `conversion_report.json`, never retried via another library.

## 3. docling-serve API

- `POST {url}/v1/convert/file` multipart (field `files`), sync JSON or `{"task_id": ...}`
- Async: poll `GET {url}/v1/status/poll/{task_id}`, then `GET {url}/v1/result/{task_id}`
- `DoclingClient` isolates endpoint contract, retries/backoff (model: `worktree llm_client.py`)
- `url` configurable (see §4). Default `http://localhost:5001`.

## 4. Config — `<vault>/convert_config.json` (mirrors `pipeline_config.json` pattern)

```json
{
  "docling": { "url": "http://localhost:5001", "workers": 1, "timeout": 300, "retry_delay": 1.0 },
  "pdf": { "split_threshold": 100, "chunk_pages": 50 },
  "hebrew": { "dict_path": "data/hebrew_dict.json", "ambiguity_margin": 2.0 },
  "translation": {
    "base_url": "", "reviewer_base_url": "",
    "api_key_env": "TRANSLATE_API_KEY",
    "model": "minimax-m2.7", "reviewer_model": "kimi-k2.7",
    "chunk_chars": 6000, "review_sample": 0.2,
    "glossary_path": "data/domain_terms/glossary.csv"
  }
}
```

- `translation.base_url` is canonical; `reviewer_base_url` inherits `base_url` unless explicitly set.
- Env precedence: `TRANSLATE_BASE_URL` primary, `QMD_OPENAI_BASE_URL` fallback; reviewer also checks `TRANSLATE_REVIEWER_BASE_URL`.
- `translation.glossary_path` and `chunk_chars`/`review_sample` are consumed by `scripts/translate.py` etc.; no `enabled` key.
- Extra config keys are **allowed** and ignored (waiver).
- `docling.workers` default `1`, configurable; extra keys like `timeout`/`retry_delay` tolerated.
- Deep-merge with defaults; committed example at repo root / `example_vault/`.

## 5. Output layout

Mirror tree: `raw/a/b/file.docx` → `raw_md/a/b/file.md` (via `with_suffix(".md")` on relative path).

Breaking the vault 3-folder model (`raw`/`wiki`/`index`) for `raw_md/` and `data/hebrew_dict.json` is **explicitly waived**.

## 6. YAML frontmatter (per doc, `yaml.safe_dump`)

```yaml
title: <embedded doc metadata → first md heading → filename stem>
created: <doc metadata OR file mtime; omit if <24h old>
original_file: <original filename>
original_ext: <original extension, lowercased>
hebrew_fixed: true  # only when auto-fix fired
```

- `title` priority: `docx`/`pptx` core title, `html` `<title>`, `msg`/`eml` Subject, PDF metadata Title → first markdown heading (`^#+\s+(.+)`) → `path.stem`
- `created` priority: `msg`/`eml` Date header, `docx`/`pptx` core `created`, PDF `CreationDate` (`D:YYYYMMDDhhmmss`) → `path.stat().st_mtime`; if `now - created < 24h` omit field (RECENT_WINDOW)
- Timezone-naive datetimes coerced to UTC.

## 7. Hebrew reversal detection/fix (runs on ALL converted docs, incl. txt/msg/eml/pandoc-html)

**Symptoms:**
- Reversed words: characters of a single word flipped (`םולש` → `שלום`)
- Reversed word order: 2–3 word phrase in reverse order

**Scoring (word-level):** For each Hebrew token (`[֐-׿]+`, length ≥2; single-char tokens never scored — waiver), compare `word` vs `char-reversed` via `wordfreq.word_frequency(lang='he')` (3.1.1) + persistent dictionary frequency. Clear win = `rev_score >= cur * margin` (or `cur==0` when final-form prefilter fires) → fix; close call → leave as-is + flag in `report["ambiguous"]`.

**Scoring (phrase-level):** 2–3 word Hebrew sequences checked against phrase dictionary; `rev_phrase` frequency `>= cur * margin` (or `cur==0`) → fix word order.

**Final-form invariant (cheap pre-filter):** `ך ם ן ף ץ` may only end a word — token starting with one is treated as certain reversal (`cur` forced to `0`). Medial final-forms (`שלםום`) are **not** flagged (waiver).

**Tuning:** `hebrew.ambiguity_margin` (default `2.0`), `DEFAULT_MIN_SCORE=1e-5` floor on `rev_score` to suppress `wordfreq` noise (real words like `שלום ~4e-4`, corpus dict `~1e-3+`).

**Waivers:** Single-char Hebrew, medial final-forms, and extra `hebrew_fix` internals are explicitly out of scope for review.

## 8. Dictionary — `<vault>/data/hebrew_dict.json`

- Persistent `{"words": {...}, "phrases": {...}}` with counts.
- Updated each run; fed **ONLY** by pass-1 textual docs (`txt`/`msg`/`eml`/`docx`/`html`/`htm`/`pptx`), never by OCR'd PDF output.
- `HebrewDictionary` exposes `build_dictionary(texts, dict_path)` and `fix_text(md, dictionary) -> (fixed_md, report)`.
- Accumulates across runs (`load` → `update_from_text` → `save`).

## 9. Large PDFs

- If `pdf_page_count(path) > split_threshold` (default `100`): split into `≤ chunk_pages`-page PDFs (default `50`) in a temp dir via `pypdfium2`, convert each chunk via docling, concatenate chunk mds in order with `\n\n`, delete intermediates.
- `convert(path, client, config)` encapsulates the split-or-direct logic.

## 10. Email attachments

- `msg`/`eml` extract `attachments[(filename, bytes)]`.
- Each attachment converted via same routing table (using `routing_ext = Path(att_name).suffix`), output to `<vault>/raw_md/<parent>/<stem>_attachments/<att_stem>.md` (name-collision de-duplicated with `_<n>` suffix).
- **Attachments are content-deduped against the full dataset (raw files + prior attachments)** via SHA-256 of `att_bytes`: if hash matches any raw file or previously converted attachment, no new file is written; `report["files"]["<mail>#<att>"] = {"status":"duplicate","duplicate_of":canonical,"hash":...}` and the parent email links to the canonical markdown (`- [name](canonical.md) (duplicate of canonical)`). Unique attachments are converted normally and their hash is added to the global `seen` map so later attachments dedup against them.
- Converted attachments linked from parent email md (`## Attachments` → `- [name](rel/link)` for unique, `- [name](canonical) (duplicate ...)` for dupes).

## 11. Re-runs — `should_skip`

- `dst.stat().st_mtime >= src.stat().st_mtime` and not `--force` → skip (`status: skipped, reason: up to date`).
- `--force` reconverts everything (deduplication suppressions remain — see §13).

## 12. Reporting

- `raw_md/conversion_report.json` — per-file `{status, converter, hebrew_fixed, ambiguous, reason/error, duplicate_of, hash}` plus console summary `converted/skipped/failed/duplicate -> report_path`.
- **Dedup logging (added per review):** after summary, if any `status=="duplicate"`, print:
  ```
  dedup: N duplicate(s) suppressed (no raw_md output, see duplicate_of):
    duplicate: <rel> -> <canonical>  hash:<8chars>
  ```
- `hebrew_fix` report surfaces `fixed_words`/`fixed_phrases`/`ambiguous`/`hebrew_fixed` (only `hebrew_fixed`+`ambiguous` persisted per-file in `conversion_report.json` for brevity).

## 13. Deduplication — dataset-wide, content-hash (added per review)

**Requirement:** Filename may differ but content is the same → treat as duplicate across the entire `raw/` dataset.

**Current design (accepted):**

- SHA-256 over file bytes (`8192`-byte streaming), computed before `should_skip`/`--force`.
- `seen: dict[hash → canonical rel_posix]` built over `sorted(all_files)` — lexicographically first wins, deterministic.
- Subsequent identical bytes → `report["files"][rel] = {"status":"duplicate","duplicate_of": seen[hash], "hash": file_hash}` + `continue` (no `raw_md` output — **1:1 file matching is intentionally broken for duplicates; owner accepts this, consumers must follow `duplicate_of`)**.
- Console summary includes `duplicate` count + detailed dedup log (§12).
- **Interplay with `--force`/`should_skip`:** dedup check runs *before* `should_skip`; `--force` does not override dedup (a duplicate stays suppressed even with `--force`).
- **Attachments:** content-deduped against the full dataset — SHA-256 of attachment bytes checked against `seen` (raw files + prior attachments). Duplicates suppress the new file, report `duplicate_of` (raw file `a/b/file.pdf` or prior attachment `mail.eml#att`), and link to the canonical's markdown in the parent email's `## Attachments` section. Unique attachments are added to `seen` for subsequent dedup.
- **Minors explicitly waived as rare:** identical bytes with different extensions colliding (e.g. same bytes as `.txt` and `.eml` routing differently) — not handled; `hash` not stored for `converted`/`skipped` entries (only `duplicate` entries carry it).

## 14. Concurrency

- `ThreadPoolExecutor(max_workers = max(1, cfg["docling"].workers))` — **only `pass2` PDFs use workers** (`convert_batch(pass2, use_workers=True)`); `pass1` textual formats (`docx`/`html`/`htm`/`pptx`/`txt`/`msg`/`eml`) run sequentially. This is **by choice** — fast conversions don't need threading. Owner waiver confirmed.

## 15. Testing

- Synthetic fixtures only (no real `raw/` fixtures in repo).
- `tests/test_convert_to_md.py` — routing, frontmatter (incl. 24h rule), `should_skip`/`--force`, `load_config` merge, docling stub HTTP server for `docx`→docling and `pdf` split, `txt`/`eml` end-to-end.
- `tests/test_hebrew_fix.py` — word-level (clear win → fix, close call → `ambiguous`), phrase-level, `HebrewDictionary` counts/save-load/build.
- `tests/test_docling_convert.py` — `DoclingClient` sync/async/500-retries, `pdf_page_count`/`split_pdf`, `convert` small-vs-large PDF.
- No dedicated dedup tests yet (recommended: 3 cases — identical content different names, sorted winner, `--force`+duplicate).

## 16. Module structure (owner requirement — 3 modules, main imports other two)

```
scripts/convert_to_md.py   — orchestrator: CLI (<vault_root> [--force] [--config PATH]),
                             config loader, routing table, raw/→raw_md/ walk, two-pass
                             orchestration (textual pass builds dict → PDF pass uses it),
                             markitdown/extract_msg/email converters, pandoc html,
                             metadata extraction, frontmatter writer, dedup + attachment
                             handling, conversion_report.json + console summary.

scripts/hebrew_fix.py      — Hebrew reversal: dictionary load/save/update,
                             word-level scoring (wordfreq + dictionary, current vs
                             char-reversed), phrase-order fixing, final-form prefilter,
                             ambiguity margin → leave + flag. Exposes
                             build_dictionary(texts, dict_path) and
                             fix_text(md, dictionary) -> (fixed_md, report).

scripts/docling_convert.py — docling-serve client: DoclingClient (multipart POST
                             {url}/v1/convert/file, async poll, retries/backoff),
                             pdf_page_count + split via pypdfium2, per-chunk convert,
                             concatenate. Exposes convert(path, client, config).
```

**Stage 5 — Hebrew → English translation** (<a id="stage5"></a>see `docs/superpowers/plans/hebrew-translation-pipeline.md` as authority; addendum at `docs/superpowers/specs/stage5-translation-spec-addendum.md`):

```
scripts/glossary_translate.py  — domain glossary bootstrap (MiniMax M2.7 bulk-proposes all terms with 2–3 real context sentences from raw_md/raw → glossary_proposed.csv; --mock for CI)
data/domain_terms/glossary.csv — committed template (headers + one example row + # comments); runtime gate via scripts/check_glossary.py
scripts/check_glossary.py      — CSV gate: fails if glossary missing or any row status != approved (pending is blocked)
scripts/translate.py           — structural heading→paragraph chunking (chunk_chars from config, qmd-aligned), filtered glossary injection (approved|keep_source only, word-boundary), person-name allowlist mask (592 first + 818 last), structured output with ⟦he:⟧ zero-guessing, content-addressed store + ledger (blocked_on_term)
scripts/translation_qa.py      — deterministic QA battery (no LLM): residual_hebrew, untranslated_block, glossary_retention, heading/structure/numeric fidelity, length_ratio, markup_integrity, marker_count
scripts/translation_reviewer.py— sampled reviewer (Kimi K2.7, ~10–20%, deterministic seed): marker-only glossary consistency + AskQE + structure spot-check → review_report.json
scripts/review_queue.py        — single CSV queue (CSV source of truth, markdown view): list | gen-packets | parse --dry-run/--ledger | clean; single queue only, classification deferred
data/translation_policy.md     — policy template (versioned as part of ledger)
data/translation_prompt.md     — prompt template (versioned)
```

Extra modules outside these three (e.g. `hebrew_yap_stemmer.py`) are unrelated and ignored per waiver.

## 17. Explicit waivers / non-goals (from review)

- Breaking the vault 3-folder model for `raw_md/`/`data/` and `convert_config.json` is OK.
- Speculative Generality / extra modules outside required three — ignore.
- Extra config keys — OK.
- Single-char Hebrew never scored — OK.
- Medial final-forms (`שלםום`) ignored — OK.
- Dedup minors (cross-extension collision, hash audit) — rare, not handled.
- Concurrency limited to PDFs — by choice.

## 18. Implementation status

| Step | Status | Notes |
|------|--------|-------|
| `pandoc` for `html`/`htm` | ✅ done | `pandoc` is now required; missing binary fails fast with install hint |
| Content-hash dedup | ✅ done | 1:1 break + `duplicate_of` + dedup log |
| Dedup log | ✅ done | §12 |
| Concurrency | ✅ as-designed | sequential pass1 + threaded pass2 |
| Hebrew `min_score`/phrase flags | known gap | not blocking per waivers |
| `.htm` alias | ✅ done | routed to pandoc |
| `pip install markitdown` | pending | `txt` currently falls back to direct read |

---

*Source: interview rounds + `doctor-mid-nite-static-captain-atom` plan + review comments 2026-08-13. Any future spec change should edit this file and re-lint.*
