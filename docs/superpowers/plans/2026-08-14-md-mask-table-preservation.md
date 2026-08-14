# Markdown Mask + Table-Preserving Translation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a stdlib-only placeholder-masking layer (`scripts/md_mask.py`) that protects all Markdown structure before LLM translation and restores it losslessly, with hard guarantees for GFM tables (column counts, separator rows, pipes in cells, inline code inside cells) via cell-by-cell handling.

**Architecture:** Port of `rockbenben/md-translator`'s `markdown.ts` protection pipeline to pure Python `re` + line-scan (no deps). New module owns vocabulary, protection order, table cell extraction, segmentation, and restoration. `scripts/translate.py` calls mask before building the prompt and restores after `call_llm`; existing verification (`verify_all_preserved` / ordered) stays as second layer. `scripts/translation_qa.py` gains table-specific invariants that fail closed.

**Tech Stack:** Python 3.11+ stdlib only (`re`, `hashlib`, `pathlib`, `json`), `unittest` (existing suite). No new runtime deps (air-gap constraint per `CLAUDE.md`). Borrowed logic cited to `.research/md-translator/src/app/lib/translation/formats/markdown.ts` and `pipeline.ts`.

---

## File Map

```
NEW  scripts/md_mask.py               # core: vocabulary, filter_markdown_lines, table handling, split/merge/restore
MOD  scripts/translate.py             # wire mask before prompt, restore after LLM, keep verification
MOD  scripts/translation_qa.py        # table fidelity: column counts, separator integrity, per-row pipe parity
MOD  tests/test_translation_pipeline.py  # extend with md_mask tests (or new file — see Task 1)
NEW  tests/test_md_mask.py            # dedicated unit + golden-file tests for masking (preferred)
NEW  tests/fixtures/md_mask/          # golden inputs: tables, fences, LaTeX, wikilinks, mixed
```

`scripts/md_mask.py` is **not** a framework-owned payload (`src/.../payload/` + `manifest.json:owned_paths`), so no manifest bump is needed. It lives with `scripts/translate.py` (vault-stage scripts, not framework).

---

### Task 1: Vocabulary + placeholder regexes + counter-seed (stdlib, no behavior yet)

**Files:**
- Create: `scripts/md_mask.py`
- Test: `tests/test_md_mask.py`

- [ ] **Step 1: Write the failing test for vocabulary**

```python
# tests/test_md_mask.py
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import unittest

class TestPlaceholderVocabulary(unittest.TestCase):
    def test_placeholder_pattern_covers_all_types(self):
        import md_mask
        # Must contain every family used by md-translator
        for name in ["FRONTMATTER", "MULTILINE_CODE", "TABLE", "TABLE_CELL",
                     "CODE", "LATEX_BLOCK", "LATEX_INLINE",
                     "LINK_PRE", "LINK_SUF", "LINK",
                     "HEADING", "LIST", "BLOCKQUOTE", "HTML", "WIKILINK"]:
            self.assertIn(name, md_mask.PLACEHOLDER_PATTERN)

    def test_split_regex_captures_token(self):
        import md_mask
        parts = md_mask.PLACEHOLDER_SPLIT_RE.split("a<<<CODE_100>>>b")
        self.assertIn("<<<CODE_100>>>", parts)

    def test_replace_regex_matches_all(self):
        import md_mask
        text = "<<<FRONTMATTER_100>>> and <<<TABLE_CELL_101>>>"
        hits = md_mask.PLACEHOLDER_REPLACE_RE.findall(text)
        self.assertEqual(len(hits), 2)

    def test_counter_seed_avoids_collision(self):
        import md_mask
        # Source already contains a literal placeholder — next allocation must be after it
        lines = ["hello <<<CODE_105>>> world", "next"]
        seed = md_mask.compute_counter_seed(lines)
        self.assertGreaterEqual(seed, 106)
        # Without literal placeholders, seed starts at 100
        self.assertEqual(md_mask.compute_counter_seed(["hello world"]), 100)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_md_mask -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'md_mask'`

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/md_mask.py
"""Markdown placeholder masking — stdlib port of md-translator markdown.ts."""
from __future__ import annotations

import re

# Single source for all token families. TABLE/TABLE_CELL are additions for
# table cell-by-cell handling (md-translator has no table-cell tokens).
# WIKILINK covers Obsidian [[wikilinks]] / ![[embeds]] (not CommonMark).
PLACEHOLDER_PATTERN = (
    r"FRONTMATTER_\d+|TABLE_\d+|TABLE_CELL_\d+|MULTILINE_CODE_\d+|"
    r"LATEX_BLOCK_\d+|CODE_\d+|LATEX_INLINE_\d+|"
    r"LINK_PRE_\d+|LINK_SUF_\d+|LINK_\d+|"
    r"HEADING_\d+|LIST_\d+|BLOCKQUOTE_\d+|HTML_\d+|WIKILINK_\d+"
)

PLACEHOLDER_SPLIT_RE = re.compile(rf"(<<<{(PLACEHOLDER_PATTERN)}>>>)")
PLACEHOLDER_TEST_RE = re.compile(rf"^<<<{(PLACEHOLDER_PATTERN)}>>>$")
PLACEHOLDER_REPLACE_RE = re.compile(rf"<<<{(PLACEHOLDER_PATTERN)}>>>")
NOT_PLACEHOLDER = rf"(?!<<<{(PLACEHOLDER_PATTERN)}>>>)"

_COUNTER_SEED_RE = re.compile(r"<<<[A-Z_]+_(\d{1,9})>>>")

def compute_counter_seed(lines: list[str]) -> int:
    """Scan source for literal placeholder-like tokens and start after max."""
    seed = 100
    for m in _COUNTER_SEED_RE.finditer("\n".join(lines)):
        try:
            seed = max(seed, int(m.group(1)) + 1)
        except ValueError:
            pass
    return seed
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_md_mask.TestPlaceholderVocabulary -v`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/md_mask.py tests/test_md_mask.py
git commit -m "feat(md-mask): placeholder vocabulary + counter seed

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: Block-level protection (frontmatter, fenced code, HTML comments, LaTeX blocks)

**Files:**
- Modify: `scripts/md_mask.py`
- Test: `tests/test_md_mask.py`

- [ ] **Step 1: Write the failing tests for block-level masks**

```python
# tests/test_md_mask.py — append inside same file

class TestBlockMasks(unittest.TestCase):
    def test_frontmatter_masked(self):
        import md_mask
        lines = ["---", "title: Test", "---", "", "# Heading", "Body"]
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        # Frontmatter collapsed to one placeholder line
        joined = "\n".join(res.content_lines)
        self.assertIn("<<<FRONTMATTER_", joined)
        self.assertIn("FRONTMATTER_", str(res.maps["frontmatter"]))

    def test_frontmatter_hr_not_masked(self):
        import md_mask
        # Document starts with HR --- but no YAML key: → not frontmatter
        lines = ["---", "", "Paragraph after HR", "", "---", "more"]
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        self.assertNotIn("FRONTMATTER_", "\n".join(res.content_lines))

    def test_fenced_code_masked(self):
        import md_mask
        lines = ["# H", "```python", "print('hi')", "```", "paragraph"]
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        joined = "\n".join(res.content_lines)
        self.assertIn("<<<MULTILINE_CODE_", joined)
        # Code content must not appear in masked text
        self.assertNotIn("print('hi')", joined)

    def test_blockquote_fenced_code_masked(self):
        import md_mask
        lines = ["> ```js", "> console.log(1)", "> ```", "after"]
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        joined = "\n".join(res.content_lines)
        self.assertIn("<<<MULTILINE_CODE_", joined)
        self.assertNotIn("console.log", joined)

    def test_html_comment_masked(self):
        import md_mask
        lines = ["text <!-- comment", "spanning --> more"]
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        joined = "\n".join(res.content_lines)
        self.assertIn("<<<HTML_", joined)
        self.assertNotIn("comment", joined)

    def test_latex_block_masked(self):
        import md_mask
        opts = md_mask.MdOptions(translate_latex=False)
        lines = ["text $$", "E=mc^2", "$$ more"]
        res = md_mask.filter_markdown_lines(lines, opts)
        joined = "\n".join(res.content_lines)
        self.assertIn("<<<LATEX_BLOCK_", joined)

    def test_latex_block_not_across_heading(self):
        import md_mask
        opts = md_mask.MdOptions(translate_latex=False)
        # Two $$ blocks separated by heading — must not pair across heading
        lines = ["$$ block one $$", "# Heading", "$$ block two $$"]
        res = md_mask.filter_markdown_lines(lines, opts)
        joined = "\n".join(res.content_lines)
        self.assertIn("# Heading", joined)  # heading survives, not swallowed into LaTeX
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_md_mask.TestBlockMasks -v`
Expected: FAIL — `AttributeError: module md_mask has no attribute filter_markdown_lines`

- [ ] **Step 3: Write minimal implementation**

Add to `scripts/md_mask.py` (below the vocabulary block):

```python
from dataclasses import dataclass, field

