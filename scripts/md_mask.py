"""Markdown placeholder masking — stdlib port of md-translator markdown.ts."""
from __future__ import annotations

import re

# Single source for all token families. TABLE/TABLE_CELL are additions for
# table cell-by-cell handling (md-translator has no table-cell tokens).
# WIKILINK covers Obsidian [[wikilinks]] / ![[embeds]] (not CommonMark).
PLACEHOLDER_PATTERN = (
    r"FRONTMATTER_\d+|TABLE_CELL_\d+|TABLE_\d+|MULTILINE_CODE_\d+|"
    r"LATEX_BLOCK_\d+|LATEX_INLINE_\d+|CODE_\d+|"
    r"LINK_PRE_\d+|LINK_SUF_\d+|LINK_\d+|"
    r"HEADING_\d+|LIST_\d+|BLOCKQUOTE_\d+|HTML_\d+|WIKILINK_\d+"
)

PLACEHOLDER_SPLIT_RE = re.compile(rf"(<<<(?:{PLACEHOLDER_PATTERN})>>>)")
PLACEHOLDER_TEST_RE = re.compile(rf"^<<<(?:{PLACEHOLDER_PATTERN})>>>$")
PLACEHOLDER_REPLACE_RE = re.compile(rf"<<<(?:{PLACEHOLDER_PATTERN})>>>")
NOT_PLACEHOLDER = rf"(?!<<<(?:{PLACEHOLDER_PATTERN})>>>)"

_COUNTER_SEED_RE = re.compile(r"<<<[A-Z_]+_(\d{1,9})>>>")

# ── Table helpers (also used by translation_qa) ──────────────────────────
_TABLE_SEP_RE = re.compile(r"^\s*\|?(\s*:?-+:?\s*\|)+\s*:?-+:?\s*\|?\s*$")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")


def _is_table_separator(line: str) -> bool:
    return bool(_TABLE_SEP_RE.match(line))


def _looks_like_table_row(line: str) -> bool:
    stripped = line.replace("\\|", "")
    return bool(_TABLE_ROW_RE.match(stripped)) and "|" in stripped


def _split_table_cells(row: str) -> list[str]:
    """Split a GFM row on unescaped pipes.

    After inline code masking, a cell like `a|b` is already <<<CODE_n>>>,
    so a bare | scan is safe. Escaped \\| is preserved.
    Leading/trailing empties from outer pipes are dropped.
    """
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
            j = i
            cells.extend(c.strip() for c in _split_table_cells(lines[j]))
            j += 2  # skip separator
            while j < len(lines) and _looks_like_table_row(lines[j]) and not _is_table_separator(lines[j]):
                cells.extend(c.strip() for c in _split_table_cells(lines[j]))
                j += 1
            i = j
        else:
            i += 1
    return cells


def compute_counter_seed(lines: list[str]) -> int:
    """Scan source for literal placeholder-like tokens and start after max."""
    seed = 100
    for m in _COUNTER_SEED_RE.finditer("\n".join(lines)):
        try:
            seed = max(seed, int(m.group(1)) + 1)
        except ValueError:
            pass
    return seed


