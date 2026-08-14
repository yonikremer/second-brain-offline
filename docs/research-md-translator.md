# Research: rockbenben/md-translator — Markdown structure-preserving translation

Date: 2026-08-14
Question: how does `rockbenben/md-translator` keep Markdown structure intact, and how can that idea be ported to Python (stdlib-only) for this vault's Hebrew→English pipeline?
Local clone: `.research/md-translator/` · Upstream: <https://github.com/rockbenben/md-translator> · Live demo: <https://tools.newzone.top/en/md-translator>

All claims cite primary sources (upstream README + source in the clone). Secondary summaries are marked **[via WebFetch]**.

## 1. What it is

**MD Translator** is a browser-based Markdown translator that "solves the broken-formatting problem" by translating only the prose layer while leaving structure byte-perfect (`.research/md-translator/README.md:18-27`, `src/app/lib/translation/formats/markdown.ts:1-7`).

- **Format-preserving**: tokenizes FrontMatter, code blocks, LaTeX, links, image paths, headings, lists, blockquotes, HTML/JSX into `<<<PLACEHOLDER_n>>>` tokens, restores losslessly after translation (`README.md:24`, `markdown.ts:38-49`).
- **Engines**: 7 traditional MT (DeepL/Google/Azure/DeepLX/Qwen-MT/TranslateGemma/GTX) + 17+ LLM providers including MiniMax, OpenAI, Claude, Gemini, plus any **Custom OpenAI-compatible endpoint** (Ollama/vLLM/etc.) (`README.md:54-70`).
- **Privacy-first**: all parsing runs client-side; LLM requests go directly browser→endpoint; API keys in `localStorage`, cache in IndexedDB (`README.md:32`).
- **Stack**: Next.js + TypeScript, Node ≥20.9, `p-limit` + `p-retry` + `spark-md5` (`package.json`, `src/app/lib/translation/pipeline.ts:20-22`).
- **License**: MIT (`LICENSE`, `README.md:3`).

## 2. Architecture (pipeline)