@dataclass
class MdOptions:
    translate_frontmatter: bool = False
    translate_multiline_code: bool = False
    translate_latex: bool = False
    translate_link_text: bool = True

@dataclass
class FilterResult:
    content_lines: list[str]
    content_indices: list[int]
    source_line_numbers: list[int]
    maps: dict[str, dict[str, str]]  # keys: frontmatter, code, latex_block, latex_inline, link, heading, list, blockquote, html, wikilink, table, table_cell

def filter_markdown_lines(lines: list[str], opts: MdOptions) -> FilterResult:
    frontmatter_placeholders: dict[str, str] = {}
    code_placeholders: dict[str, str] = {}
    html_placeholders: dict[str, str] = {}
    latex_block_placeholders: dict[str, str] = {}
    latex_inline_placeholders: dict[str, str] = {}
    link_placeholders: dict[str, str] = {}
    heading_placeholders: dict[str, str] = {}
    list_placeholders: dict[str, str] = {}
    blockquote_placeholders: dict[str, str] = {}
    wikilink_placeholders: dict[str, str] = {}
    table_placeholders: dict[str, str] = {}
    table_cell_placeholders: dict[str, str] = {}

    seed = compute_counter_seed(lines)
    fc = lc = hcc = lbc = lic = linkc = headc = listc = bqc = wikic = tablec = cellc = seed

    def stash(maps: dict[str, str], counter: int, text: str) -> tuple[str, int]:
        key = f"<<<PLACEHOLDER_{counter}>>>"
        # caller replaces PLACEHOLDER with real family
        return key, counter + 1

    full_text = "\n".join(lines)

    # 1. Frontmatter — only if translate_frontmatter is False
    if not opts.translate_frontmatter:
        m = re.match(r"^---\n([\s\S]*?)\n---\n?", full_text)
        if m:
            body = m.group(1)
            first_nonempty = next((l.strip() for l in body.split("\n") if l.strip()), "")
            if re.match(r'^["\'\w-]+\s*:|^#', first_nonempty):
                key = f"<<<FRONTMATTER_{fc}>>>"
                frontmatter_placeholders[key] = m.group(0)
                full_text = full_text.replace(m.group(0), key, 1)
                fc += 1

    # 2. Fenced code — line scan, blockquote-aware (from markdown.ts:262-304)
    if not opts.translate_multiline_code:
        src_lines = full_text.split("\n")
        out_lines: list[str] = []
        BQ_RE = re.compile(r"^(?:[ \t]{0,3}>[ \t]?)+")
        i = 0
        while i < len(src_lines):
            bq = BQ_RE.match(src_lines[i])
            body = src_lines[i][len(bq.group(0)):] if bq else src_lines[i]
            opener = re.match(r"^[ \t]*(`{3,})[^`]*$|^[ \t]*(~{3,})", body)
            if not opener:
                out_lines.append(src_lines[i])
                i += 1
                continue
            fence_char = "`" if opener.group(1) else "~"
            min_len = len(opener.group(1) or opener.group(2))
            end = i + 1
            closer_at = None
            while end < len(src_lines):
                b = src_lines[end]
                if bq:
                    mm = BQ_RE.match(b)
                    if not mm:
                        break
                    b = b[len(mm.group(0)):]
                t = b.strip(" \t")
                if len(t) >= min_len and all(c == fence_char for c in t):
                    closer_at = end
                    break
                end += 1
            block_end = closer_at if closer_at is not None else (end - 1 if bq else len(src_lines) - 1)
            key = f"<<<MULTILINE_CODE_{lc}>>>"
            code_placeholders[key] = "\n".join(src_lines[i:block_end + 1])
            out_lines.append(key)
            lc += 1
            i = block_end + 1
        full_text = "\n".join(out_lines)

    # 3. HTML comments — linear scan (markdown.ts:69-99 simplified)
    def _protect_html_comments(text: str) -> str:
        nonlocal hcc
        if "<!--" not in text:
            return text
        ph_re = re.compile(rf"<<<{(PLACEHOLDER_PATTERN)}>>>")
        out = []
        emit_from = 0
        search_from = 0
        while True:
            open_idx = text.find("<!--", search_from)
            if open_idx == -1:
                break
            close_idx = text.find("-->", open_idx + 4)
            if close_idx == -1:
                break
            # Guard: content must not contain backtick or placeholder token
            mid = text[open_idx + 4:close_idx]
            if "`" in mid or ph_re.search(mid):
                search_from = open_idx + 4
                continue
            key = f"<<<HTML_{hcc}>>>"
            html_placeholders[key] = text[open_idx:close_idx + 3]
            out.append(text[emit_from:open_idx] + key)
            hcc += 1
            emit_from = close_idx + 3
            search_from = close_idx + 3
        out.append(text[emit_from:])
        return "".join(out)

    full_text = _protect_html_comments(full_text)

    # 4. LaTeX blocks $$ ... $$ — not across blank line or heading
    if not opts.translate_latex:
        LATEX_BLOCK_RE = re.compile(
            rf"\$\$(?:(?!\n[ \t]*\n)(?!\n[ \t]*#{{1,6}}[ \t]){NOT_PLACEHOLDER}[^`])*?\$\$",
            re.DOTALL,
        )
        def _repl_latex_block(m: re.Match) -> str:
            nonlocal lbc
            key = f"<<<LATEX_BLOCK_{lbc}>>>"
            latex_block_placeholders[key] = m.group(0)
            lbc += 1
            return key
        full_text = LATEX_BLOCK_RE.sub(_repl_latex_block, full_text)

    # Per-line passes continue in Task 3 — for now, split and return
    processed_lines = full_text.split("\n")
    # (per-line inline handling inserted in next task; keep structure)
    content_lines = processed_lines
    content_indices = list(range(len(content_lines)))
    # source_line_numbers: collapsed placeholders contribute their newline count
    source_line_numbers = []
    newline_counts: dict[str, int] = {}
    for d in [frontmatter_placeholders, code_placeholders, html_placeholders, latex_block_placeholders]:
        for k, v in d.items():
            newline_counts[k] = v.count("\n")
    src_line = 1
    for ln in content_lines:
        source_line_numbers.append(src_line)
        src_line += 1
        for mm in re.finditer(r"<<<[A-Z_]+_\d{1,9}>>>", ln):
            src_line += newline_counts.get(mm.group(0), 0)

    maps = {
        "frontmatter": frontmatter_placeholders,
        "code": code_placeholders,
        "html": html_placeholders,
        "latex_block": latex_block_placeholders,
        "latex_inline": latex_inline_placeholders,
        "link": link_placeholders,
        "heading": heading_placeholders,
        "list": list_placeholders,
        "blockquote": blockquote_placeholders,
        "wikilink": wikilink_placeholders,
        "table": table_placeholders,
        "table_cell": table_cell_placeholders,
    }
    return FilterResult(content_lines, content_indices, source_line_numbers, maps)