from dataclasses import dataclass


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
    maps: dict[str, dict[str, str]]


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

    full_text = "\n".join(lines)

    # 1. Frontmatter — only if translate_frontmatter is False
    if not opts.translate_frontmatter:
        m = re.match(r"^---\n([\s\S]*?)\n---\n?", full_text)
        if m:
            body = m.group(1)
            first_nonempty = next((ln.strip() for ln in body.split("\n") if ln.strip()), "")
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
        out: list[str] = []
        emit_from = 0
        search_from = 0
        while True:
            open_idx = text.find("<!--", search_from)
            if open_idx == -1:
                break
            close_idx = text.find("-->", open_idx + 4)
            if close_idx == -1:
                break
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

    # ── Per-line inline passes ─────────────────────────────────────────

    processed_lines = full_text.split("\n")
    result_lines: list[str] = []

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
                m2 = 0
                while j + m2 < len(line) and line[j + m2] == "`":
                    m2 += 1
                if m2 == n:
                    close = j
                    break
                j += m2
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

        # HTML — self-closing, close, open (quote-aware)
        HTML_SELF_RE = re.compile(
            rf"<([a-zA-Z][a-zA-Z0-9-]*)(?:\s+[a-zA-Z_:@#{{](?:{NOT_PLACEHOLDER}[^>\"']|\"[^\"]*\"|'[^']*')*?|\s*)\/>"
        )
        HTML_CLOSE_RE = re.compile(r"</([a-zA-Z][a-zA-Z0-9-]*)>")
        HTML_OPEN_RE = re.compile(
            rf"<([a-zA-Z][a-zA-Z0-9-]*)(?:\s+[a-zA-Z_:@#](?:{NOT_PLACEHOLDER}[^>\"']|\"[^\"]*\"|'[^']*')*|\s*\/|\s+)?>"
        )

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

    # ── Table pass: detect blocks and rewrite rows as TABLE + TABLE_CELL placeholders
    table_block_lines: list[str] = []
    i2 = 0
    while i2 < len(result_lines):
        if i2 + 1 < len(result_lines) and _looks_like_table_row(result_lines[i2]) and _is_table_separator(result_lines[i2 + 1]):
            start = i2
            end = i2 + 2
            while end < len(result_lines) and _looks_like_table_row(result_lines[end]) and not _is_table_separator(result_lines[end]):
                end += 1
            for r in range(start, end):
                raw_row = result_lines[r]
                if _is_table_separator(raw_row):
                    key = f"<<<TABLE_{tablec}>>>"
                    table_placeholders[key] = raw_row
                    table_block_lines.append(key)
                    tablec += 1
                else:
                    cells_raw = _split_table_cells(raw_row)
                    masked_cells: list[str] = []
                    for c in cells_raw:
                        c_stripped = c.strip()
                        leading = c[:len(c) - len(c.lstrip())]
                        trailing = c[len(c.rstrip()):]
                        if c_stripped == "":
                            masked_cells.append(c)
                        else:
                            key2 = f"<<<TABLE_CELL_{cellc}>>>"
                            table_cell_placeholders[key2] = c_stripped
                            masked_cells.append(leading + key2 + trailing)
                            cellc += 1
                    row_masked = "|" + "|".join(masked_cells) + "|"
                    table_block_lines.append(row_masked)
            i2 = end
        else:
            table_block_lines.append(result_lines[i2])
            i2 += 1

    content_lines = table_block_lines
    content_indices = list(range(len(content_lines)))
    # source_line_numbers: collapsed placeholders contribute their newline count
    newline_counts: dict[str, int] = {}
    for d in [frontmatter_placeholders, code_placeholders, html_placeholders, latex_block_placeholders]:
        for k, v in d.items():
            newline_counts[k] = v.count("\n")
    source_line_numbers: list[int] = []
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


# ── Segmentation / merge / restore ─────────────────────────────────────


@dataclass
class LineSegments:
    mapping: list  # list of dicts: {type: "placeholder"|"empty"|"text", ...}


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
                leading = re.match(r"^\s*", seg).group(0) if seg else ""
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
    all_maps: dict[str, str] = {}
    for d in maps.values():
        all_maps.update(d)
    out = text
    for _ in range(10):
        nxt = PLACEHOLDER_REPLACE_RE.sub(lambda m: all_maps.get(m.group(0), m.group(0)), out)
        if nxt == out:
            break
        out = nxt
    return out


def apply_remove_chars_to_markdown(text: str, remove_chars: str) -> str:
    if not remove_chars or not remove_chars.strip():
        return text
    chars = remove_chars.split()
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


def get_table_cell_texts(maps: dict[str, dict[str, str]]) -> list[str]:
    """Return cell texts in placeholder order (by numeric suffix)."""
    cells = maps.get("table_cell", {})
    if not cells:
        return []

    def key_num(k: str) -> int:
        m = re.search(r"(\d+)", k)
        return int(m.group(1)) if m else 0

    return [cells[k] for k in sorted(cells, key=key_num)]


def inject_translated_table_cells(maps: dict[str, dict[str, str]], translated_cells: list[str]) -> None:
    """Replace table_cell values in-place with translated cell texts."""
    cells = maps.get("table_cell", {})
    if not cells:
        return
    ordered_keys = sorted(cells, key=lambda k: int(re.search(r"(\d+)", k).group(1) or 0))  # type: ignore
    for k, v in zip(ordered_keys, translated_cells):
        cells[k] = v