```
Source markdown
  │
  ├─ filterMarkdownLines(lines, mdOptions)   // markdown.ts:180 — protection pass
  │     fullText = lines.join("\n")
  │     1. FrontMatter   /^---\n … \n---/  (YAML-like guard, skip HR)   :182-236
  │     2. Multiline code  fences ≥3×` or ~  (line scan, blockquote-aware) :243-304
  │     3. HTML comments   <!-- … -->       (linear scan, O(n))          :69-100,306-322
  │     4. LaTeX blocks    $$ … $$         (guarded, no blank-line/heading cross):104,332-339
  │     5. Per-line:
  │        inline code  `…`  (linear scan, exact backtick-run matching)  :138-178,347-353
  │        inline LaTeX $…$  (pandoc rules, currency guard)              :356-372
  │        HTML self-closing / close / open tags (quote-aware)           :374-410
  │        images  ![alt](url)  (split prefix/suffix, alt stays)         :412-431
  │        links   [text](url) (translateLinkText toggle)                :434-449
  │        headings  #{1,6}     (prefix → placeholder)                    :452-457
  │        lists   -/* / 1.     (prefix → placeholder)                    :460-465
  │        blockquotes  >       (prefix → placeholder)                    :468-473
  │     → contentLines + contentIndices + sourceLineNumbers + 9 placeholder maps
  │
  ├─ splitMarkdownSegments(contentLines, sourceLineNumbers)  // markdown.ts:599-628
  │     split each line by PLACEHOLDER_SPLIT_REGEX, keep placeholder/empty verbatim,
  │     collect only trimmed text segments → textsToTranslate[] + lineSegments[]
  │
  ├─ pipeline.translateLines(textsToTranslate, config, deps)  // pipeline.ts:1005-1104
  │     three paths:
  │     a) context-aware LLM batch  (documentType && LLM && >1 line) → translateWithContext
  │        wraps each line as [TRANSLATE_i]line[/TRANSLATE_i] + [CONTEXT] neighbors,
  │        builds contextPrompt, extracts via extractTranslatedLinesWithNumbers,
  │        per-batch cache delete on gap, per-line glossary enforcement, progress.
  │     b) chunk-based MT (chunkSize defined) → join non-blank lines by \n or "<>" (deeplx),
  │        split into chunks ≤ chunkSize, translate each chunk, line-count mismatch
  │        triggers per-line rescue (serial + throttled), failedK soft-fill.
  │     c) line-by-line concurrent (default for generic) → pLimit(concurrency),
  │        batched cache probe to skip delayTime on hits, translateSingleWithGlossary per line.
  │
  │     Common machinery (pipeline.ts:34-540):
  │     - translateCore: HAS_TRANSLATABLE_CONTENT guard, IndexedDB cache, service dispatch,
  │       HTML-entity unescape only for gtxFreeAPI/google/webgoogletranslate
  │     - translateSingle: pRetry + per-request AbortController timeout + rateLimitGate wait,
  │       optionalFields forwarding, per-request glossary block (filtered to terms in text),
  │       strictGlossaryTerms retry via SparkMD5-hashed cache key
  │     - enforceGlossaryOnLine: leak-through applyGlossaryToText + one strict retry
  │     - Authed errors abort runController; 429 trips global cooldown; generic errors retried
  │
  ├─ post-process: applyRemoveCharsToSegments → mergeMarkdownSegments → restorePlaceholders
  │     merge: lineSegments × translatedTexts → lines with placeholders re-inserted (:631-639)
  │     removeChars: splitBySpaces + PLACEHOLDER_SPLIT_REGEX guard so tokens never touched (:647-661)
  │     restore: Map(all placeholders) + fixed-point iteration ≤10 passes for nested tokens (:540-562)
  │
  └─ export: per-file, per-language (120+ targets), writing to Hugo/Jekyll/VitePress/Docusaurus i18n trees
```

Headless consumers (CLI, Node server) inject `PipelineCache` + `translate` + `signal` via `PipelineDeps` (`pipeline.ts:53-98`); the web UI's `useTranslationState` hook is one such consumer. `scripts/cli.ts` + `src/app/lib/translation/cliFormat.ts` share `MARKDOWN_DEFAULTS` as single source of truth (`markdown.ts:25-33`).

## 3. Structure-preservation techniques (the core idea to port)

### 3.1 Placeholder vocabulary

```ts
// markdown.ts:38-39 — single source for all 12 token families
"FRONTMATTER_\\d+|MULTILINE_CODE_\\d+|LATEX_BLOCK_\\d+|CODE_\\d+|LATEX_INLINE_\\d+"
"|LINK_PRE_\\d+|LINK_SUF_\\d+|LINK_\\d+|HEADING_\\d+|LIST_\\d+|BLOCKQUOTE_\\d+|HTML_\\d+"
// wrapped as <<<TOKEN>>> — exotic enough the LLM rarely invents it
// PLACEHOLDER_SPLIT_REGEX / TEST / REPLACE built from it at module load (:45-49)
```

`NOT_PLACEHOLDER_LOOKAHEAD = (?!<<<TOKEN>>>)` prevents later passes from eating earlier passes' tokens (e.g. HTML tag regex swallowing `<<<CODE_n>>>` — `markdown.ts:57-58,106-113`).

**Counter seed**: scans the source for any literal `<<<*_digits>>>` and starts counters after the max found (+1, floor 100, 1-9 digit cap to avoid `1e+21` float rendering) — prevents user-supplied literal tokens from colliding with allocated ones (`markdown.ts:198-216`).

### 3.2 Element-by-element

| Element | Strategy | Source |
|---------|----------|--------|
| **YAML FrontMatter** | Whole `^---\n … \n---` block → `<<<FRONTMATTER_n>>>`. Guard: first non-empty line must look like `key:` or `#` comment, otherwise it's an HR `---` (`markdown.ts:221-236`). Toggle `translateFrontmatter` (default false). | `markdown.ts:221-236`, `MARKDOWN_DEFAULTS` |
| **Fenced code** | **Line-scan, not regex** — open = ≥3× same char (`` ` `` or `~`), `` ` `` fence's info string must not contain `` ` ``; close = same char, len ≥ open, only whitespace trimmed. Blockquote prefix `> ` stripped before matching so `> ```js` inside quotes is still fenced. Unclosed fence extends to doc end (CommonMark). O(lines). | `markdown.ts:238-304` |
| **HTML comments** | Linear scan for `<!--`→`-->` with cached cursors (`close`, `` ` ``, placeholder token), O(n). Guards: `<!--` inside backtick span is literal; content must not contain `` ` `` or a placeholder token (prevents `<!--` + fence-token + `-->` cross-token pairing that swallows prose). Whole-document pass, before LaTeX. | `markdown.ts:69-100,306-322` |
| **LaTeX blocks `$$…$$`** | `LATEX_BLOCK_RE` with three guards: not across blank line, not across ATX heading line, not across placeholder token. Nearby empty-line/heading/placeholder comments in source. Toggle `translateLatex`. | `markdown.ts:104,324-339` |
| **Inline code `` `…` ``** | **Linear scan**, not nested-quantifier regex (avoids catastrophic backtracking on unclosed `` ` `` — comment cites 28-char tail ~1.8s exponential). Matches CommonMark rule: N backticks open, exactly N close. Unpaired run kept literal. | `markdown.ts:132-178,347-353` |
| **Inline LaTeX `$…$`** | `(?<!\\)\$(non-space start)…(?!\d)` + trailing `(?!\d)` so `price $100` doesn't pair; pure-number content kept as currency. Toggle `translateLatex`. | `markdown.ts:341,356-372` |
| **HTML tags** | Three passes: self-closing `<tag …/>`, close `</tag>`, open `<tag …>`. Attr segment is quote-aware (`"[^"]*"|'[^']*'`) so `title="a>b"` isn't truncated. Open-tag attr must start with `[a-zA-Z_:@#{` or `/` — prevents `a<b` prose from becoming a fake tag. | `markdown.ts:106-113,374-410` |
| **Images `![alt](url)`** | Always: prefix `![` → `<<<LINK_PRE_n>>>`, suffix `](url)` → `<<<LINK_SUF_n>>>`, alt text stays translatable. URL allows one level of nested parens (Wikipedia `/wiki/A_(b)`). Empty alt → whole image → `<<<LINK_n>>>` (opaque). | `markdown.ts:412-431` |
| **Links `[text](url)`** | If `translateLinkText` (default true): same split as images, only `text` translated. Else whole link opaque. | `markdown.ts:434-449` |
| **Headings `## `** | Prefix `#{1,6}\s` → `<<<HEADING_n>>>`, heading text stays. Byte-perfect marker; translation can't change level. | `markdown.ts:452-457` |
| **Lists `- /* / 1.`** | Prefix `^\s*(?:[-*]|\d+\.)\s+` → `<<<LIST_n>>>`. Preserves marker + indentation. | `markdown.ts:460-465` |
| **Blockquotes `> `** | Prefix `^>\s` → `<<<BLOCKQUOTE_n>>>`. | `markdown.ts:468-473` |
| **Emphasis `**bold**` etc.** | **Not** protected — comment says protecting it "cuts sentences and loses context" (`markdown.ts:475-476`). | `markdown.ts:475-476` |
| **Tables** | Not masked separately — treated as paragraph prose (GFM tables ride through list/heading/blockwise plus link/code guards; pipe handling is prompt-level). | Implicit in `filterMarkdownLines` + `README.md:41` |
| **MDX/Astro `<Alert>`** | Opaque block via HTML tag passes; prose between components still translated. `Ignore Formatting` toggle bypasses all parsing for complex MDX. | `README.md:28,42` |

### 3.3 Why the **order** matters

The source comments are explicit about ordering hazards (`markdown.ts:306-313,324-331`):

1. **FrontMatter → Fences → HTML comments → LaTeX blocks → per-line inline**. HTML comments must run before LaTeX blocks, otherwise `<!-- TODO: fix $$` pairs with a later `$$` in prose and freezes the comment + prose into a fake formula.
2. **Fences before inline code/LaTeX/HTML**: fence placeholder rows contain no backticks/blank lines, so bare `[^`]` or blank-line-guarded LaTeX regexes would otherwise cross fence tokens and swallow prose between two fences.
3. **Inline code before inline LaTeX/HTML**: `` `<!--` `` or `` `$$` `` inside a code span must not open a comment/formula.

Each regex that could cross a placeholder carries `NOT_PLACEHOLDER_LOOKAHEAD` (`markdown.ts:57-58,104,112`).

### 3.4 Source line numbers (failure panel)

Four whole-document categories collapse to one line each (frontmatter, multiline code, HTML comments, LaTeX blocks). `sourceLineNumbers` reconstructs the 1-based physical source line per `contentLine` by counting `\n` inside each collapsed placeholder's original (`markdown.ts:488-503`). Without this, the failure modal would point at the collapsed index, not the file.

### 3.5 Segmentation & restoration

- **Split**: `PLACEHOLDER_SPLIT_REGEX` (capturing) partitions each `contentLine` into `placeholder | empty (all-whitespace) | text` (`markdown.ts:599-628`). Only `text` segments (trimmed, with leading/trailing space saved) enter `textsToTranslate[]`; `empty` never goes to the translator (would be swallowed or hallucinated).
- **Merge**: `mergeMarkdownSegments` re-interleaves `leading + translatedTexts[index] + trailing` (`markdown.ts:631-639`).
- **Restore**: single `Map` of all 9 maps, `String.replace(PLACEHOLDER_REPLACE_REGEX, Map.get ?? match)` in a fixed-point loop ≤10 iterations — handles nesting (inner `<<<CODE_n>>>` inside an outer `<<<HTML_m>>>` value) (`markdown.ts:540-562`). `?? match` leaves user-supplied literal tokens untouched.
- **`removeChars`**: splits by the same `PLACEHOLDER_SPLIT_REGEX` and only strips from non-placeholder segments — deleting a char inside `<<<CODE_100>>>` would corrupt the token and drop the protected block (`markdown.ts:644-661`).

### 3.6 Pipeline-level preservation

- **Context-aware LLM batch**: `translateWithContext` (`pipeline.ts:594-995`) sends `contextWindow` lines per request, each target wrapped as `[TRANSLATE_i]…[/TRANSLATE_i]` and neighbors as `[CONTEXT]…[/CONTEXT]`; extracts by numbered markers with echo-guard and merge-guard; on `hasRealGap` deletes the batch cache key so retries hit the live service, not the cached bad response.
- **Soft-fill**: any slot still `undefined` after retries is filled with the original source line and recorded in `failures` (with `line` = physical source line, `index` = array index, `lang`/`file`) — output is always complete, failures surfaced in `TranslateFailurePanel` (`pipeline.ts:970-986`).
- **Cache**: key = `SparkMD5(text + cacheSuffix)` where `cacheSuffix` hashes `{sourceLang, targetLang, method, config, systemPrompt, userPrompt, glossaryTerms}` (`pipeline.ts:304-341,1081-1092`); per-line cache (`cache.getMany`) and batch cache; purge on mismatch.
- **Retry**: per-request `AbortController` timeout, `pRetry` with method-specific backoff, global `rateLimitGate` cooldown on 429, circuit breaker after 3 consecutive dry clusters, 10s/1.5s auto-retry pass before soft-fill (`pipeline.ts:359-540,751-870`).

## 4. How this maps to the vault's Python pipeline

### 4.1 What this vault already does

`scripts/translate.py` uses **preservation-by-verification**, not masking:

- Chunks at heading/paragraph boundaries, never mid-fence/table (`translate.py:484-559`).
- Extracts invariants from the source chunk — `yaml_frontmatter`, `code_sections` (fenced+inline), `person_names` (allowlist `data/person_names/`), `english_spans`, `urls_and_paths` — via regexes (`translate.py:68-86,340-423`), passes them as explicit verbatim context in the prompt (`build_prompt` → `preserve_block` listing JSON arrays, "MUST appear exactly and in same relative order"), then verifies with `verify_all_preserved` / `verify_all_ordered` / `verify_global_order` after translation (`translate.py:425-481,594-635,874-890`).
- Filtered glossary (only `term_he` occurring at word boundaries) (`translate.py:562-591`), structured JSON output `{translation, unknown_terms, notes}` with `response_format=json_object` (`translate.py:653-677`), `⟦he:term⟧` markers for unknowns.
- Content-addressed store `data/translations/<sha>/translation.md` + `ledger.jsonl` (`translate.py:88-102,912-939`).

Verification is a safety net, but a single slipped `|` or translated URL still requires a retry — the model is trusted to obey the prompt.

### 4.2 What md-translator does differently

| Aspect | Vault (`translate.py`) | md-translator (`markdown.ts` + `pipeline.ts`) |
|--------|------------------------|-----------------------------------------------|
| **Core guarantee** | Prompt + verify (soft, retry on miss) | Mask → translate → restore (hard, structure never reaches the model) |
| **Parsing** | Heading/paragraph chunker + regex invariants | Whole-document placeholder pass + per-line segmentation; fences via line-scan, inline code via linear scan (not regex), HTML via quote-aware regexes |
| **Code/LaTeX/HTML/links** | Listed in prompt, verified after | Replaced by `<<<TOKEN_n>>>` before translation, restored after — model never sees them |
| **English spans** | Extracted + listed in prompt as "keep verbatim" | Not treated specially — Latin prose is just prose; code/URL masking already protects identifiers/paths |
| **FrontMatter** | Included in chunk (re-attached to first chunk) | Optional toggle; when protected, whole block opaque |
| **Line numbers** | Not tracked (chunk-level) | `sourceLineNumbers` maps collapsed lines back to physical lines for failure UI |
| **Segmentation** | Chunk = unit | Line → placeholder/empty/text segments; only text segments translated (placeholder never translated) |
| **Retry** | 3 retries on `call_llm` HTTP | `pRetry` per request + `rateLimitGate` + window-halving + gap-cluster retry + auto-retry + soft-fill |
| **Cache** | Content-addressed output (hash of source) | Per-request IndexedDB cache keyed by `hash(text + method+langs+config+glossary)` + batch cache |
| **RemoveChars** | N/A | Placeholder-aware (never touches tokens) |

The two are **complementary**, not mutually exclusive. Masking is strongest for code/URLs/math/HTML (where verification is weakest); verification is strongest as a final invariant check (where masking could still be bypassed if the model drops a token).

## 5. Porting to Python (stdlib-only)

The vault's CLI constraint is **pure stdlib** (`CLAUDE.md: Conventions — Pure stdlib. No runtime deps in pyproject.toml`). md-translator's placeholder engine is a good fit: it is ** pure regex + line-scan**, no AST library, no Node. The entire `markdown.ts` protection pass can be reimplemented with `re` + string scanning.

### 5.1 Minimal vendor: one new module

Create `scripts/md_mask.py` (or `src/second_brain_vault_framework/payload/scripts/md_mask.py` if it ships in the vault — check `manifest.json: owned_paths` before adding) containing a faithful Python port of `markdown.ts:38-562`. Keep the placeholder names identical (`<<<FRONTMATTER_n>>>` etc.) so logs/cache remain comparable.

```python
# Sketch — names mirror markdown.ts so diffing is trivial
import re

PLACEHOLDER_PATTERN = r"FRONTMATTER_\d+|MULTILINE_CODE_\d+|LATEX_BLOCK_\d+|CODE_\d+|LATEX_INLINE_\d+|LINK_PRE_\d+|LINK_SUF_\d+|LINK_\d+|HEADING_\d+|LIST_\d+|BLOCKQUOTE_\d+|HTML_\d+"
PLACEHOLDER_SPLIT_RE = re.compile(rf"(<<<{(PLACEHOLDER_PATTERN)}>>>)")
PLACEHOLDER_TEST_RE  = re.compile(rf"^<<<{(PLACEHOLDER_PATTERN)}>>>$")
PLACEHOLDER_REPLACE_RE = re.compile(rf"<<<{(PLACEHOLDER_PATTERN)}>>>")
NOT_PLACEHOLDER = rf"(?!<<<{(PLACEHOLDER_PATTERN)}>>>)"

# markdown-it-py is NOT needed — the whole pass is re + line-scan.
# If you prefer an AST, add markdown-it-py as an optional extra, but the
# regex port satisfies the stdlib constraint and matches upstream behavior.

def protect_inline_code(line: str, store) -> str:
    # linear scan: count backtick run length N, find exactly-N closing run — markdown.ts:138-178
    ...

def protect_html_comments(text: str, store) -> str:
    # linear scan with cached cursors — markdown.ts:69-99
    ...

LATEX_BLOCK_RE = re.compile(rf"\$\$(?:(?!\n[ \t]*\n)(?!\n[ \t]*#{{1,6}}[ \t]){NOT_PLACEHOLDER}[^`])*?\$\$", re.DOTALL)
HTML_SELF_CLOSING_RE = re.compile(rf"<([a-zA-Z][a-zA-Z0-9-]*)(?:\s+[a-zA-Z_:@#{{](?:{NOT_PLACEHOLDER}[^>\"']|\"[^\"]*\"|'[^']*')*?|\s*)\/>")
HTML_OPEN_TAG_RE       = re.compile(rf"<([a-zA-Z][a-zA-Z0-9-]*)(?:\s+[a-zA-Z_:@#](?:{NOT_PLACEHOLDER}[^>\"']|\"[^\"]*\"|'[^']*')*|\s*\/|\s+)?>")
```

Then port `filter_markdown_lines` (`markdown.ts:180-519`), `split_markdown_segments` / `merge_markdown_segments` (`markdown.ts:599-639`), `restore_placeholders` (`markdown.ts:540-562`), and `apply_remove_chars_to_markdown` (`markdown.ts:644-661`) verbatim, preserving:

- counter-seed scan for literal placeholder collision avoidance (`markdown.ts:198-216`)
- frontmatter YAML guard (`markdown.ts:223-230`)
- fence line-scan with `BQ_PREFIX_RE` for `> ```js` (`markdown.ts:262-304`)
- inline LaTeX currency/whitespace guards (`markdown.ts:361-367`)
- URL one-level paren nesting (`markdown.ts:413-449`)
- `sourceLineNumbers` reconstruction from collapsed placeholder newlines (`markdown.ts:488-503`)
- fixed-point restore loop ≤10 (`markdown.ts:554-560`)

**Estimated size**: ~450-550 lines of Python (the TS source is 662 lines, much is comments).

### 5.2 Wiring into `translate.py`

1. **Before** `build_prompt` in `translate.py:858-867`, call `md_mask.filter_markdown_lines` on `chunk_text` to get `contentLines` + placeholder maps. Then `split_markdown_segments` to get `textsToTranslate`.
2. **Translate** `textsToTranslate` (not the raw chunk) via `call_llm` — each segment is already trimmed; leading/trailing space is stored in `lineSegments` and re-added on merge, so the model sees clean prose.
3. **After** translation, `merge_markdown_segments` then `restore_placeholders` (before any `removeChars` — restoring first prevents char-stripping from corrupting tokens, same order as `markdown.ts:647-661` comment).
4. **Keep the existing verification** (`verify_all_preserved` / `verify_all_ordered` / `verify_global_order` on the restored output) as defense-in-depth — masking handles 95% of cases; verification catches the remainder (e.g. model dropped a `<<<LIST_n>>>` token).
5. For **Obsidian `[[wikilinks]]` / `![[embeds]]`** (not CommonMark, so md-translator doesn't handle them): add a pre-mask regex `r"!?\[\[[^\]]+\]\]"` before the `filter_markdown_lines` pass, same pattern `docs/research-hebrew-md-translation.md:276-278` already documents. Store in a `wikilinkPlaceholders` map and include in `restore_placeholders`.

### 5.3 What NOT to port

- `pipeline.ts`'s `pLimit`/`pRetry`/`rateLimitGate`/IndexedDB/circuit-breaker machinery — `translate.py:653-691` already has bounded retries (max 3) and `urllib.request` with timeout; adding a global rate-limit gate is worthwhile but is a separate concern from Markdown preservation.
- The Next.js UI, `next-intl`, `ToolPage` shell, and all React hooks — irrelevant to the CLI pipeline.
- `contextTranslation.ts` / `translateWithContext` — useful for long-document coherence but orthogonal to structure preservation; evaluate separately.

### 5.4 Testing

- **Golden-file tests**: take 5-10 representative vault notes (headings, fences, inline code, LaTeX `$$…$$` + `$…$`, links/images, tables, blockquotes, HTML `<span>`/`<!-- -->`, wikilinks) and assert `filter → restore` round-trips byte-identical when translation is mocked as identity — this validates the mask/restore layer in isolation.
- **Property test**: `restore_placeholders(filter_markdown_lines(text).maps, merge(...)) == text` for any `text` that doesn't contain literal placeholders (or does — then counter-seed must avoid collision).
- **Failure injection**: mock `call_llm` to return output with a dropped `<<<CODE_n>>>` and assert verification catches it (`verify_all_preserved` non-empty → retry path).

## 6. Recommendation

- **Do not vendor the Next.js app** (`md-translator` is a browser tool, not a pipeline library). Its value to this vault is the **~600 lines of `markdown.ts` protection logic + the placeholder vocabulary/order/restore design**, which ports cleanly to stdlib Python.
- **Do vendor the idea** as `scripts/md_mask.py` (stdlib-only, no new deps), wire it into `scripts/translate.py` as a **mask-before / restore-after** layer, and **keep the existing prompt+verification** (`translate.py:425-481,874-890`) as a second layer. This matches the ranked recommendation in `docs/research-hebrew-md-translation.md:501-522` (Pattern A mask + Pattern C verify) and gives hard guarantees where prompt obedience is weakest (code/LaTeX/HTML/URLs).
- **Keep the on-prem MiniMax M2.7 endpoint** (`convert_config.json: translation.model = minimax-m2.7`, `TRANSLATE_BASE_URL`) — md-translator's `Custom (OpenAI-compatible)` provider proves the same HTTP shape works; no engine change needed. Validate Hebrew quality via the existing Phase-0 review queue and `residual_hebrew_ratio` / `length_ratio` gates (`docs/research-hebrew-md-translation.md:475-499`).
- If an AST-based alternative is ever wanted, `mdpo` (Markdown→PO→translate→rebuild, CommonMark, BSD-3, `docs/research-hebrew-md-translation.md:170-182`) is the principled parser-based option, but it adds a PO artifact and per-paragraph context loss — the regex placeholder port is simpler and sufficient for this vault's needs.

## 7. Caveats

- md-translator's **tables** are GFM and not separately masked — its table preservation relies on the LLM not emitting stray `|` (prompt-level). If tables prove fragile after masking, add cell-by-cell JSON translation as `docs/research-hebrew-md-translation.md:237-245,469` suggests.
- **Obsidian wikilinks** (`[[…]]`) are outside CommonMark/GFM — md-translator doesn't handle them; the Python port must add them explicitly (§5.2.5).
- **Emphasis** (`**bold**`) is intentionally **not** masked upstream — translating it is safe; masking it would cut sentences (`markdown.ts:475-476`). Keep this.
- md-translator's **"Context-Aware Translation"** caveat notes that surrounding-paragraph context can raise formatting error risk (`README.md:78` **[via WebFetch]**) — the Python port should default to segment-level context (previous chunk tail, as `translate.py:611-612` already does) unless whole-document coherence is needed.
- No hands-on benchmark was run on this vault's corpus; the ranking rests on published benchmarks and structural analysis.

## 7. Implementation (this vault) — divergence for tables

The stdlib port lives in `scripts/md_mask.py` (Tasks 1-5 of `docs/superpowers/plans/2026-08-14-md-mask-table-preservation.md`).

**Divergence from upstream for tables:** `md-translator` treats tables as GFM prose (no cell-level protection; pipe handling is prompt-level). This vault adds **cell-by-cell masking** (`<<<TABLE_n>>>` for separator rows, `<<<TABLE_CELL_n>>>` per cell via `_split_table_cells` that respects escaped `\|` and masked inline `CODE` tokens). Only `TABLE_CELL` values are sent to the LLM; pipes/separators/colons never reach it. `scripts/translation_qa.py:check_table_fidelity` (Task 7) quarantines on column/row/separator drift — strict, fail-closed (`data/translation_policy.md: Tables`).

**Obsidian wikilinks** (`[[...]]` / `![[...]]`) are also added as `<<<WIKILINK_n>>>` opaque tokens — upstream has no such concept (not CommonMark).

Integration: `scripts/translate.py` masks before `build_prompt` and restores after `call_llm`; segment-count mismatch raises `RuntimeError` (fail closed). Existing `verify_all_preserved` stays as second layer. See `tests/test_md_mask.py` + `tests/fixtures/md_mask/` for golden roundtrips.

---
Sources: `.research/md-translator/README.md`, `.research/md-translator/src/app/lib/translation/formats/markdown.ts`, `.research/md-translator/src/app/lib/translation/pipeline.ts`, `.research/md-translator/src/app/lib/translation/types.ts` + `config.ts` + `cache.ts`, `scripts/translate.py`, `docs/research-hebrew-md-translation.md`, <https://github.com/rockbenben/md-translator> (WebFetch 2026-08-14).