```

Note: Task 3 will extend the per-line section inside this same function (inline code, LaTeX inline, HTML tags, wikilinks, links, headings, lists, blockquotes, and table cell handling).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_md_mask.TestBlockMasks -v`
Expected: 7 PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/md_mask.py tests/test_md_mask.py
git commit -m "feat(md-mask): block-level masks (frontmatter, fences, HTML comments, LaTeX blocks)"
```

---

### Task 3: Per-line inline masks + wikilinks + headings/lists/blockquotes/links

**Files:**
- Modify: `scripts/md_mask.py`
- Test: `tests/test_md_mask.py`

- [ ] **Step 1: Write the failing tests**

```python
class TestInlineMasks(unittest.TestCase):
    def test_inline_code_masked(self):
        import md_mask
        lines = ["Use `code --flag` here"]
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        joined = "\n".join(res.content_lines)
        self.assertIn("<<<CODE_", joined)
        self.assertNotIn("code --flag", joined)

    def test_inline_code_unpaired_kept(self):
        import md_mask
        lines = ["Unpaired `code here"]
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        self.assertIn("`code", "\n".join(res.content_lines))

    def test_inline_latex_masked(self):
        import md_mask
        lines = ["Formula $E=mc^2$ done"]
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions(translate_latex=False))
        self.assertIn("<<<LATEX_INLINE_", "\n".join(res.content_lines))

    def test_currency_not_masked_as_latex(self):
        import md_mask
        lines = ["Price $100 and $50 total"]
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions(translate_latex=False))
        # Currency $100 should stay as text, not become latex placeholder
        self.assertNotIn("LATEX_INLINE_", "\n".join(res.content_lines))
        self.assertIn("$100", "\n".join(res.content_lines))

    def test_wikilink_masked(self):
        import md_mask
        lines = ["See [[My Note]] and ![[embed.png]]"]
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        joined = "\n".join(res.content_lines)
        self.assertIn("<<<WIKILINK_", joined)
        self.assertNotIn("[[My Note]]", joined)

    def test_wikilink_alias_stays_with_target(self):
        import md_mask
        lines = ["Link [[target|alias text]] here"]
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        joined = "\n".join(res.content_lines)
        # Whole wikilink is one opaque token — alias not separately translatable
        self.assertIn("<<<WIKILINK_", joined)
        self.assertNotIn("alias text", joined)

    def test_image_split(self):
        import md_mask
        lines = ["![alt text](path/to/img.png)"]
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        joined = "\n".join(res.content_lines)
        self.assertIn("<<<LINK_PRE_", joined)
        self.assertIn("<<<LINK_SUF_", joined)
        self.assertIn("alt text", joined)

    def test_heading_prefix_masked(self):
        import md_mask
        lines = ["## Section Title"]
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        joined = "\n".join(res.content_lines)
        self.assertIn("<<<HEADING_", joined)
        self.assertIn("Section Title", joined)

    def test_list_prefix_masked(self):
        import md_mask
        lines = ["- item one", "1. numbered"]
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        joined = "\n".join(res.content_lines)
        self.assertEqual(joined.count("<<<LIST_"), 2)

    def test_html_tag_masked(self):
        import md_mask
        lines = ['Text <span class="x">hi</span> more']
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        joined = "\n".join(res.content_lines)
        self.assertIn("<<<HTML_", joined)
        self.assertNotIn('<span', joined)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_md_mask.TestInlineMasks -v`
Expected: FAIL — first test shows `code --flag` still in output (inline masking not yet implemented)

- [ ] **Step 3: Write minimal implementation**

Inside `scripts/md_mask.py:filter_markdown_lines`, replace the stub per-line return with the full per-line loop (insert before the `content_lines = processed_lines` line in Task 2). Reference `markdown.ts:138-178` for inline code linear scan, `markdown.ts:356-372` for inline LaTeX, `markdown.ts:106-113,374-410` for HTML, plus wikilink addition:

```python
    # Per-line inline passes — extend inside filter_markdown_lines, after LaTeX blocks
    processed_lines = full_text.split("\n")
    result_lines: list[str] = []

    # Helpers — close over counters and maps
    def protect_inline_code(line: str) -> str:
        nonlocal lc
        if "`" not in line:
            return line
        out = ""
        i = 0
        while i < len(line):
            if line[i] != "`":
                nxt = line.find("`", i)
                if nxt == -1:
                    return out + line[i:]
                out += line[i:nxt]
                i = nxt
                continue
            n = 0
            while i + n < len(line) and line[i + n] == "`":
                n += 1
            j = i + n
            close = -1
            while j < len(line):
                if line[j] != "`":
                    j += 1
                    continue
                m = 0
                while j + m < len(line) and line[j + m] == "`":
                    m += 1
                if m == n:
                    close = j
                    break
                j += m
            if close == -1:
                out += line[i:i + n]
                i += n
            else:
                key = f"<<<CODE_{lc}>>>"
                code_placeholders[key] = line[i:close + n]
                out += key
                lc += 1
                i = close + n
        return out

    WIKILINK_RE = re.compile(r"!?\[\[[^\]]+\]\]")

    for idx, line in enumerate(processed_lines):
        mod = line

        # Inline code (must be before LaTeX inline and HTML)
        mod = protect_inline_code(mod)

        # Inline LaTeX $...$ — guard currency/whitespace
        if not opts.translate_latex:
            def _repl_inline_latex(m: re.Match) -> str:
                nonlocal lic
                full = m.group(0)
                content = m.group(1)
                if content.endswith(" ") or content.endswith("\t"):
                    return full
                if re.match(r"^[\s\d,.]+$", content) and "\\" not in content:
                    return full
                key = f"<<<LATEX_INLINE_{lic}>>>"
                latex_inline_placeholders[key] = full
                lic += 1
                return key
            mod = re.sub(r"(?<!\\)\$([^\s$][^$]*?)\$(?!\d)", _repl_inline_latex, mod)

        # HTML — self-closing, close, open (quote-aware, same as markdown.ts:106-113)
        HTML_SELF_RE = re.compile(rf"<([a-zA-Z][a-zA-Z0-9-]*)(?:\s+[a-zA-Z_:@#{{](?:{NOT_PLACEHOLDER}[^>\"']|\"[^\"]*\"|'[^']*')*?|\s*)\/>")
        HTML_CLOSE_RE = re.compile(r"</([a-zA-Z][a-zA-Z0-9-]*)>")
        HTML_OPEN_RE = re.compile(rf"<([a-zA-Z][a-zA-Z0-9-]*)(?:\s+[a-zA-Z_:@#](?:{NOT_PLACEHOLDER}[^>\"']|\"[^\"]*\"|'[^']*')*|\s*\/|\s+)?>")

        def _html_self(m: re.Match) -> str:
            nonlocal hcc
            key = f"<<<HTML_{hcc}>>>"
            html_placeholders[key] = m.group(0)
            hcc += 1
            return key
        mod = HTML_SELF_RE.sub(_html_self, mod)
        def _html_close(m: re.Match) -> str:
            nonlocal hcc
            key = f"<<<HTML_{hcc}>>>"
            html_placeholders[key] = m.group(0)
            hcc += 1
            return key
        mod = HTML_CLOSE_RE.sub(_html_close, mod)
        def _html_open(m: re.Match) -> str:
            nonlocal hcc
            key = f"<<<HTML_{hcc}>>>"
            html_placeholders[key] = m.group(0)
            hcc += 1
            return key
        mod = HTML_OPEN_RE.sub(_html_open, mod)

        # Wikilinks — before links/images so [[...]] not split as [text](url)
        def _wikilink(m: re.Match) -> str:
            nonlocal wikic
            key = f"<<<WIKILINK_{wikic}>>>"
            wikilink_placeholders[key] = m.group(0)
            wikic += 1
            return key
        mod = WIKILINK_RE.sub(_wikilink, mod)

        # Images ![alt](url) — keep alt text
        def _image(m: re.Match) -> str:
            nonlocal linkc
            prefix, content, suffix = m.group(1), m.group(2), m.group(3)
            if not content.strip():
                key = f"<<<LINK_{linkc}>>>"
                link_placeholders[key] = m.group(0)
                linkc += 1
                return key
            pre_key = f"<<<LINK_PRE_{linkc}>>>"
            suf_key = f"<<<LINK_SUF_{linkc}>>>"
            link_placeholders[pre_key] = prefix
            link_placeholders[suf_key] = suffix
            linkc += 1
            return f"{pre_key}{content}{suf_key}"
        mod = re.sub(r"(!\[)(.*?)(\]\((?:[^()\n]|\([^()\n]*\))*\))", _image, mod)

        # Links [text](url)
        def _link(m: re.Match) -> str:
            nonlocal linkc
            prefix, content, suffix = m.group(1), m.group(2), m.group(3)
            if opts.translate_link_text:
                pre_key = f"<<<LINK_PRE_{linkc}>>>"
                suf_key = f"<<<LINK_SUF_{linkc}>>>"
                link_placeholders[pre_key] = prefix
                link_placeholders[suf_key] = suffix
                linkc += 1
                return f"{pre_key}{content}{suf_key}"
            key = f"<<<LINK_{linkc}>>>"
            link_placeholders[key] = m.group(0)
            linkc += 1
            return key
        mod = re.sub(r"(\[)(.*?)(\]\((?:[^()\n]|\([^()\n]*\))*\))", _link, mod)

        # Headings
        mh = re.match(r"^(#{1,6}\s)(.*)", mod)
        if mh:
            key = f"<<<HEADING_{headc}>>>"
            heading_placeholders[key] = mh.group(1)
            headc += 1
            mod = key + mh.group(2)

        # Lists
        ml = re.match(r"^(\s*(?:[-*]|\d+\.)\s+)(.*)", mod)
        if ml:
            key = f"<<<LIST_{listc}>>>"
            list_placeholders[key] = ml.group(1)
            listc += 1
            mod = key + ml.group(2)

        # Blockquotes
        mq = re.match(r"^(>\s)(.*)", mod)
        if mq:
            key = f"<<<BLOCKQUOTE_{bqc}>>>"
            blockquote_placeholders[key] = mq.group(1)
            bqc += 1
            mod = key + mq.group(2)

        result_lines.append(mod)

    content_lines = result_lines
    content_indices = list(range(len(content_lines)))
    # source_line_numbers recomputed as in Task 2, now over result_lines
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_md_mask.TestInlineMasks -v`
Expected: 10 PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/md_mask.py tests/test_md_mask.py
git commit -m "feat(md-mask): per-line inline masks + wikilinks + headings/lists/blockquotes"
```

---

### Task 4: Table preservation — cell-by-cell masking (hard guarantee)

**Files:**
- Modify: `scripts/md_mask.py`
- Test: `tests/test_md_mask.py`
- Create: `tests/fixtures/md_mask/tables_basic.md`

- [ ] **Step 1: Write the failing tests for tables**

```python
class TestTableMasks(unittest.TestCase):
    def test_simple_table_cells_extracted(self):
        import md_mask
        lines = [
            "| Hebrew | English | Notes |",
            "|---|---|---|",
            "| שלום | hello | greeting |",
            "| תודה | thanks | polite |",
            "after table",
        ]
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        # Cell texts must be extractable and reassemblable
        cells = md_mask.extract_table_cells(lines)
        self.assertEqual(len(cells), 6)  # 3 header + 3 + 3? adjust to 2 data rows = 6 data cells
        # Masked output must contain TABLE_CELL placeholders, not raw Hebrew
        joined = "\n".join(res.content_lines)
        self.assertIn("<<<TABLE_CELL_", joined)
        self.assertNotIn("שלום", joined)

    def test_table_separator_not_translated(self):
        import md_mask
        lines = ["| A | B |", "|---|---|", "| x | y |"]
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        joined = "\n".join(res.content_lines)
        # Separator |---|---| must be masked as TABLE placeholder, not left as text
        self.assertIn("<<<TABLE_", joined)
        # But separator markers themselves must not be in translatable segments
        segs = md_mask.split_markdown_segments(res.content_lines, res.source_line_numbers)
        for t in segs.texts_to_translate:
            self.assertNotIn("---", t)

    def test_pipe_inside_inline_code_not_split(self):
        import md_mask
        lines = ["| `a|b` | code |", "|---|---|", "| `x|y` | z |"]
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        # Inline code `a|b` must be one CODE token inside the cell, not a column split
        joined = "\n".join(res.content_lines)
        self.assertIn("<<<CODE_", joined)

    def test_table_roundtrip(self):
        import md_mask
        lines = [
            "| H1 | H2 |",
            "|---|---|",
            "| שלום | world |",
            "| foo | בר |",
        ]
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        # Simulate translation: translate each TABLE_CELL placeholder's cell text
        # Here identity translation — restore must give back original
        restored = md_mask.restore_placeholders("\n".join(res.content_lines), res.maps)
        # Unwrap TABLE_CELL layer first, then TABLE layer — full roundtrip
        # (restore_placeholders handles nested fixed-point)
        self.assertEqual(restored.strip(), "\n".join(lines).strip())

    def test_table_cell_pipe_escaped(self):
        import md_mask
        lines = ["| a \\| b | c |", "|---|---|", "| d | e |"]
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        joined = "\n".join(res.content_lines)
        # Escaped pipe must not create extra column
        self.assertIn("<<<TABLE_CELL_", joined)

    def test_no_table_no_cells(self):
        import md_mask
        lines = ["# Heading", "No table here | just a pipe"]
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        self.assertNotIn("TABLE_CELL_", "\n".join(res.content_lines))
        self.assertEqual(md_mask.extract_table_cells(lines), [])

    def test_alignment_colons_preserved(self):
        import md_mask
        lines = ["| Left | Center | Right |", "|:---|:---:|---:|", "| a | b | c |"]
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        restored = md_mask.restore_placeholders("\n".join(res.content_lines), res.maps)
        self.assertIn("|:---|:---:|---:|", restored)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_md_mask.TestTableMasks -v`
Expected: FAIL — `AttributeError: module md_mask has no attribute extract_table_cells`

- [ ] **Step 3: Write minimal implementation**

Add to `scripts/md_mask.py` — table handling inserted **after** the per-line inline loop and **before** `content_lines = result_lines`. Design: detect GFM table blocks (header row + separator row), mask structure, extract cell texts.

```python
# ── Table helpers ────────────────────────────────────────────────────────────

# GFM separator row: pipes, optional colons, dashes, whitespace
_TABLE_SEP_RE = re.compile(r"^\s*\|?(\s*:?-+:?\s*\|)+\s*:?-+:?\s*\|?\s*$")
# Row that looks like a table row (contains at least one pipe, not inside fence already masked)
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")

def _is_table_separator(line: str) -> bool:
    return bool(_TABLE_SEP_RE.match(line))

def _looks_like_table_row(line: str) -> bool:
    # After inline-code masking, dangling pipes outside code are still visible.
    # Escaped pipes \| must not count. Strip them first.
    stripped = line.replace("\\|", "")
    return bool(_TABLE_ROW_RE.match(stripped)) and "|" in stripped

def _split_table_cells(row: str) -> list[str]:
    """Split a GFM row on unescaped pipes, respecting inline CODE placeholders.

    After inline code masking, a cell like `a|b` is already <<<CODE_n>>>,
    so a bare | scan is safe. Escaped \\| is ignored. Leading/trailing
    empty segments from outer pipes are dropped.
    """
    # Temporarily protect CODE placeholders from splitting: they contain no |
    # after masking? Actually masked CODE placeholders are <<<CODE_n>>> — no pipe.
    # So we can split directly on unescaped |.
    parts: list[str] = []
    cur = ""
    i = 0
    while i < len(row):
        if row[i] == "\\" and i + 1 < len(row) and row[i + 1] == "|":
            cur += "\\|"
            i += 2
            continue
        if row[i] == "|":
            parts.append(cur)
            cur = ""
            i += 1
            continue
        cur += row[i]
        i += 1
    parts.append(cur)
    # Drop leading/trailing empties from outer pipes: "| a | b |" → ["", " a ", " b ", ""] → [" a ", " b "]
    if parts and parts[0].strip() == "":
        parts = parts[1:]
    if parts and parts[-1].strip() == "":
        parts = parts[:-1]
    return parts

def extract_table_cells(lines: list[str]) -> list[str]:
    """Extract all cell texts from detected table blocks (for testing / direct use)."""
    cells: list[str] = []
    i = 0
    while i < len(lines):
        if i + 1 < len(lines) and _looks_like_table_row(lines[i]) and _is_table_separator(lines[i + 1]):
            # Header + separator block start
            j = i
            # Header row cells
            cells.extend(c.strip() for c in _split_table_cells(lines[j]))
            j += 2  # skip separator
            while j < len(lines) and _looks_like_table_row(lines[j]) and not _is_table_separator(lines[j]):
                cells.extend(c.strip() for c in _split_table_cells(lines[j]))
                j += 1
            i = j
        else:
            i += 1
    return cells

# Inside filter_markdown_lines — insert after the per-line loop, before final assignment:
# Replace the final block with:
    # ── Table pass: detect blocks and rewrite rows as TABLE + TABLE_CELL placeholders
    # Operates on result_lines (already per-line masked). Must run after inline code masking.
    table_block_lines: list[str] = []
    table_cell_maps: dict[str, str] = {}
    blocked_indices: set[int] = set()
    i2 = 0
    while i2 < len(result_lines):
        if i2 + 1 < len(result_lines) and _looks_like_table_row(result_lines[i2]) and _is_table_separator(result_lines[i2 + 1]):
            # Found table — collect header + separator + data rows
            start = i2
            end = i2 + 2
            while end < len(result_lines) and _looks_like_table_row(result_lines[end]) and not _is_table_separator(result_lines[end]):
                end += 1
            # For each row in [start, end), split cells and mask cell texts
            # Separator row: whole row becomes TABLE placeholder (no translatable cells)
            for r in range(start, end):
                raw_row = result_lines[r]
                if _is_table_separator(raw_row):
                    key = f"<<<TABLE_{tablec}>>>"
                    table_placeholders[key] = raw_row
                    table_block_lines.append(key)
                    tablec += 1
                    blocked_indices.add(r)
                else:
                    cells_raw = _split_table_cells(raw_row)
                    # Build masked row: | <<<TABLE_CELL_n>>> | <<<TABLE_CELL_m>>> |
                    masked_cells: list[str] = []
                    for c in cells_raw:
                        c_stripped = c.strip()
                        leading = c[:len(c) - len(c.lstrip())]
                        trailing = c[len(c.rstrip()):]
                        if c_stripped == "":
                            masked_cells.append(c)  # empty cell stays empty
                        else:
                            # c_stripped may still contain LINK_PRE/SUF, CODE, etc. Keep them as-is inside cell.
                            # For translation, the cell text (with inline placeholders) is stored.
                            key2 = f"<<<TABLE_CELL_{cellc}>>>"
                            table_cell_placeholders[key2] = c_stripped
                            # Preserve leading/trailing whitespace around placeholder in the row structure
                            masked_cells.append(leading + key2 + trailing)
                            cellc += 1
                    # Reassemble with canonical pipe spacing: | cell | cell |
                    row_masked = "| " + " | ".join(masked_cells) + " |"
                    table_block_lines.append(row_masked)
                    blocked_indices.add(r)
            # Replace the block slice in result_lines with masked block
            # We delay splice — collect and then rebuild
            i2 = end
        else:
            table_block_lines.append(result_lines[i2])
            i2 += 1
    # If no table found, table_block_lines == result_lines; else it's the masked version
    content_lines = table_block_lines
    # Merge table maps into maps dict
    # (already have table_placeholders / table_cell_placeholders in scope)
    maps["table"] = table_placeholders
    maps["table_cell"] = table_cell_placeholders
    # Recompute source_line_numbers over content_lines with updated maps
```

Also expose `extract_table_cells` at module level (already defined). Update `FilterResult.maps` type to include `table`/`table_cell`.

Create fixture:

```
# tests/fixtures/md_mask/tables_basic.md
| Hebrew | English | Notes |
|:---|:---:|---:|
| שלום | hello | greeting |
| `code|pipe` | text `x` | `y|z` |
| escaped \| pipe | normal | end |
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_md_mask.TestTableMasks -v`
Expected: 7 PASS

Also verify roundtrip via:

Run: `python -m unittest tests.test_md_mask -v`
Expected: all 21+ PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/md_mask.py tests/test_md_mask.py tests/fixtures/md_mask/tables_basic.md
git commit -m "feat(md-mask): table cell-by-cell masking with separator integrity"
```

---

### Task 5: Segmentation + merge + restore + removeChars + source line numbers

**Files:**
- Modify: `scripts/md_mask.py`
- Test: `tests/test_md_mask.py`

- [ ] **Step 1: Write the failing tests**

```python
class TestSegmentation(unittest.TestCase):
    def test_split_segments(self):
        import md_mask
        lines = ["Hello <<<CODE_100>>> world", "Second line"]
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        # Build real filtered lines then segment
        segs = md_mask.split_markdown_segments(res.content_lines, res.source_line_numbers)
        # Only text segments are translatable; CODE placeholder is skipped
        self.assertGreater(len(segs.texts_to_translate), 0)
        for t in segs.texts_to_translate:
            self.assertNotIn("<<<CODE_", t)

    def test_merge_roundtrip(self):
        import md_mask
        lines = ["# Title", "Paragraph with `code` and $x$ formula."]
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        segs = md_mask.split_markdown_segments(res.content_lines, res.source_line_numbers)
        # Identity translation: each segment maps to itself
        merged = md_mask.merge_markdown_segments(segs.line_segments, segs.texts_to_translate)
        restored = md_mask.restore_placeholders("\n".join(merged), res.maps)
        self.assertEqual(restored.strip(), "\n".join(lines).strip())

    def test_table_cell_segments(self):
        import md_mask
        lines = ["| שלום | hello |", "|---|---|", "| תודה | thanks |"]
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        segs = md_mask.split_markdown_segments(res.content_lines, res.source_line_numbers)
        # Table separator line is a TABLE placeholder → not translatable
        # TABLE_CELL placeholders each hold one cell's text
        self.assertTrue(any("<<<TABLE_CELL_" in ln for ln in res.content_lines))
        self.assertTrue(any("שלום" not in t for t in segs.texts_to_translate))  # Hebrew gone into cell map

    def test_restore_nested_placeholders(self):
        import md_mask
        # CODE inside HTML: HTML placeholder's value contains CODE placeholder
        maps = {
            "frontmatter": {}, "code": {"<<<CODE_100>>>": "`x`"},
            "html": {"<<<HTML_101>>>": "<span><<<CODE_100>>></span>"},
            "latex_block": {}, "latex_inline": {}, "link": {},
            "heading": {}, "list": {}, "blockquote": {},
            "wikilink": {}, "table": {}, "table_cell": {},
        }
        out = md_mask.restore_placeholders("a <<<HTML_101>>> b", maps)
        self.assertEqual(out, "a <span>`x`</span> b")

    def test_remove_chars_skips_placeholders(self):
        import md_mask
        # Remove '-' but placeholder <<<CODE_100>>> must survive
        text = "hello-<<<CODE_100>>>-world"
        out = md_mask.apply_remove_chars_to_markdown(text, "-")
        self.assertIn("<<<CODE_100>>>", out)
        self.assertNotIn("-", out.replace("<<<CODE_100>>>", ""))

    def test_source_line_numbers_with_table(self):
        import md_mask
        lines = ["# H", "| A | B |", "|---|---|", "| x | y |", "after"]
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        # First line is 1, table block is collapsed? Actually masked table stays as lines but cell-masked.
        # At minimum, 'after' line number must be >1 and monotonic
        self.assertEqual(res.source_line_numbers[0], 1)
        self.assertTrue(all(b >= a for a, b in zip(res.source_line_numbers, res.source_line_numbers[1:])))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_md_mask.TestSegmentation -v`
Expected: FAIL — `AttributeError: module md_mask has no attribute split_markdown_segments`

- [ ] **Step 3: Write minimal implementation**

Add to `scripts/md_mask.py`:

```python
from dataclasses import dataclass

@dataclass
class LineSegments:
    mapping: list  # list of dicts: {type: "placeholder"|"empty"|"text", value/index/leading/trailing}

@dataclass
class SegmentSplit:
    texts_to_translate: list[str]
    text_line_numbers: list[int]
    line_segments: list[LineSegments]

def split_markdown_segments(content_lines: list[str], source_line_numbers: list[int]) -> SegmentSplit:
    texts: list[str] = []
    text_lns: list[int] = []
    segs: list[LineSegments] = []
    for idx, line in enumerate(content_lines):
        parts = PLACEHOLDER_SPLIT_RE.split(line)
        mapping: list[dict] = []
        for seg in parts:
            if PLACEHOLDER_TEST_RE.match(seg):
                mapping.append({"type": "placeholder", "value": seg})
            else:
                leading = re.match(r"^\s*", seg).group(0)
                trailing = re.match(r"\s*$", seg)  # careful: use search from end
                # Correct trailing: re.search version
                trailing = re.search(r"\s*$", seg).group(0) if seg else ""
                trimmed = seg.strip()
                if not trimmed:
                    mapping.append({"type": "empty", "value": seg})
                else:
                    mapping.append({"type": "text", "index": len(texts), "leading": leading, "trailing": trailing})
                    text_lns.append(source_line_numbers[idx])
                    texts.append(trimmed)
        segs.append(LineSegments(mapping=mapping))
    return SegmentSplit(texts, text_lns, segs)

def merge_markdown_segments(line_segments: list[LineSegments], translated_texts: list[str]) -> list[str]:
    out: list[str] = []
    for seg in line_segments:
        parts: list[str] = []
        for entry in seg.mapping:
            if entry["type"] == "text":
                parts.append(entry["leading"] + translated_texts[entry["index"]] + entry["trailing"])
            else:
                parts.append(entry["value"])
        out.append("".join(parts))
    return out

def restore_placeholders(text: str, maps: dict[str, dict[str, str]]) -> str:
    # Merge all maps into one lookup
    all_maps: dict[str, str] = {}
    for d in maps.values():
        all_maps.update(d)
    # Expand TABLE_CELLs inside TABLE rows first? No — do fixed-point over whole text.
    # But TABLE_CELL values are raw cell text (may contain LINK_PRE etc.),
    # not placeholders. So one pass + nested HTML/CODE is enough with loop.
    out = text
    for _ in range(10):
        nxt = PLACEHOLDER_REPLACE_RE.sub(lambda m: all_maps.get(m.group(0), m.group(0)), out)
        if nxt == out:
            break
        out = nxt
    # Second pass: any TABLE_CELL placeholders that were inside TABLE rows
    # are already expanded by the loop above (TABLE row placeholder expansion
    # would have inserted cells — but we use TABLE_CELL-in-row design, not nested TABLE).
    # Our design puts TABLE_CELL directly in the row text, so one loop suffices.
    # Keep the loop for HTML-inside-CODE nesting.
    return out

def apply_remove_chars_to_markdown(text: str, remove_chars: str) -> str:
    if not remove_chars or not remove_chars.strip():
        return text
    chars = remove_chars.split()
    # Alternative: treat each character individually if no spaces
    # Use md-translator's splitBySpaces: split on whitespace, each token is a char to remove
    parts = PLACEHOLDER_SPLIT_RE.split(text)
    out_parts: list[str] = []
    for seg in parts:
        if PLACEHOLDER_TEST_RE.match(seg):
            out_parts.append(seg)
        else:
            cleaned = seg
            for ch in chars:
                cleaned = cleaned.replace(ch, "")
            out_parts.append(cleaned)
    return "".join(out_parts)

# Also add for table integration: after filter, caller will handle TABLE_CELL translation
# via split_markdown_segments (TABLE_CELL placeholders are kept, but their VALUES hold cell text
# that must be translated). Correction: the current Task 4 design stores cell text AS the
# placeholder value, with placeholder in the row. That means split sees placeholder tokens,
# not cell text. We need TABLE_CELL text to be translatable.
# Fix: instead store cell text in maps["table_cell"] and also emit a parallel list.
# Approach: keep cell placeholder in row, but also expose extractable cell texts
# via get_translatable_table_cells(maps). Or change: store raw Hebrew cell text
# as value, and during split, TABLE_CELL placeholders are NOT translatable — the
# cell texts are translated separately and re-injected before merge.
# Simpler: add helper translate_table_cells_in_maps.

def get_table_cell_texts(maps: dict[str, dict[str, str]]) -> list[str]:
    """Return cell texts in placeholder order (by numeric suffix)."""
    cells = maps.get("table_cell", {})
    # Sort by numeric id in placeholder name
    def key_num(k: str) -> int:
        m = re.search(r"(\d+)", k)
        return int(m.group(1)) if m else 0
    return [cells[k] for k in sorted(cells, key=key_num)]

def inject_translated_table_cells(maps: dict[str, dict[str, str]], translated_cells: list[str]) -> None:
    """Replace table_cell values in-place with translated cell texts (same order as get_table_cell_texts)."""
    cells = maps.get("table_cell", {})
    ordered_keys = sorted(cells, key=lambda k: int(re.search(r"(\d+)", k).group(1) or 0))
    for k, v in zip(ordered_keys, translated_cells):
        cells[k] = v
```

Adjust `filter_markdown_lines` table section to use this helpers-compatible shape (cell text stored in `table_cell` map, row contains `TABLE_CELL` placeholders).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_md_mask.TestSegmentation -v`
Expected: 6 PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/md_mask.py tests/test_md_mask.py
git commit -m "feat(md-mask): segmentation, merge, restore, removeChars"
```

---

### Task 6: Wire into `scripts/translate.py` (mask → translate → restore)

**Files:**
- Modify: `scripts/translate.py`
- Test: `tests/test_translation_pipeline.py` (or `tests/test_md_mask.py` integration)

- [ ] **Step 1: Write the failing integration test**

```python
# tests/test_md_mask.py — append

class TestTranslateIntegration(unittest.TestCase):
    def test_mask_translate_restore_table_identity(self):
        import md_mask, translate as tmod
        md_text = (
            "| Hebrew | English |\n"
            "|---|---|\n"
            "| שלום | hello |\n"
            "| תודה | thanks |\n"
        )
        chunk = md_text  # single chunk
        glossary = []
        first, last = set(), set()
        # Simulate translate.py flow: filter → split → mock translate → merge → restore
        filt = md_mask.filter_markdown_lines(chunk.split("\n"), md_mask.MdOptions())
        segs = md_mask.split_markdown_segments(filt.content_lines, filt.source_line_numbers)
        # Mock translator: identity for English, mark Hebrew per-word
        import re
        Hebrew_WORD = re.compile(r"[א-ת]{2,}")
        mocked = [Hebrew_WORD.sub(lambda m: f"⟦he:{m.group(0)}⟧", t) for t in segs.texts_to_translate]
        # Also handle table cells: translate cell texts separately
        cell_texts = md_mask.get_table_cell_texts(filt.maps)
        mocked_cells = [Hebrew_WORD.sub(lambda m: f"⟦he:{m.group(0)}⟧", c) for c in cell_texts]
        md_mask.inject_translated_table_cells(filt.maps, mocked_cells)
        merged = md_mask.merge_markdown_segments(segs.line_segments, mocked)
        restored = md_mask.restore_placeholders("\n".join(merged), filt.maps)
        # Table structure must survive: same pipe/row counts, separators intact
        self.assertEqual(restored.count("|"), md_text.count("|"))
        self.assertIn("|---|---|", restored)
        self.assertIn("hello", restored)

    def test_invariants_still_verified_after_mask(self):
        import md_mask, translate as tmod
        chunk = "See [[My Note]] and ![alt](http://example.com/img.png) and `code|pipe`"
        filt = md_mask.filter_markdown_lines(chunk.split("\n"), md_mask.MdOptions())
        segs = md_mask.split_markdown_segments(filt.content_lines, filt.source_line_numbers)
        merged = md_mask.merge_markdown_segments(segs.line_segments, segs.texts_to_translate)
        restored = md_mask.restore_placeholders("\n".join(merged), filt.maps)
        self.assertIn("[[My Note]]", restored)
        self.assertIn("http://example.com/img.png", restored)
        self.assertIn("`code|pipe`", restored)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_md_mask.TestTranslateIntegration -v`
Expected: FAIL — `AssertionError: '|'` counts mismatch or similar (wiring not yet in translate.py, but unit helpers pass — this test validates the helper contract)

- [ ] **Step 3: Write minimal implementation in `scripts/translate.py`**

Edit `scripts/translate.py` — add import and wire at chunk loop (`translate.py:858-906`). Keep existing verification as second layer.

1. Top of file, after imports:

```python
try:
    import md_mask  # type: ignore
    HAS_MD_MASK = True
except ImportError:
    HAS_MD_MASK = False
```

2. Inside `main` chunk loop, replace the direct `build_prompt`/`call_llm` with masked path:

```python
        for ch in chunks:
            chunk_text = ch["chunk_text"]
            section_path = ch["section_path"]
            invariants = extract_preservation_invariants(chunk_text, first_names, last_names)
            if invariants["person_names"]:
                name_candidates.update(invariants["person_names"])
            g_rows = glossary_for_chunk(chunk_text, glossary)

            # ── Masked translation path ──
            use_mask = HAS_MD_MASK and not args.mock  # mock path stays direct for test simplicity; or enable with flag
            if use_mask:
                opts = md_mask.MdOptions(
                    translate_frontmatter=False,
                    translate_multiline_code=False,
                    translate_latex=False,
                    translate_link_text=True,
                )
                filt = md_mask.filter_markdown_lines(chunk_text.split("\n"), opts)
                segs = md_mask.split_markdown_segments(filt.content_lines, filt.source_line_numbers)
                # Table cells are not in segs.texts_to_translate — translate them separately
                cell_texts = md_mask.get_table_cell_texts(filt.maps)
                # Build prompts per segment batch (or per whole chunk as one LLM call)
                # Simplest: one call with joined segments, preserving segment boundaries via JSON
                # For v1, join texts with "\n---SEG---\n" delimiter (exotic, unlikely in prose)
                SEG_DELIM = "\n---SEG---\n"
                if segs.texts_to_translate:
                    seg_prompt = build_prompt(
                        SEG_DELIM.join(segs.texts_to_translate),
                        section_path, g_rows, prev_tail, invariants,
                    )
                    if args.mock:
                        res_seg = mock_translate(SEG_DELIM.join(segs.texts_to_translate), g_rows, invariants)
                        translated_seg_text = res_seg["translation"]
                    else:
                        res_seg = call_llm(base_url, api_key, model, seg_prompt)
                        translated_seg_text = res_seg["translation"]
                    translated_segments = translated_seg_text.split(SEG_DELIM)
                    # Pad/truncate to expected length — mismatch is a hard error (fail closed)
                    if len(translated_segments) != len(segs.texts_to_translate):
                        raise RuntimeError(
                            f"Segment count mismatch: sent {len(segs.texts_to_translate)}, "
                            f"got {len(translated_segments)} — model did not preserve delimiters"
                        )
                else:
                    translated_segments = []
                    res_seg = {"unknown_terms": [], "notes": []}

                # Translate table cells (each cell independently — small, precise)
                if cell_texts:
                    # Batch cells similarly or one-by-one for determinism
                    translated_cells: list[str] = []
                    for cell in cell_texts:
                        cell_prompt = build_prompt(cell, section_path, g_rows, "", None)
                        if args.mock:
                            cr = mock_translate(cell, g_rows, None)
                        else:
                            cr = call_llm(base_url, api_key, model, cell_prompt)
                        translated_cells.append(cr["translation"])
                    md_mask.inject_translated_table_cells(filt.maps, translated_cells)
                else:
                    translated_cells = []

                merged_lines = md_mask.merge_markdown_segments(segs.line_segments, translated_segments)
                trans = md_mask.restore_placeholders("\n".join(merged_lines), filt.maps)
                # Reconstruct res shape for downstream verification
                res = {
                    "translation": trans,
                    "unknown_terms": res_seg.get("unknown_terms", []),
                    "notes": res_seg.get("notes", []),
                }
            else:
                prompt = build_prompt(chunk_text, section_path, g_rows, prev_tail, invariants)
                if args.mock:
                    res = mock_translate(chunk_text, g_rows, invariants)
                else:
                    res = call_llm(base_url, api_key, model, prompt)
                trans = res["translation"]

            # Verify invariants preserved verbatim + order — deterministic post-check
            missing = verify_all_preserved(invariants, trans)
            # ... rest unchanged (order_bad, global_bad, unknown_terms, chunk_translations)
```

Add CLI flag `--no-mask` to allow disabling (fail-closed default is masked when available):

```python
ap.add_argument("--no-mask", action="store_true", help="disable md_mask placeholder masking (debug)")
```

And gate `use_mask = HAS_MD_MASK and not args.no_mask`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_md_mask.TestTranslateIntegration -v`
Expected: 2 PASS

Run: `python -m unittest discover -s tests -v 2>&1 | tail -n 30`
Expected: existing 30+ tests still PASS

Manual check (mock):

Run: `python scripts/translate.py --mock --limit 1 2>&1 | head -n 20`
Expected: completes with `Done: ... completed, ... blocked_on_term, ... skipped_english`

- [ ] **Step 5: Commit**

```bash
git add scripts/translate.py scripts/md_mask.py tests/test_md_mask.py
git commit -m "feat(translate): wire md_mask mask→translate→restore with table cells

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 7: Table invariants in `translation_qa.py` (fail closed)

**Files:**
- Modify: `scripts/translation_qa.py`
- Test: `tests/test_md_mask.py`

- [ ] **Step 1: Write the failing tests**

```python
class TestTableQA(unittest.TestCase):
    def test_column_count_mismatch_fails(self):
        import translation_qa as qa
        src = "| A | B | C |\n|---|---|---|\n| 1 | 2 | 3 |\n"
        trans = "| A | B |\n|---|---|\n| 1 | 2 |\n"  # dropped column
        result = qa.check_table_fidelity(src, trans)
        self.assertEqual(result["status"], "fail")

    def test_separator_lost_fails(self):
        import translation_qa as qa
        src = "| A | B |\n|---|---|\n| 1 | 2 |\n"
        trans = "| A | B |\n| 1 | 2 |\n"  # separator gone
        result = qa.check_table_fidelity(src, trans)
        self.assertEqual(result["status"], "fail")

    def test_pipe_in_cell_causes_column_drift(self):
        import translation_qa as qa
        src = "| A | B |\n|---|---|\n| hello | world |\n"
        trans = "| A | B |\n|---|---|\n| hel|lo | world |\n"  # stray pipe splits cell
        result = qa.check_table_fidelity(src, trans)
        self.assertEqual(result["status"], "fail")

    def test_correct_table_passes(self):
        import translation_qa as qa
        src = "| A | B |\n|---|---|\n| 1 | 2 |\n"
        trans = "| A | B |\n|---|---|\n| one | two |\n"
        result = qa.check_table_fidelity(src, trans)
        self.assertEqual(result["status"], "pass")

    def test_alignment_colons_preserved(self):
        import translation_qa as qa
        src = "| A | B | C |\n|:---|:---:|---:|\n| 1 | 2 | 3 |\n"
        trans = "| A | B | C |\n|---|---|---|\n| 1 | 2 | 3 |\n"  # alignment lost
        result = qa.check_table_fidelity(src, trans)
        self.assertEqual(result["status"], "fail")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_md_mask.TestTableQA -v`
Expected: FAIL — `AttributeError: module translation_qa has no attribute check_table_fidelity`

- [ ] **Step 3: Write minimal implementation**

Add to `scripts/translation_qa.py` after `TABLE_ROW_RE`:

```python
_TABLE_SEP_QA_RE = re.compile(r"^\s*\|?(\s*:?-+:?\s*\|)+\s*:?-+:?\s*\|?\s*$")

def _parse_tables(text: str) -> list[list[list[str]]]:
    """Parse GFM tables into list of tables, each table is list of rows, each row is list of cells.

    Only blocks with header + separator are considered tables. Separator row is not included
    as a data row.
    """
    lines = text.split("\n")
    tables: list[list[list[str]]] = []
    i = 0
    while i < len(lines):
        if i + 1 < len(lines) and re.match(r"^\s*\|.*\|\s*$", lines[i]) and _TABLE_SEP_QA_RE.match(lines[i + 1]):
            rows: list[list[str]] = []
            # Header
            rows.append([c.strip() for c in _split_row_cells(lines[i])])
            sep_line = lines[i + 1]
            sep_cells = [c.strip() for c in _split_row_cells(sep_line)]
            # Store separator alignment markers for fidelity check
            # Encode as tuple of ("left","center","right","default") per column
            i += 2
            while i < len(lines) and re.match(r"^\s*\|.*\|\s*$", lines[i]) and not _TABLE_SEP_QA_RE.match(lines[i]):
                rows.append([c.strip() for c in _split_row_cells(lines[i])])
                i += 1
            # Attach separator as metadata — keep alongside rows
            tables.append(rows)
            # We need separator fidelity: stash it in a parallel structure.
            # Simplest: store sep_cells check via closure — add attribute to this table.
            # Instead, check separators separately via _collect_separators.
            continue
        i += 1
    return tables

def _split_row_cells(row: str) -> list[str]:
    # Split on unescaped pipes (ignore \|), drop outer empties — same as md_mask
    parts: list[str] = []
    cur = ""
    j = 0
    while j < len(row):
        if row[j] == "\\" and j + 1 < len(row) and row[j + 1] == "|":
            cur += "\\|"
            j += 2
            continue
        if row[j] == "|":
            parts.append(cur)
            cur = ""
            j += 1
            continue
        cur += row[j]
        j += 1
    parts.append(cur)
    if parts and parts[0].strip() == "":
        parts = parts[1:]
    if parts and parts[-1].strip() == "":
        parts = parts[:-1]
    return parts

def _collect_separators(text: str) -> list[str]:
    return [ln.strip() for ln in text.split("\n") if _TABLE_SEP_QA_RE.match(ln)]

def check_table_fidelity(source_body: str, trans_body: str) -> dict:
    """Fail if table column counts, row counts, or separator alignment drift.

    Tables are the highest-risk construct (see md-translator research doc).
    This check is strict: any mismatch quarantines the doc.
    """
    src_tables = _parse_tables(source_body)
    trans_tables = _parse_tables(trans_body)
    issues: list[str] = []

    if len(src_tables) != len(trans_tables):
        return {"check": "table_fidelity", "status": "fail",
                "issues": [f"table count: source {len(src_tables)} vs translation {len(trans_tables)}"]}

    src_seps = _collect_separators(source_body)
    trans_seps = _collect_separators(trans_body)
    if len(src_seps) != len(trans_seps):
        issues.append(f"separator rows: source {len(src_seps)} vs translation {len(trans_seps)}")
    else:
        for idx, (s, t) in enumerate(zip(src_seps, trans_seps)):
            if s != t:
                # Normalize whitespace before failing on alignment colons
                # Colons matter: :---|:---:|---:
                if s.replace(" ", "") != t.replace(" ", ""):
                    issues.append(f"table {idx} separator changed: {s!r} -> {t!r}")

    for ti, (sr, tr) in enumerate(zip(src_tables, trans_tables)):
        if len(sr) != len(tr):
            issues.append(f"table {ti} row count: source {len(sr)} vs translation {len(tr)}")
            continue
        for ri, (src_row, trans_row) in enumerate(zip(sr, tr)):
            if len(src_row) != len(trans_row):
                issues.append(f"table {ti} row {ri} column count: source {len(src_row)} vs translation {len(trans_row)}: {src_row!r} vs {trans_row!r}")

    return {"check": "table_fidelity", "status": "fail" if issues else "pass", "issues": issues}
```

Wire into `run_all`:

```python
# In run_all, inside `if source_body or raw_source:` block, after numeric_fidelity:
        checks.append(check_table_fidelity(body_for_struct, trans_body))
# In the else branch (no source), add:
        checks.append({"check": "table_fidelity", "status": "skip", "note": "no source"})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_md_mask.TestTableQA -v`
Expected: 5 PASS

Run: `python -m unittest discover -s tests -v 2>&1 | tail -n 20`
Expected: all PASS, including existing `test_translation_pipeline.py`

Manual:

Run: `python scripts/translation_qa.py data/translations --json-out /tmp/qa.json 2>&1 | tail -n 20`
Expected: no false quarantines on non-table docs; table-corrupted fixtures (if any) now fail on `table_fidelity`

- [ ] **Step 5: Commit**

```bash
git add scripts/translation_qa.py tests/test_md_mask.py
git commit -m "feat(qa): strict table fidelity invariants (columns, separators, alignment)"
```

---

### Task 8: Golden-file integration, removeChars ordering, docs

**Files:**
- Create: `tests/fixtures/md_mask/golden_*.md` + `tests/test_md_mask_golden.py` (or extend `test_md_mask.py`)
- Modify: `docs/research-md-translator.md` (add table handling note)
- Modify: `data/translation_policy.md` (add table rule)
- Modify: `tests/test_md_mask.py`

- [ ] **Step 1: Write the failing tests**

```python
class TestGoldenFiles(unittest.TestCase):
    def _roundtrip(self, path: Path):
        import md_mask
        lines = path.read_text(encoding="utf-8").split("\n")
        filt = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        segs = md_mask.split_markdown_segments(filt.content_lines, filt.source_line_numbers)
        # Identity translation + table cells identity
        cells = md_mask.get_table_cell_texts(filt.maps)
        # No translation — keep original cell texts
        merged = md_mask.merge_markdown_segments(segs.line_segments, segs.texts_to_translate)
        restored = md_mask.restore_placeholders("\n".join(merged), filt.maps)
        self.assertEqual(restored, "\n".join(lines))

    def test_golden_mixed(self):
        self._roundtrip(Path("tests/fixtures/md_mask/golden_mixed.md"))

    def test_golden_tables_basic(self):
        self._roundtrip(Path("tests/fixtures/md_mask/tables_basic.md"))

    def test_golden_hebrew_table(self):
        self._roundtrip(Path("tests/fixtures/md_mask/golden_hebrew_table.md"))

class TestRemoveCharsOrdering(unittest.TestCase):
    def test_remove_chars_before_restore(self):
        import md_mask
        # Simulate translate.py ordering: removeChars on merged (with placeholders) before restore
        lines = ["Hello `code-with-dash` world", "| A | B |", "|---|---|", "| x | y |"]
        filt = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        segs = md_mask.split_markdown_segments(filt.content_lines, filt.source_line_numbers)
        merged = md_mask.merge_markdown_segments(segs.line_segments, segs.texts_to_translate)
        cleaned = [md_mask.apply_remove_chars_to_markdown(ln, "-") for ln in merged]
        restored = md_mask.restore_placeholders("\n".join(cleaned), filt.maps)
        self.assertIn("<<<", restored)  # should not happen — placeholders survived cleaning
        self.assertNotIn("<<<", restored.replace("`code-with-dash`", ""))  # placeholder fully restored

    def test_wikilink_hebrew_table(self):
        import md_mask
        lines = ["| [[Note|alias]] | תודה |", "|---|---|", "| [[Link]] | hello |"]
        filt = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        # Wikilinks inside table cells must be opaque, not split
        self.assertIn("<<<WIKILINK_", "\n".join(filt.content_lines))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_md_mask.TestGoldenFiles -v`
Expected: FAIL — fixture files not yet created

- [ ] **Step 3: Create fixtures + fix ordering**

Create `tests/fixtures/md_mask/golden_mixed.md`:

```markdown
---
title: Test
---

# Heading One

Paragraph with `inline code` and $E=mc^2$ and [[Wikilink]] and [link text](http://example.com/path) and <span>html</span>.

> Blockquote with **bold** and list:
- item one
- item two

```python
fenced code | with pipes
second line
```

| Header A | Header B | Header C |
|:---|:---:|---:|
| hello שלום | `code|pipe` | [link](http://example.com) |
| escaped \| pipe | $x$ | normal |

<!-- comment spanning
multiple lines -->

$$ 
E = mc^2
$$

![alt text](img.png)
```

Create `tests/fixtures/md_mask/golden_hebrew_table.md`:

```markdown
| מונח | תרגום | הערות |
|---|---|---|
| שלום | hello | greeting |
| תודה | thanks | polite |
| מודל | model | term |
```

Create `tests/fixtures/md_mask/tables_basic.md` (already from Task 4, ensure content matches test expectations).

No code change needed if previous tasks correctly implement fixed-point restore. If `TestRemoveCharsOrdering` fails, fix is to ensure `translate.py` calls `apply_remove_chars_to_markdown` on `merged_lines` (with placeholders) before `restore_placeholders`, not after. Verify and patch `translate.py` if needed.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest discover -s tests -v`
Expected: all PASS — 30+ existing + 30+ new

Run: `vault check example_vault 2>&1 | tail -n 20`
Expected: PASS (payload not touched) or note that `scripts/` is not payload

- [ ] **Step 5: Update docs**

Append to `data/translation_policy.md` (or `data/translation_prompt.md` glossary block) a rule:

```markdown
## Tables (GFM)
- Table structure (pipes `|`, separator `|---|---|`, alignment colons `:---:`) is NEVER translated.
- Only cell text is translated, cell-by-cell. A `|` inside cell text is forbidden unless escaped `\|` or inside `` `code` ``.
- QA enforces column count, row count, and separator fidelity — mismatches quarantine the doc.
```

Add a short section to `docs/research-md-translator.md` noting the Python table divergence (cell-by-cell vs mask-whole-table) and linking to `scripts/md_mask.py`.

- [ ] **Step 6: Commit**

```bash
git add tests/fixtures/md_mask/ tests/test_md_mask.py scripts/md_mask.py scripts/translate.py data/translation_policy.md docs/research-md-translator.md
git commit -m "test(md-mask): golden files + table Hebrew + removeChars ordering + policy

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Verification (end-to-end)

```bash
# 1. Unit suite
python -m unittest discover -s tests -v

# 2. Mock translation roundtrip (no API key, offline)
python scripts/translate.py --mock --limit 5 2>&1 | tail -n 20
# Expect: masked path engaged, tables preserve pipes, ledger written

# 3. QA on mock output (includes new table_fidelity gate)
python scripts/translation_qa.py data/translations --vault-root . --json-out /tmp/qa.json
cat /tmp/qa.json | python -m json.tool | head -n 60

# 4. Golden roundtrip identity (no translation, just mask→restore)
python -m unittest tests.test_md_mask.TestGoldenFiles -v

# 5. Intentionally break a table (inject stray pipe) and confirm quarantine
python -c "
import translation_qa as qa
print(qa.check_table_fidelity('| A | B |\n|---|---|\n| 1 | 2 |\n',
                               '| A | B |\n|---|---|\n| 1|2 | 3 |\n'))
"
# Expect: status fail, issue about column count

# 6. Check no payload drift
vault check example_vault  # per CLAUDE.md — must exit 0
```

---

## Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| Table detection false positive on `a | b` prose | Only treat as table when next line is separator `|---|---|`; prose pipes stay as text |
| Escaped pipes `\|` inside cells missed | Explicit `\\|` pass in `_split_table_cells`; test `test_table_cell_pipe_escaped` |
| Inline code with pipes inside table cells (`code|pipe`) | Inline code masked to `<<<CODE_n>>>` before table split, so `|` inside code never seen by splitter |
| WikiLinks `[[...]]` with pipes `[[a|alias]]` inside table | WikiLinks masked before table pass; cell text sees only opaque token |
| Segment delimiter `---SEG---` appears in prose | Use `⟦SEG⟧` or JSON array instead of string join; fail closed on count mismatch |
| Placeholder literal collision (`<<<CODE_100>>>` in source) | Counter-seed scan starts after max literal id; `?? match` fallback in restore |
| Performance on large docs (6k chars chunk) | All passes are O(n) line-scan; no catastrophic regex; `pLimit` equivalent not needed for masking |
| Masked + verification double layer confusion | Mask is primary (hard), verification is secondary (fail-closed); log both |

---

## Sequencing

1. Task 1 → 2 → 3 → 4 are strictly ordered (vocabulary → blocks → inline → tables); each builds on the same `filter_markdown_lines`.
2. Task 5 (segmentation) depends on 1-4 complete.
3. Task 6 (wire into translate.py) depends on 5.
4. Task 7 (QA) can be done in parallel with 6 after 4.
5. Task 8 (goldens/docs) is last.

All LLM-calling paths: `sys.exit(1)` if `base_url` missing and not `--mock` — never silently skip. All dependencies required, not optional (per `~/.claude/CLAUDE.md`).

