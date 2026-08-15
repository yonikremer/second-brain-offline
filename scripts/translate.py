#!/usr/bin/env python3
"""Translate Hebrew markdown chunks to English with glossary + name guard.

- Structural chunking at heading/paragraph boundaries (never mid-sentence/table/code).
- English-only docs are skipped (no Hebrew → ledger skipped_english, no output file).
- Preservation by verification (not masking): person names, English spans, and
  URLs/file-paths are extracted from the source chunk, passed to the LLM as
  explicit verbatim-context, and verified to appear in the output.
- Filtered glossary: only terms occurring in chunk are injected.
- Person-name guard: exact-match against data/person_names/ (593 first + 818 last),
  extracted + verified (not masked).
- Structured output {translation, unknown_terms, notes} via response_format=json_object.
- Zero-guessing: unknown terms → ⟦he:<term>⟧ markers, blocked_on_term ledger.
- Content-addressed store data/translations/<sha>/translation.md + ledger.jsonl.
- Bounded retries (max 3).

Config: convert_config.json translation block:
  translation {base_url (\"\"), reviewer_base_url (\"\"), api_key_env (TRANSLATE_API_KEY),
               model (minimax-m2.7), reviewer_model (kimi-k2.7), chunk_chars (6000),
               review_sample (0.2), glossary_path (data/domain_terms/glossary.csv),
               fix_rounds (3)}
  Defaults: base_url \"\", reviewer_base_url \"\", chunk_chars 6000, review_sample 0.2,
            glossary_path data/domain_terms/glossary.csv, fix_rounds 3, model minimax-m2.7,
            reviewer_model kimi-k2.7, api_key_env TRANSLATE_API_KEY.
  Env precedence: TRANSLATE_BASE_URL primary, QMD_OPENAI_BASE_URL fallback;
  reviewer uses TRANSLATE_REVIEWER_BASE_URL override (see translation_reviewer.py).
  fix_rounds precedence: CLI --fix-rounds > TRANSLATE_FIX_ROUNDS env > config > 3 (0=disable).
Fail-fast if base_url missing. --mock for CI (mock is PERSON-sentinel aware: splits by
  ⟦PERSON_n⟧, only wraps remaining [א-ת]{2,} as ⟦he:…⟧ so sentinels are not marked).

CLI:
  python scripts/translate.py [vault_root] [--input DIR] [--glossary PATH] [--out DIR]
                              [--check] [--mock] [--force] [--resume] [--limit N] [--fix-rounds N]
  vault_root positional (default ".")
  --input DIR     corpus dir (default raw_md/raw auto-detect)
  --glossary PATH glossary.csv override (default translation.glossary_path or vault/data/domain_terms/glossary.csv)
  --out DIR       output store dir (default vault/data/translations, canonical ledger vault/data/translations/ledger.jsonl)
  --check         only check glossary gate, exit 1 if blocked
  --mock          offline mock (glossary substitution + sentinel-aware Hebrew marking)
  --force         retranslate even if cached (content-addressed <sha> already exists)
  --resume        same as default (resume by hash, kept for docs compat)
  --limit N       limit files (0=all)
  --fix-rounds N  max LLM fix rounds per doc after QA failures (default 3, 0=disable, env TRANSLATE_FIX_ROUNDS overrides config)
"""
from __future__ import annotations

import argparse
import csv
try:
    from translation_common import read_csv_lines_skip_comments as _shared_read_csv
    _HAS_SHARED = True
except ImportError:
    try:
        from scripts.translation_common import read_csv_lines_skip_comments as _shared_read_csv
        _HAS_SHARED = True
    except ImportError:
        _HAS_SHARED = False
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

_scripts_dir = os.path.dirname(os.path.abspath(__file__))
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

import md_mask  # type: ignore

PERSON_OPEN = "⟦PERSON_"
PERSON_CLOSE = "⟧"
EN_OPEN = "⟦EN_"
EN_CLOSE = "⟧"
HE_MARKER_FMT = "⟦he:{term}⟧"

# Hebrew char range (narrow א-ת) — word runs used for mock/qa sentinel-aware marking
HEBREW_WORD_RE = re.compile(r"[א-ת]{2,}")
# English preservation: Latin-script spans that must survive verbatim (verified, not masked)
_EN_SENTINEL_RE = re.compile(re.escape(EN_OPEN) + r"\d+" + re.escape(EN_CLOSE))
_PERSON_SENTINEL_RE = re.compile(re.escape(PERSON_OPEN) + r"\d+" + re.escape(PERSON_CLOSE))
# Contiguous Latin-script run — excludes '/' so file-paths are not merged into English spans
_EN_SPAN_RE = re.compile(r"[A-Za-z]{2,}(?:[ \t]*[A-Za-z0-9\-'\".,;:()&`]+)*")
# URLs and file-paths to preserve verbatim
_URL_RE = re.compile(r"https?://[^\s<>\[\]()\"']+|www\.[^\s<>\[\]()\"']+")
_FILEPATH_RE = re.compile(
    r"(?:[A-Za-z]:)?[\\/][\w.\-/\\]+|"  # /abs/path or C:\path or \path
    r"\b[\w.\-]+\.(?:md|py|json|csv|txt|pdf|docx|xlsx|png|jpg|jpeg|yaml|yml|toml|sh|js|ts)\b|"
    r"\b[\w.\-]+/[\w.\-/]*"
)
# YAML frontmatter and code sections — must be preserved verbatim and in order
_YAML_RE = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
_FENCED_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
_CODE_RE = re.compile(r"```.*?```|`[^`\n]+`", re.DOTALL)


def get_ledger_path(vault_root: Path, out_root: Path | None = None) -> Path:
    """Canonical ledger path: vault_root/data/translations/ledger.jsonl.

    If out_root is an explicit custom dir outside the vault, use out_root/ledger.jsonl
    so --out tests still write locally. Otherwise always canonical.
    """
    canonical = vault_root / "data" / "translations" / "ledger.jsonl"
    if out_root is None:
        return canonical
    try:
        # If out_root is inside vault_root, prefer canonical to avoid split ledgers
        out_root.resolve().relative_to(vault_root.resolve())
        return canonical
    except ValueError:
        return out_root / "ledger.jsonl"


def load_config(vault_root: Path) -> dict:
    p = vault_root / "convert_config.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def resolve_fix_rounds(cfg: dict, cli_value: int | None) -> int:
    """Resolve fix_rounds: CLI > env TRANSLATE_FIX_ROUNDS > config > default 3."""
    if cli_value is not None:
        try:
            v = int(cli_value)
            return max(0, v)
        except (TypeError, ValueError):
            pass
    env = os.environ.get("TRANSLATE_FIX_ROUNDS")
    if env is not None:
        try:
            return max(0, int(env.strip()))
        except (TypeError, ValueError):
            pass
    tcfg = cfg.get("translation", {}) if isinstance(cfg, dict) else {}
    raw = tcfg.get("fix_rounds", 3)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 3


def resolve_corpus_dir(vault_root: Path, explicit: Path | None = None) -> Path:
    if explicit:
        p = Path(explicit)
        if not p.is_dir():
            print(f"ERROR: input dir not found: {p}", file=sys.stderr)
            sys.exit(1)
        return p
    for name in ("raw_md", "raw"):
        p = vault_root / name
        if p.is_dir() and any(p.rglob("*.md")):
            return p
    print(f"ERROR: neither raw_md/ nor raw/ with *.md under {vault_root}", file=sys.stderr)
    sys.exit(1)


def _read_csv_skip_comments(path: Path) -> list[str]:
    """Read CSV text stripping # comment and empty lines (matches check_glossary) — via translation_common."""
    if _HAS_SHARED:
        return _shared_read_csv(path)
    text = path.read_text(encoding="utf-8")
    return [l for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]


def load_glossary(glossary_path: Path) -> list[dict]:
    if not glossary_path.exists():
        return []
    lines = _read_csv_skip_comments(glossary_path)
    if not lines:
        return []
    reader = csv.DictReader(lines)
    if not reader.fieldnames:
        return []
    return list(reader)


def load_codenames(vault_root: Path) -> set[str]:
    """Load org codenames that must NOT be masked as person names.

    Codenames are Hebrew terms like ברק/דניאל that collide with common given names
    but have English equivalents in the glossary and must be translated.
    File: data/person_names/codenames.txt — one term per line, # comments ignored.
    """
    p = vault_root / "data" / "person_names" / "codenames.txt"
    if not p.exists():
        return set()
    try:
        return {line.strip() for line in p.read_text(encoding="utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#")}
    except OSError:
        return set()


def load_person_names(vault_root: Path, exclude: set[str] | None = None) -> tuple[set[str], set[str]]:
    first_p = vault_root / "data" / "person_names" / "first_names.txt"
    last_p = vault_root / "data" / "person_names" / "last_names_ranked.txt"
    first: set[str] = set()
    last: set[str] = set()
    for p, s in [(first_p, first), (last_p, last)]:
        if p.exists():
            try:
                for line in p.read_text(encoding="utf-8").splitlines():
                    t = line.strip()
                    if t:
                        s.add(t)
            except OSError:
                pass
    # Codename exclusion: org codenames that are common Hebrew names must be translated,
    # not masked as PERSON. Static file + dynamic glossary terms are both excluded.
    codenames = load_codenames(vault_root)
    if exclude:
        codenames = codenames | exclude
    if codenames:
        first -= codenames
        last -= codenames
    return first, last


def mask_person_names(text: str, first: set[str], last: set[str]) -> tuple[str, list[str]]:
    """Exact-match scan for person names (unigram + bigram), mask to sentinels.

    Token-boundary safe: replaces whole Hebrew tokens only, never substrings
    inside a longer word (e.g. 'דן' inside 'דניאל').
    """
    tokens = re.findall(r"[א-ת]{2,}", text)
    token_set = set(tokens)

    words = re.findall(r"[א-ת]+", text)
    bigram_candidates: set[str] = set()
    for i in range(len(words) - 1):
        bg = f"{words[i]} {words[i+1]}"
        bigram_candidates.add(bg)

    single_names: set[str] = {t for t in token_set if t in first or t in last}
    bigram_names: set[str] = set()
    for bg in bigram_candidates:
        parts = bg.split()
        if len(parts) == 2 and parts[0] in first and parts[1] in last:
            bigram_names.add(bg)

    # If a bigram was matched, its component tokens must not also be listed as singles
    if bigram_names:
        bigram_tokens: set[str] = set()
        for bg in bigram_names:
            bigram_tokens.update(bg.split())
        single_names -= bigram_tokens

    all_names = sorted(single_names | bigram_names, key=len, reverse=True)
    if not all_names:
        return text, []

    name_to_sentinel: dict[str, str] = {}
    mapping: list[str] = []
    for name in all_names:
        sentinel = f"{PERSON_OPEN}{len(mapping)}{PERSON_CLOSE}"
        name_to_sentinel[name] = sentinel
        mapping.append(name)

    masked = _mask_via_tokens(text, name_to_sentinel)
    return masked, mapping


def _mask_via_tokens(text: str, name_to_sentinel: dict[str, str]) -> str:
    """Replace names at token boundaries by scanning Hebrew word spans."""
    bigram_set = {k for k in name_to_sentinel if " " in k}
    single_set = {k for k in name_to_sentinel if " " not in k}

    he_spans = [(m.start(), m.end(), m.group(0)) for m in re.finditer(r"[א-ת]+", text)]
    if not he_spans:
        return text

    skip: set[int] = set()
    replacements: dict[int, str] = {}

    # Bigram pass first (longer matches win)
    for i in range(len(he_spans) - 1):
        if i in skip:
            continue
        bg = f"{he_spans[i][2]} {he_spans[i + 1][2]}"
        if bg in bigram_set:
            sep = text[he_spans[i][1]: he_spans[i + 1][0]]
            if sep in ("", " ", "\t", "\n", "־", "-", "־"):
                replacements[i] = name_to_sentinel[bg]
                skip.add(i + 1)

    # Single pass for remaining tokens
    for i, (_, _, tok) in enumerate(he_spans):
        if i in skip or i in replacements:
            continue
        if tok in single_set:
            replacements[i] = name_to_sentinel[tok]

    if not replacements:
        return text

    # Rebuild via offset scan
    result: list[str] = []
    cur = 0
    i = 0
    while i < len(he_spans):
        s, e, _tok = he_spans[i]
        if i in replacements:
            result.append(text[cur:s])
            result.append(replacements[i])
            if (i + 1) in skip:
                cur = he_spans[i + 1][1]
                i += 2
            else:
                cur = e
                i += 1
        elif i in skip:
            i += 1
        else:
            i += 1
    result.append(text[cur:])
    return "".join(result)


def unmask_person_names(text: str, mapping: list[str]) -> str:
    for i, name in enumerate(mapping):
        text = text.replace(f"{PERSON_OPEN}{i}{PERSON_CLOSE}", name)
    return text


def is_english_only_doc(text: str, he_threshold: int = 10, ratio_threshold: float = 0.02) -> bool:
    """Return True for docs that are entirely (or effectively) English.

    Strips frontmatter + code fences before counting so English docs with
    YAML/code aren't misclassified. Heuristic: Hebrew char count < he_threshold
    AND Hebrew/(Hebrew+Latin) < ratio_threshold, with at least some Latin.
    This matches "entirely English" while tolerating stray Hebrew characters.
    """
    body = text
    if body.startswith("---\n"):
        end = body.find("\n---\n", 4)
        if end != -1:
            body = body[end + 5:]
    # Strip code fences — they may contain Hebrew-like chars in comments
    body = re.sub(r"```.*?```", "", body, flags=re.DOTALL)
    body = re.sub(r"`[^`]*`", "", body)
    he_chars = len(re.findall(r"[א-ת]", body))
    latin_chars = len(re.findall(r"[A-Za-z]", body))
    if latin_chars < 20:
        return False
    if he_chars == 0:
        return True
    if he_chars < he_threshold and (he_chars / max(he_chars + latin_chars, 1)) < ratio_threshold:
        return True
    return False


def mask_english_spans(text: str) -> tuple[str, list[str]]:
    """Legacy: mask contiguous Latin-script spans to EN sentinels.

    Kept for tests/back-compat. New flow uses extract_english_spans() +
    verify_preserved() — LLM sees raw English with preservation context
    instead of sentinels.
    """
    # Split by PERSON sentinels so we don't capture the word "PERSON"
    p_parts = _PERSON_SENTINEL_RE.split(text)
    p_sents = _PERSON_SENTINEL_RE.findall(text)
    en_mapping: list[str] = []
    out_parts: list[str] = []
    for idx, seg in enumerate(p_parts):
        # Within each non-PERSON segment, mask English spans
        def _repl(m: re.Match) -> str:
            span = m.group(0).strip()
            # Require at least 2 letters and at least one word with 2+ letters
            if len(re.findall(r"[A-Za-z]", span)) < 2:
                return m.group(0)
            # Skip very short fragments that are mostly punctuation
            if len(span) < 2:
                return m.group(0)
            sentinel = f"{EN_OPEN}{len(en_mapping)}{EN_CLOSE}"
            en_mapping.append(span)
            return sentinel
        # Only mask spans that look like real English (at least 2 consecutive letters)
        masked_seg = _EN_SPAN_RE.sub(_repl, seg)
        out_parts.append(masked_seg)
        if idx < len(p_sents):
            out_parts.append(p_sents[idx])
    return "".join(out_parts), en_mapping


def unmask_english_spans(text: str, mapping: list[str]) -> str:
    for i, span in enumerate(mapping):
        text = text.replace(f"{EN_OPEN}{i}{EN_CLOSE}", span)
    return text


# ── Preservation-by-verification (LLM sees raw text + invariants as context) ──

def extract_english_spans(text: str) -> list[str]:
    """Extract contiguous Latin-script spans that must be preserved verbatim.

    URLs/paths are excluded here (handled by extract_urls_and_paths).
    A newline sentinel breaks spans that had a URL in the middle.
    """
    # Mask URLs/paths first so they don't pollute English spans
    masked = _URL_RE.sub("\n", text)
    masked = _FILEPATH_RE.sub("\n", masked)
    spans: list[str] = []
    for m in _EN_SPAN_RE.finditer(masked):
        span = m.group(0).strip()
        if len(span) < 2 or len(re.findall(r"[A-Za-z]", span)) < 2:
            continue
        # Must contain at least one 2+ letter word (avoid isolated numbers/punct)
        if not re.search(r"[A-Za-z]{2,}", span):
            continue
        if span not in spans:
            spans.append(span)
    return spans


def extract_urls_and_paths(text: str) -> list[str]:
    """Extract URLs and file-paths that must be preserved verbatim."""
    urls: list[str] = []
    for m in _URL_RE.finditer(text):
        s = m.group(0).strip().rstrip(".,;:)]}'\"")
        if len(s) >= 8 and s not in urls:
            urls.append(s)
    # Remove URLs before path scan so we don't capture fragments like 's://'
    masked = _URL_RE.sub(" ", text)
    paths: list[str] = []
    for m in _FILEPATH_RE.finditer(masked):
        s = m.group(0).strip().rstrip(".,;:)]}'\"")
        if len(s) < 3:
            continue
        if "/" not in s and "\\" not in s and "." not in s:
            continue
        # Avoid tiny fragments and duplicates of URLs
        if s in urls or s in paths:
            continue
        paths.append(s)
    return urls + paths


def extract_person_names(text: str, first: set[str], last: set[str]) -> list[str]:
    """Extract person names from text using the same allowlist logic as masking."""
    _, mapping = mask_person_names(text, first, last)
    return mapping


def extract_yaml_frontmatter(text: str) -> list[str]:
    m = _YAML_RE.search(text)
    return [m.group(0)] if m else []


def extract_code_sections(text: str) -> list[str]:
    """Extract fenced + inline code sections in source order."""
    return [m.group(0) for m in _CODE_RE.finditer(text)]


def extract_preservation_invariants(text: str, first: set[str], last: set[str]) -> dict:
    """Collect all invariants that must survive translation verbatim.

    Returns dict with keys: person_names, english_spans, urls_and_paths,
    yaml_frontmatter, code_sections
    Each is a deduplicated list in order of appearance (except yaml which is 0/1).
    Code/YAML are extracted first and masked out before english/url extraction
    to avoid double-counting text inside code.
    """
    yaml_blocks = extract_yaml_frontmatter(text)
    code_blocks = extract_code_sections(text)
    # Mask yaml + code so english/url extraction ignores their interior
    masked = _YAML_RE.sub("\n", text)
    masked = _CODE_RE.sub("\n", masked)
    return {
        "yaml_frontmatter": yaml_blocks,
        "code_sections": code_blocks,
        "person_names": extract_person_names(masked, first, last),
        "english_spans": extract_english_spans(masked),
        "urls_and_paths": extract_urls_and_paths(masked),
    }


def verify_preserved(source_invariants: list[str], translation: str) -> list[str]:
    """Return subset of source_invariants missing verbatim in translation."""
    return [s for s in source_invariants if s not in translation]


def verify_all_preserved(invariants: dict, translation: str) -> dict:
    """Verify all categories; returns {category: [missing,...]} for failures."""
    missing: dict[str, list[str]] = {}
    for cat, items in invariants.items():
        bad = verify_preserved(items, translation)
        if bad:
            missing[cat] = bad
    return missing


def verify_ordered(source_items: list[str], translation: str) -> list[str]:
    """Return items that are out of order (present but monotonic violation)."""
    positions: list[int | None] = []
    for item in source_items:
        try:
            positions.append(translation.index(item))
        except ValueError:
            positions.append(None)
    present = [(i, p) for i, p in enumerate(positions) if p is not None]
    out: list[str] = []
    for k in range(1, len(present)):
        if present[k][1] < present[k - 1][1]:
            out.append(source_items[present[k][0]])
    return out


def verify_all_ordered(invariants: dict, translation: str) -> dict:
    """Check order per category; returns {category: [out_of_order,...]}."""
    bad: dict[str, list[str]] = {}
    for cat, items in invariants.items():
        if len(items) <= 1:
            continue
        oo = verify_ordered(items, translation)
        if oo:
            bad[cat] = oo
    return bad


def verify_global_order(source_text: str, invariants: dict, translation: str) -> list[str]:
    """Check that all preserved pieces appear in same relative order as in source."""
    all_occurrences: list[tuple[int, str]] = []
    for items in invariants.values():
        for val in items:
            idx = source_text.find(val)
            if idx != -1:
                all_occurrences.append((idx, val))
    all_occurrences.sort(key=lambda x: x[0])
    ordered_vals = [v for _, v in all_occurrences]
    if len(ordered_vals) <= 1:
        return []
    return verify_ordered(ordered_vals, translation)



def chunk_markdown(md_text: str, max_chars: int = 6000) -> list[dict]:
    """Split at heading boundaries, then paragraph boundaries if chunk exceeds budget.
    Never mid-code-block / mid-frontmatter / mid-table (table handled as paragraph).
    Returns [{section_path, chunk_text}].

    Note: kept heading→paragraph aligned with how qmd chunks markdown for
    embedding (qmd's chunking is internal: AST for code + heading-aware for
    markdown, not exposed as a library). If qmd exposes a chunk API later,
    wire it here — same boundaries keep translation chunks = retrieval chunks.
    """
    # Separate frontmatter
    body = md_text
    frontmatter = ""
    if md_text.startswith("---\n"):
        end = md_text.find("\n---\n", 4)
        if end != -1:
            frontmatter = md_text[: end + 5]
            body = md_text[end + 5:]

    # Track code fence state so we never split inside one
    lines = body.split("\n")
    sections: list[dict] = []
    cur_lines: list[str] = []
    cur_heading = ""
    in_fence = False

    def flush():
        nonlocal cur_lines, cur_heading
        if cur_lines:
            # Trim
            text = "\n".join(cur_lines).strip()
            if text:
                sections.append({"section_path": cur_heading, "chunk_text": text})
            cur_lines = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            cur_lines.append(line)
            continue
        if not in_fence and re.match(r"^#{1,6}\s+", line):
            # Heading boundary — flush previous section
            flush()
            cur_heading = line.strip()
            cur_lines.append(line)
        else:
            cur_lines.append(line)
            # Check if current buffer exceeds budget and we're at a paragraph boundary
            if not in_fence and sum(len(l) for l in cur_lines) > max_chars:
                # Look back for blank line (paragraph boundary)
                # Find last empty line in cur_lines
                last_blank = -1
                for i in range(len(cur_lines) - 1, -1, -1):
                    if cur_lines[i].strip() == "":
                        last_blank = i
                        break
                if last_blank > 0:
                    head = cur_lines[: last_blank + 1]
                    tail = cur_lines[last_blank + 1 :]
                    text = "\n".join(head).strip()
                    if text:
                        sections.append({"section_path": cur_heading, "chunk_text": text})
                    cur_lines = tail
                # else: no paragraph boundary found — keep accumulating (rare, very long paragraph)

    flush()
    # Re-attach frontmatter to first chunk
    if frontmatter and sections:
        sections[0]["chunk_text"] = frontmatter + "\n" + sections[0]["chunk_text"]
    elif frontmatter and not sections:
        sections.append({"section_path": "", "chunk_text": frontmatter})

    if not sections and body.strip():
        sections.append({"section_path": "", "chunk_text": body.strip()})
    return sections


def glossary_for_chunk(chunk_text: str, glossary: list[dict]) -> list[dict]:
    """Filter glossary to entries whose term_he occurs in chunk at word boundaries.

    Only terms with status 'approved' or 'keep_source' are injected — 'proposed'
    rows must not leak into prompts. Uses token-boundary matching to avoid
    injecting מודל when chunk contains only מודלים.
    """
    relevant = []
    he_tokens = set(re.findall(r"[א-ת]+", chunk_text))
    has_hebrew = re.compile(r"[א-ת]")
    for row in glossary:
        term = (row.get("term_he") or "").strip()
        if not term:
            continue
        status = (row.get("status") or "approved").strip()
        if status not in ("approved", "keep_source"):
            continue
        # Fast path: exact token match for single-token Hebrew terms
        if " " not in term and term in he_tokens:
            relevant.append(row)
            continue
        # Boundary-aware regex: require word boundaries so substring inside longer word doesn't match
        if has_hebrew.search(term):
            pat = r"(?<![א-ת])" + re.escape(term) + r"(?![א-ת])"
        else:
            pat = r"(?<![A-Za-z0-9_])" + re.escape(term) + r"(?![A-Za-z0-9_])"
        if re.search(pat, chunk_text):
            relevant.append(row)
    return relevant


def build_prompt(chunk_text: str, section_path: str, glossary_rows: list[dict],
                 prev_tail: str = "", invariants: dict | None = None) -> str:
    glossary_block = ""
    if glossary_rows:
        lines = []
        for r in glossary_rows:
            term = r.get("term_he", "")
            eng = r.get("english", "")
            ks = r.get("keep_source", "0")
            if ks == "1":
                lines.append(f"- {term} → KEEP AS-IS (do not translate)")
            elif eng:
                lines.append(f"- {term} → {eng}")
            else:
                lines.append(f"- {term} → (translate per context)")
        glossary_block = "Glossary (use these exact renderings):\n" + "\n".join(lines) + "\n\n"

    prev_block = ""
    if prev_tail:
        prev_block = f"Previous chunk tail (context only, do not re-emit):\n{prev_tail[:800]}\n\n"

    # Preservation context: invariants the LLM sees verbatim and must copy exactly
    preserve_block = ""
    if invariants:
        parts: list[str] = []
        for cat, label in [("yaml_frontmatter", "YAML frontmatter (keep exactly, first block)"),
                           ("code_sections", "Code sections (fenced/inline — keep exactly, in order)"),
                           ("person_names", "Person names (Hebrew — keep exactly, in order)"),
                           ("english_spans", "English spans (Latin — keep verbatim, in order)"),
                           ("urls_and_paths", "URLs/file-paths (keep verbatim, in order)")]:
            items = invariants.get(cat) or []
            if items:
                # Cap to keep prompt small; full list still verified after
                shown = items[:30]
                # For yaml/code, trim long blocks for prompt but verification uses full value
                def _short(s: str) -> str:
                    return s[:300] + ("…(truncated)" if len(s) > 300 else "")
                shown_short = [_short(s) for s in shown]
                parts.append(f"{label}: {json.dumps(shown_short, ensure_ascii=False)}")
                if len(items) > 30:
                    parts[-1] += f" (+{len(items)-30} more)"
        if parts:
            preserve_block = "Preserve verbatim IN ORDER — these strings from the source MUST appear exactly and in the same relative order in the output (code/YAML frontmatter included):\n" + "\n".join(f"- {p}" for p in parts) + "\n\n"

    return (
        f"Translate this Hebrew markdown chunk to faithful technical English.\n"
        f"Rules:\n"
        f"- Preserve headings, lists, tables, code fences exactly (same counts) and in the same order.\n"
        f"- Use glossary renderings exactly where they appear.\n"
        f"- Person names, English/URLs/code/YAML listed below must be copied verbatim and kept in the same relative order as in the source — do not translate, transliterate, reorder, or alter them.\n"
        f"- Never invent translations for unknown terms — list them in unknown_terms.\n"
        f"- Output JSON: {{\"translation\": string, \"unknown_terms\": [string], \"notes\": [string]}}\n\n"
        f"{glossary_block}"
        f"{preserve_block}"
        f"{prev_block}"
        f"Section: {section_path}\n\n"
        f"Chunk to translate:\n{chunk_text}\n"
    )


def format_qa_failures(checks: list[dict]) -> list[dict]:
    """Filter QA checks to failures only (status==fail)."""
    return [c for c in checks if c.get("status") == "fail"]


def build_fix_prompt(source_text: str, prev_translation: str, failures: list[dict],
                     glossary_rows: list[dict] | None = None,
                     invariants: dict | None = None) -> str:
    """Prompt for LLM to repair previous translation given QA failures."""
    src_cap = source_text[:12000]
    if len(source_text) > 12000:
        src_cap += f"\n…(truncated {len(source_text) - 12000} chars omitted — chunked fix should have been used)"
    prev_cap = prev_translation[:12000]
    if len(prev_translation) > 12000:
        prev_cap += f"\n…(truncated {len(prev_translation) - 12000} chars omitted — chunked fix should have been used)"
    _full_failure = json.dumps(failures, ensure_ascii=False, indent=2)
    failure_block = _full_failure[:6000]
    if len(_full_failure) > 6000:
        failure_block += "\n…(truncated)"
    glossary_block = ""
    if glossary_rows:
        lines = []
        for r in glossary_rows[:20]:
            term = r.get("term_he", "")
            eng = r.get("english", "")
            if term and eng:
                lines.append(f"- {term} → {eng}")
        if lines:
            glossary_block = "Glossary (must use exactly):\n" + "\n".join(lines)
            if glossary_rows and len(glossary_rows) > 20:
                glossary_block += f"\n(+{len(glossary_rows) - 20} more)"
            glossary_block += "\n\n"
    invariants_block = ""
    if invariants:
        parts = []
        for cat, items in invariants.items():
            if items:
                shown = items[:10]
                part = f"{cat}: {json.dumps(shown, ensure_ascii=False)}"
                if len(items) > 10:
                    part += f" (+{len(items) - 10} more)"
                parts.append(part)
        if parts:
            invariants_block = "Preserve verbatim in order:\n" + "\n".join(f"- {p}" for p in parts) + "\n\n"
    return (
        "You are repairing a Hebrew→English markdown translation that FAILED scripted QA checks.\n"
        "Fix ONLY the reported failures. Keep everything else identical.\n"
        "Rules:\n"
        "- Preserve headings, lists, tables, code fences exactly (same counts) and in order.\n"
        "- Use glossary renderings exactly where they appear.\n"
        "- Person names, English/URLs/code/YAML below must be copied verbatim and in order.\n"
        "- Never invent translations for unknown terms — use ⟦he:term⟧ and list in unknown_terms.\n"
        "- Output JSON: {\"translation\": string, \"unknown_terms\": [string], \"notes\": [string]}\n\n"
        f"{glossary_block}"
        f"{invariants_block}"
        f"QA failures to fix:\n{failure_block}\n\n"
        f"Original Hebrew source:\n{src_cap}\n\n"
        f"Previous translation (to repair):\n{prev_cap}\n"
    )


def _build_chunked_fix_prompts(source_text: str, prev_translation: str, failures: list[dict],
                               glossary_rows: list[dict] | None, invariants: dict | None,
                               chunk_chars: int,
                               first_names: set[str] | None = None, last_names: set[str] | None = None) -> list[str]:
    """Split large-doc fix into per-chunk prompts to avoid 12k truncation loss."""
    src_chunks = chunk_markdown(source_text, max_chars=chunk_chars)
    prev_chunks = chunk_markdown(prev_translation, max_chars=chunk_chars) if prev_translation.strip() else []
    n = max(len(src_chunks), len(prev_chunks), 1)
    fn = first_names if first_names is not None else set()
    ln = last_names if last_names is not None else set()
    prompts: list[str] = []
    for i in range(n):
        src = src_chunks[i]["chunk_text"] if i < len(src_chunks) else ""
        prev = prev_chunks[i]["chunk_text"] if i < len(prev_chunks) else ""
        section = src_chunks[i].get("section_path", "") if i < len(src_chunks) else f"chunk {i+1}/{n}"
        # Per-chunk filtering: only inject glossary terms that occur in this chunk
        if src and glossary_rows:
            cg = glossary_for_chunk(src, glossary_rows)
            chunk_glossary: list[dict] | None = cg  # [] means no terms in this chunk — keep empty
        elif src:
            chunk_glossary = None
        else:
            chunk_glossary = glossary_rows
        # Chunk-specific invariants with real person-name allowlist
        chunk_invariants = extract_preservation_invariants(src, fn, ln) if src else None
        if chunk_invariants is None:
            chunk_invariants = invariants
            if chunk_glossary is None:
                chunk_glossary = glossary_rows
        p = build_fix_prompt(src, prev, failures, chunk_glossary, chunk_invariants)
        # Annotate that failures are global — model should only fix those affecting its chunk
        global_note = "Note: QA failures above are global for the whole document — fix only those that affect your chunk's section, keep rest identical.\n\n"
        p = f"Chunk {i+1}/{n} — Section: {section}\n{global_note}" + p
        prompts.append(p)
    return prompts


def run_qa_for_doc(source_path: Path, trans_body: str, trans_meta: dict,
                   glossary: list[dict], vault_root: Path | None) -> list[dict]:
    """Run scripted QA battery; returns list of check dicts. Falls back gracefully."""
    try:
        import translation_qa as qa_mod
    except ImportError:
        return []
    try:
        return qa_mod.run_all(source_path, trans_body, trans_meta, glossary, vault_root=vault_root)
    except Exception as e:
        return [{"check": "qa_runner", "status": "fail", "error": str(e)[:500]}]


def _translate_chunks(raw_text: str, first_names: set[str], last_names: set[str],
                      glossary: list[dict], base_url: str, api_key: str, model: str,
                      mock: bool, chunk_chars: int, no_mask: bool,
                      name_candidates: set[str] | None) -> tuple[str, list[str], list[dict]]:
    """Translate raw_text chunk by chunk. Returns (full_translation, doc_unknown, chunk_notes)."""
    chunks = chunk_markdown(raw_text, max_chars=chunk_chars)
    chunk_translations: list[str] = []
    doc_unknown: list[str] = []
    all_notes: list[dict] = []
    prev_tail = ""
    for ch in chunks:
        chunk_text = ch["chunk_text"]
        section_path = ch["section_path"]
        invariants = extract_preservation_invariants(chunk_text, first_names, last_names)
        if name_candidates is not None and invariants["person_names"]:
            name_candidates.update(invariants["person_names"])
        g_rows = glossary_for_chunk(chunk_text, glossary)
        use_mask = not no_mask
        if use_mask:
            opts = md_mask.MdOptions(
                translate_frontmatter=False,
                translate_multiline_code=False,
                translate_latex=False,
                translate_link_text=True,
            )
            filt = md_mask.filter_markdown_lines(chunk_text.split("\n"), opts)
            segs = md_mask.split_markdown_segments(filt.content_lines, filt.source_line_numbers)
            cell_texts = md_mask.get_table_cell_texts(filt.maps)
            SEG_DELIM = "⟦SEG⟧"
            if segs.texts_to_translate:
                seg_prompt = build_prompt(
                    SEG_DELIM.join(segs.texts_to_translate),
                    section_path,
                    g_rows,
                    prev_tail,
                    invariants,
                )
                if mock:
                    res_seg = mock_translate(SEG_DELIM.join(segs.texts_to_translate), g_rows, invariants)
                    translated_seg_text = res_seg["translation"]
                else:
                    res_seg = call_llm(base_url, api_key, model, seg_prompt)
                    translated_seg_text = res_seg["translation"]
                translated_segments = translated_seg_text.split(SEG_DELIM)
                if len(translated_segments) != len(segs.texts_to_translate):
                    raise RuntimeError(
                        f"Segment count mismatch: sent {len(segs.texts_to_translate)}, "
                        f"got {len(translated_segments)} — model did not preserve delimiters"
                    )
            else:
                translated_segments = []
                res_seg = {"unknown_terms": [], "notes": []}
            if cell_texts:
                if mock:
                    cell_delim = "⟦CELL⟧"
                    joined_cells = cell_delim.join(cell_texts)
                    cr = mock_translate(joined_cells, g_rows, None)
                    translated_cells = cr["translation"].split(cell_delim)
                    if len(translated_cells) != len(cell_texts):
                        raise RuntimeError(
                            f"Cell count mismatch: sent {len(cell_texts)}, got {len(translated_cells)}"
                        )
                else:
                    cell_delim = "⟦CELL⟧"
                    joined_cells = cell_delim.join(cell_texts)
                    cell_prompt = build_prompt(joined_cells, section_path, g_rows, "", None)
                    cr = call_llm(base_url, api_key, model, cell_prompt)
                    translated_cells = cr["translation"].split(cell_delim)
                    if len(translated_cells) != len(cell_texts):
                        raise RuntimeError(
                            f"Cell count mismatch: sent {len(cell_texts)}, got {len(translated_cells)} — model did not preserve delimiters"
                        )
                md_mask.inject_translated_table_cells(filt.maps, translated_cells)
            merged_lines = md_mask.merge_markdown_segments(segs.line_segments, translated_segments)
            trans = md_mask.restore_placeholders("\n".join(merged_lines), filt.maps)
            res = {
                "translation": trans,
                "unknown_terms": res_seg.get("unknown_terms", []),
                "notes": res_seg.get("notes", []),
            }
        else:
            prompt = build_prompt(chunk_text, section_path, g_rows, prev_tail, invariants)
            if mock:
                res = mock_translate(chunk_text, g_rows, invariants)
            else:
                res = call_llm(base_url, api_key, model, prompt)
            trans = res["translation"]
        missing = verify_all_preserved(invariants, trans)
        if missing:
            for cat, items in missing.items():
                res.setdefault("notes", []).append(f"preserve_fail:{cat}:{items}")
            for items in missing.values():
                doc_unknown.extend(items)
        order_bad = verify_all_ordered(invariants, trans)
        global_bad = verify_global_order(chunk_text, invariants, trans)
        if order_bad:
            for cat, items in order_bad.items():
                res.setdefault("notes", []).append(f"order_fail:{cat}:{items}")
            for items in order_bad.values():
                doc_unknown.extend(items)
        if global_bad:
            res.setdefault("notes", []).append(f"global_order_fail:{global_bad}")
            doc_unknown.extend(global_bad)
        for ut in res.get("unknown_terms", []):
            ut = str(ut).strip()
            if ut and ut not in trans and ut in chunk_text:
                marker = HE_MARKER_FMT.format(term=ut)
                if marker not in trans:
                    trans = trans.rstrip() + f" {marker}"
            if ut:
                doc_unknown.append(ut)
        chunk_translations.append(trans)
        prev_tail = trans[-400:] if trans else ""
        if res.get("notes"):
            all_notes.append({"chunk": section_path, "notes": res["notes"]})
    full_translation = "\n\n".join(chunk_translations)
    return full_translation, doc_unknown, all_notes


def translate_one_doc(md_file: Path, vault_root: Path, out_root: Path,
                      glossary: list[dict], first_names: set[str], last_names: set[str],
                      base_url: str, api_key: str, model: str,
                      mock: bool, fix_rounds: int, chunk_chars: int,
                      no_mask: bool = False) -> dict:
    """Translate single file (no QA fix loop). Returns dict with translation,status etc."""
    _ = fix_rounds  # kept for caller compat; loop is in translate_one_doc_with_fix
    _ = out_root  # content-addressed store handled by caller (main)
    rel = md_file.relative_to(vault_root).as_posix() if md_file.is_relative_to(vault_root) else md_file.name
    raw_text = md_file.read_text(encoding="utf-8")
    if is_english_only_doc(raw_text):
        return {"skipped": True, "rel": rel, "source_hash": hashlib.sha256(raw_text.encode()).hexdigest(), "raw_text": raw_text}
    src_hash = hashlib.sha256(raw_text.encode()).hexdigest()
    name_candidates: set[str] = set()
    full_translation, doc_unknown, _notes = _translate_chunks(
        raw_text, first_names, last_names, glossary, base_url, api_key, model, mock, chunk_chars, no_mask, name_candidates)
    has_markers = "⟦he:" in full_translation
    status = "blocked_on_term" if (has_markers or doc_unknown) else "completed"
    return {
        "translation": full_translation,
        "status": status,
        "marker_count": full_translation.count("⟦he:"),
        "unknown_terms": sorted(set(doc_unknown)),
        "source_hash": src_hash,
        "rel": rel,
        "raw_text": raw_text,
        "name_candidates": name_candidates,
    }


def translate_one_doc_with_fix(md_file: Path, vault_root: Path, out_root: Path,
                               glossary: list[dict], first_names: set[str], last_names: set[str],
                               base_url: str, api_key: str, model: str,
                               mock: bool, fix_rounds: int, chunk_chars: int,
                               no_mask: bool = False) -> dict:
    """Full doc translate + QA + bounded LLM fix rounds."""
    result = translate_one_doc(md_file, vault_root, out_root, glossary, first_names, last_names,
                               base_url, api_key, model, mock, fix_rounds, chunk_chars, no_mask)
    if result.get("skipped"):
        return result
    source_path = md_file
    trans_body = result["translation"]
    raw_text = result["raw_text"]
    meta_stub = {"source_doc": result["rel"]}
    full_invariants = extract_preservation_invariants(raw_text, first_names, last_names)
    checks = run_qa_for_doc(source_path, trans_body, meta_stub, glossary, vault_root)
    failures = format_qa_failures(checks)
    fix_rounds_used = 0
    all_fix_attempts: list[dict] = []
    fix_unknown_terms: list[str] = []
    while failures and fix_rounds_used < fix_rounds:
        fix_rounds_used += 1
        if mock:
            fixed = mock_translate(raw_text, glossary_for_chunk(raw_text, glossary), full_invariants)
            new_body = fixed["translation"]
            fix_unknown_terms.extend(re.findall(r"⟦he:([^⟧]+)⟧", new_body))
        else:
            # For large docs, avoid silent 12k truncation by chunking the fix
            threshold = max(12000, chunk_chars * 2)
            is_large = len(raw_text) > threshold or len(trans_body) > threshold
            if is_large:
                prompts = _build_chunked_fix_prompts(raw_text, trans_body, failures,
                                                     glossary_for_chunk(raw_text, glossary),
                                                     full_invariants, chunk_chars,
                                                     first_names, last_names)
                # Log truncation avoidance
                print(f"  fix round {fix_rounds_used}: large doc ({len(raw_text)} src, {len(trans_body)} trans) — chunked into {len(prompts)} prompts", file=sys.stderr)
                chunk_translations: list[str] = []
                chunk_unknown: list[str] = []
                chunk_failed = False
                last_err = None
                for p_idx, p in enumerate(prompts):
                    try:
                        resp = call_llm(base_url, api_key, model, p)
                        ct = resp.get("translation", "")
                        chunk_translations.append(ct)
                        chunk_unknown.extend([str(x).strip() for x in resp.get("unknown_terms", []) if str(x).strip()])
                        chunk_unknown.extend(re.findall(r"⟦he:([^⟧]+)⟧", ct))
                    except Exception as e:
                        last_err = str(e)[:500]
                        chunk_failed = True
                        print(f"  fix chunk {p_idx+1}/{len(prompts)} failed: {last_err}", file=sys.stderr)
                        break
                if chunk_failed:
                    all_fix_attempts.append({"round": fix_rounds_used, "error": last_err or "chunk fix failed", "failures_before": failures, "chunked": True, "chunks": len(prompts), "src_len": len(raw_text), "trans_len": len(trans_body)})
                    break
                new_body = "\n\n".join(chunk_translations)
                fix_unknown_terms.extend(chunk_unknown)
                # Record that this round was chunked
                all_fix_attempts.append({"round": fix_rounds_used, "failures_before": failures, "chunked": True, "chunks": len(prompts), "src_len": len(raw_text), "trans_len": len(trans_body)})
                trans_body = new_body
                # Normalize ledger schema: also include chunked flag for non-chunked? handled below
                checks = run_qa_for_doc(source_path, trans_body, meta_stub, glossary, vault_root)
                failures = format_qa_failures(checks)
                continue
            # Normal whole-doc fix for small docs
            fix_prompt = build_fix_prompt(raw_text, trans_body, failures,
                                          glossary_rows=glossary_for_chunk(raw_text, glossary),
                                          invariants=full_invariants)
            try:
                resp = call_llm(base_url, api_key, model, fix_prompt)
                new_body = resp.get("translation", "")
                fix_unknown_terms.extend([str(x).strip() for x in resp.get("unknown_terms", []) if str(x).strip()])
                fix_unknown_terms.extend(re.findall(r"⟦he:([^⟧]+)⟧", new_body))
            except Exception as e:
                all_fix_attempts.append({"round": fix_rounds_used, "error": str(e)[:500], "failures_before": failures, "chunked": False, "src_len": len(raw_text), "trans_len": len(trans_body)})
                break
        trans_body = new_body
        all_fix_attempts.append({"round": fix_rounds_used, "failures_before": failures, "chunked": False, "src_len": len(raw_text), "trans_len": len(trans_body)})
        checks = run_qa_for_doc(source_path, trans_body, meta_stub, glossary, vault_root)
        failures = format_qa_failures(checks)
    if failures:
        final_status = "qa_failed"
    else:
        has_markers = "⟦he:" in trans_body
        final_status = "blocked_on_term" if has_markers else "completed"
    marker_terms = re.findall(r"⟦he:([^⟧]+)⟧", trans_body)
    recomputed_unknown = sorted(set(marker_terms + [str(x).strip() for x in fix_unknown_terms if str(x).strip()]))
    # When fix was attempted, merge original unknown_terms (preservation failures) with recomputed
    # so blocked docs aren't hidden if fix resolves QA but leaves invariant gaps
    if fix_rounds_used > 0:
        orig = result.get("unknown_terms", [])
        final_unknown = sorted(set(orig) | set(recomputed_unknown))
    else:
        final_unknown = result.get("unknown_terms", [])
    result.update({
        "translation": trans_body,
        "status": final_status,
        "marker_count": trans_body.count("⟦he:"),
        "unknown_terms": sorted(set(final_unknown)),
        "fix_rounds_used": fix_rounds_used,
        "fix_attempts": all_fix_attempts,
        "qa_checks": checks,
        "qa_failures": failures,
    })
    return result


def call_llm(base_url: str, api_key: str, model: str, prompt: str, retries: int = 3) -> dict:
    url = base_url.rstrip("/") + "/chat/completions"
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }).encode()

    last_err = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=payload, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        })
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = json.loads(resp.read().decode())
            content = data["choices"][0]["message"]["content"]
            obj = json.loads(content)
            return {
                "translation": str(obj.get("translation", "")).strip(),
                "unknown_terms": list(obj.get("unknown_terms", [])),
                "notes": list(obj.get("notes", [])),
            }
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:600]
            last_err = f"HTTP {e.code}: {body}"
            if e.code in (429, 500, 502, 503) and attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise RuntimeError(last_err) from e
        except Exception as e:
            last_err = str(e)
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise RuntimeError(f"LLM call failed after {retries} attempts: {last_err}") from e
    raise RuntimeError(last_err or "LLM exhausted retries")


def mock_translate(chunk_text: str, glossary_rows: list[dict], invariants: dict | None = None) -> dict:
    # Deterministic mock: apply glossary substitutions, wrap Hebrew remainder.
    # Raw English/URLs/names/code/YAML stay untouched — mirrors real LLM in preservation mode.
    out = chunk_text
    for r in glossary_rows:
        term = r.get("term_he", "")
        eng = r.get("english", "")
        if term and eng and term in out:
            out = out.replace(term, eng)
    # Protect invariants from Hebrew wrapping (person names, english, urls, code, yaml)
    if invariants:
        protected = []
        for cat in ("yaml_frontmatter", "code_sections", "person_names", "english_spans", "urls_and_paths"):
            for v in invariants.get(cat, []):
                if v and v not in protected:
                    protected.append(v)
        if protected:
            # Build alternation sorted longest first to avoid substring shadowing
            protected_sorted = sorted(protected, key=len, reverse=True)
            pat = re.compile("|".join(re.escape(p) for p in protected_sorted))
            parts = pat.split(out)
            sentinels = pat.findall(out)
            wrapped: list[str] = []
            for i, seg in enumerate(parts):
                wrapped.append(HEBREW_WORD_RE.sub(lambda m: HE_MARKER_FMT.format(term=m.group(0)), seg))
                if i < len(sentinels):
                    wrapped.append(sentinels[i])
            out = "".join(wrapped)
            return {"translation": out, "unknown_terms": [], "notes": ["mock"]}
    # Fallback: wrap all Hebrew
    out = HEBREW_WORD_RE.sub(lambda m: HE_MARKER_FMT.format(term=m.group(0)), out)
    return {"translation": out, "unknown_terms": [], "notes": ["mock"]}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Translate markdown chunks with glossary + name guard")
    ap.add_argument("vault_root", nargs="?", default=".", help="vault root")
    ap.add_argument("--input", dest="input_dir", default=None, help="corpus dir (default raw_md/raw)")
    ap.add_argument("--glossary", default=None, help="glossary.csv path")
    ap.add_argument("--out", dest="out_dir", default=None, help="output store dir")
    ap.add_argument("--check", action="store_true", help="only check glossary gate, exit 1 if blocked")
    ap.add_argument("--mock", action="store_true", help="offline mock (no LLM)")
    ap.add_argument("--force", action="store_true", help="retranslate even if cached")
    ap.add_argument("--resume", action="store_true", help="same as default (kept for docs compat)")
    ap.add_argument("--no-mask", action="store_true", help="disable md_mask placeholder masking (debug)")
    ap.add_argument("--limit", type=int, default=0, help="limit files (0=all)")
    ap.add_argument("--fix-rounds", type=int, default=None, help="max LLM fix rounds per doc after QA failures (default 3, 0=disable)")
    args = ap.parse_args(argv)

    vault_root = Path(args.vault_root).resolve()
    cfg = load_config(vault_root)
    tcfg = cfg.get("translation", {})
    fix_rounds = resolve_fix_rounds(cfg, args.fix_rounds)
    print(f"Fix rounds: {fix_rounds}")

    # Glossary gate — path from CLI > convert_config.json translation.glossary_path > default
    if args.glossary:
        glossary_path = Path(args.glossary)
    elif tcfg.get("glossary_path"):
        gp = Path(tcfg["glossary_path"])
        glossary_path = gp if gp.is_absolute() else vault_root / gp
    else:
        glossary_path = vault_root / "data" / "domain_terms" / "glossary.csv"
    # Fallback: glossary_proposed.csv if glossary.csv not yet created (pre-approval phase)
    if not glossary_path.exists() and glossary_path.name == "glossary.csv":
        alt = glossary_path.parent / "glossary_proposed.csv"
        if alt.exists():
            glossary_path = alt

    if args.check:
        # Use shared check
        try:
            from check_glossary import check_glossary
        except ImportError:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from check_glossary import check_glossary
        ok, errors = check_glossary(glossary_path)
        if ok:
            print(f"glossary OK: {glossary_path}")
            sys.exit(0)
        print(f"glossary BLOCKED: {glossary_path}", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)

    base_url = os.environ.get("TRANSLATE_BASE_URL") or os.environ.get("QMD_OPENAI_BASE_URL") or tcfg.get("base_url", "")
    api_key = os.environ.get("TRANSLATE_API_KEY") or os.environ.get("QMD_OPENAI_API_KEY") or ""
    model = tcfg.get("model") or os.environ.get("TRANSLATE_MODEL") or "minimax-m2.7"

    if not args.mock and not base_url:
        print("ERROR: translation base_url missing. Set TRANSLATE_BASE_URL or QMD_OPENAI_BASE_URL or convert_config.json translation.base_url", file=sys.stderr)
        sys.exit(1)

    glossary = load_glossary(glossary_path)
    if glossary_path.exists():
        print(f"Glossary: {len(glossary)} rows from {glossary_path}")
    else:
        print(f"Glossary: none ({glossary_path} not found) — translating without glossary")

    # Codename-aware person names: exclude org codenames (static file + glossary terms)
    # so that a codename like ברק/דניאל is translated via glossary, not masked as PERSON.
    glossary_terms = {r.get("term_he", "").strip() for r in glossary if (r.get("status") or "").strip() in ("approved", "keep_source") and r.get("term_he", "").strip()}
    # Load with glossary exclusion (primary); codenames.txt is optional manual override
    # Keep separate counts for audit (glossary_terms may be hundreds of domain terms,
    # only those overlapping the allowlist are true codename exclusions).
    raw_first, raw_last = load_person_names(vault_root)
    raw_all = raw_first | raw_last
    codenames_file = load_codenames(vault_root)
    glossary_overlap = glossary_terms & raw_all
    codenames_overlap = codenames_file & raw_all
    first_names, last_names = load_person_names(vault_root, exclude=glossary_terms)
    total_excluded = codenames_overlap | glossary_overlap
    if total_excluded:
        print(f"Codenames excluded from PERSON guard: {len(total_excluded)} (file:{len(codenames_overlap)} glossary:{len(glossary_overlap)} — {', '.join(sorted(list(total_excluded))[:5])}{' ...' if len(total_excluded) > 5 else ''})")
    print(f"Person names: {len(first_names)} first, {len(last_names)} last (raw {len(raw_first)}/{len(raw_last)}, excluded {len(total_excluded)})")

    corpus_dir = resolve_corpus_dir(vault_root, Path(args.input_dir) if args.input_dir else None)
    md_files = sorted(corpus_dir.rglob("*.md"))
    if args.limit:
        md_files = md_files[:args.limit]
    print(f"Translating {len(md_files)} files from {corpus_dir}")

    out_root = Path(args.out_dir) if args.out_dir else vault_root / "data" / "translations"
    out_root.mkdir(parents=True, exist_ok=True)

    # Ledger (canonical: vault_root/data/translations/ledger.jsonl)
    ledger_path = get_ledger_path(vault_root, out_root)
    try:
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"warn: cannot create ledger dir {ledger_path.parent}: {e}", file=sys.stderr)
    glossary_version = ""
    if glossary_path.exists():
        try:
            glossary_version = hashlib.sha256(glossary_path.read_bytes()).hexdigest()[:10]
        except OSError:
            pass

    name_candidates: set[str] = set()
    translated = 0
    blocked = 0
    skipped_english = 0
    failed_docs: list[str] = []
    qa_failed = 0

    try:
        chunk_chars = int(tcfg.get("chunk_chars", 6000))
    except (TypeError, ValueError):
        chunk_chars = 6000

    for md_file in md_files:
        rel = md_file.relative_to(vault_root).as_posix() if md_file.is_relative_to(vault_root) else md_file.name
        try:
            raw_text = md_file.read_text(encoding="utf-8")
        except OSError as e:
            print(f" skip {rel}: {e}", file=sys.stderr)
            continue

        # Cache check (content-addressed) — do before any LLM work
        src_hash_pre = hashlib.sha256(raw_text.encode()).hexdigest()
        store_dir_pre = out_root / src_hash_pre[:2] / src_hash_pre
        out_file_pre = store_dir_pre / "translation.md"
        if out_file_pre.exists() and not args.force:
            # Fail-closed: cached qa_failed must still count toward exit 1
            try:
                cached_text = out_file_pre.read_text(encoding="utf-8")
                is_qa_failed = False
                # Robust frontmatter parse (not substring) — extract JSON between --- markers
                if cached_text.startswith("---\n"):
                    end = cached_text.find("\n---\n", 4)
                    if end != -1:
                        try:
                            fm = json.loads(cached_text[4:end].strip())
                            is_qa_failed = fm.get("status") == "qa_failed"
                        except (json.JSONDecodeError, ValueError):
                            is_qa_failed = bool(re.search(r'"status"\s*:\s*"qa_failed"', cached_text))
                    else:
                        is_qa_failed = bool(re.search(r'"status"\s*:\s*"qa_failed"', cached_text))
                else:
                    is_qa_failed = bool(re.search(r'"status"\s*:\s*"qa_failed"', cached_text))
                if is_qa_failed:
                    failed_docs.append(rel)
                    qa_failed += 1
                    print(f"  {rel}: qa_failed (cached)", file=sys.stderr)
            except OSError as e:
                print(f"warn: cannot read cached {out_file_pre}: {e}", file=sys.stderr)
            continue

        # Translate with QA fix loop (handles english-only internally)
        try:
            result = translate_one_doc_with_fix(
                md_file, vault_root, out_root, glossary, first_names, last_names,
                base_url, api_key, model, args.mock, fix_rounds, chunk_chars,
                no_mask=args.no_mask)
        except RuntimeError as e:
            # Hard failure like segment mismatch
            print(f"  {rel}: error {e}", file=sys.stderr)
            failed_docs.append(rel)
            event = {
                "event": "translation_error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "source_doc": rel,
                "source_hash": src_hash_pre,
                "model": model,
                "glossary_version": glossary_version,
                "status": "error",
                "error": str(e)[:500],
            }
            with open(ledger_path, "a", encoding="utf-8") as lf:
                lf.write(json.dumps(event, ensure_ascii=False) + "\n")
            continue

        if result.get("skipped"):
            src_hash = result["source_hash"]
            event = {
                "event": "skipped_english",
                "ts": datetime.now(timezone.utc).isoformat(),
                "source_doc": rel,
                "source_hash": src_hash,
                "model": model,
                "glossary_version": glossary_version,
                "status": "skipped_english",
            }
            with open(ledger_path, "a", encoding="utf-8") as lf:
                lf.write(json.dumps(event, ensure_ascii=False) + "\n")
            skipped_english += 1
            print(f"  {rel}: skipped_english (no Hebrew)")
            continue

        # Aggregate name candidates
        if result.get("name_candidates"):
            name_candidates.update(result["name_candidates"])

        full_translation = result["translation"]
        status = result["status"]
        src_hash = result["source_hash"]
        fix_used = result.get("fix_rounds_used", 0)
        qa_failures = result.get("qa_failures", [])
        # qa_checks retained for debugging but not written to frontmatter (failures are)
        _qa_checks = result.get("qa_checks", [])

        # Write content-addressed store
        store_dir = out_root / src_hash[:2] / src_hash
        store_dir.mkdir(parents=True, exist_ok=True)
        out_file = store_dir / "translation.md"
        frontmatter = {
            "source_doc": rel,
            "source_hash": src_hash,
            "model": model,
            "glossary_version": glossary_version,
            "status": status,
            "marker_count": full_translation.count("⟦he:"),
            "unknown_terms": sorted(set(result.get("unknown_terms", []))),
            "fix_rounds_used": fix_used,
        }
        if qa_failures:
            frontmatter["qa_failures"] = qa_failures[:5]
        fm_text = "---\n" + json.dumps(frontmatter, ensure_ascii=False, indent=2) + "\n---\n\n"
        out_file.write_text(fm_text + full_translation, encoding="utf-8")

        # Ledger: fix attempts
        for attempt in result.get("fix_attempts", []):
            evt = {
                "event": "fix_attempt",
                "ts": datetime.now(timezone.utc).isoformat(),
                "source_doc": rel,
                "source_hash": src_hash,
                "model": model,
                "glossary_version": glossary_version,
                "round": attempt.get("round"),
                "failures_before": attempt.get("failures_before", [])[:3],
            }
            if "error" in attempt:
                evt["error"] = attempt["error"]
            for k in ("chunked", "src_len", "trans_len", "chunks"):
                if k in attempt:
                    evt[k] = attempt[k]
            # Ensure chunked is always present for schema consistency
            if "chunked" not in evt:
                evt["chunked"] = False
            with open(ledger_path, "a", encoding="utf-8") as lf:
                lf.write(json.dumps(evt, ensure_ascii=False) + "\n")

        # Ledger: qa_result
        qa_event = {
            "event": "qa_result",
            "ts": datetime.now(timezone.utc).isoformat(),
            "source_doc": rel,
            "source_hash": src_hash,
            "model": model,
            "glossary_version": glossary_version,
            "status": status,
            "fix_rounds_used": fix_used,
            "qa_failures": qa_failures[:5] if qa_failures else [],
        }
        with open(ledger_path, "a", encoding="utf-8") as lf:
            lf.write(json.dumps(qa_event, ensure_ascii=False) + "\n")

        # Ledger: translation event
        if status == "qa_failed":
            event = {
                "event": "qa_failed",
                "ts": datetime.now(timezone.utc).isoformat(),
                "source_doc": rel,
                "source_hash": src_hash,
                "model": model,
                "glossary_version": glossary_version,
                "status": status,
                "marker_count": frontmatter["marker_count"],
                "unknown_terms": frontmatter["unknown_terms"],
                "fix_rounds_used": fix_used,
                "qa_failures": qa_failures[:5],
            }
            failed_docs.append(rel)
            qa_failed += 1
        elif status == "blocked_on_term":
            event = {
                "event": "blocked_on_term",
                "ts": datetime.now(timezone.utc).isoformat(),
                "source_doc": rel,
                "source_hash": src_hash,
                "model": model,
                "glossary_version": glossary_version,
                "status": status,
                "marker_count": frontmatter["marker_count"],
                "unknown_terms": frontmatter["unknown_terms"],
                "fix_rounds_used": fix_used,
            }
            blocked += 1
        else:
            event = {
                "event": "translation_completed",
                "ts": datetime.now(timezone.utc).isoformat(),
                "source_doc": rel,
                "source_hash": src_hash,
                "model": model,
                "glossary_version": glossary_version,
                "status": status,
                "marker_count": frontmatter["marker_count"],
                "unknown_terms": frontmatter["unknown_terms"],
                "fix_rounds_used": fix_used,
            }
            translated += 1
        with open(ledger_path, "a", encoding="utf-8") as lf:
            lf.write(json.dumps(event, ensure_ascii=False) + "\n")

        # Console
        if status == "qa_failed":
            print(f"  {rel}: qa_failed after {fix_used} fix rounds: {qa_failures[:1]}", file=sys.stderr)
        else:
            chunk_info = f" ({fix_used} fix rounds)" if fix_used else ""
            print(f"  {rel}: {status}{chunk_info} ({frontmatter['marker_count']} markers)")

    # Log name candidates
    if name_candidates:
        cand_path = out_root / "name_candidates.txt"
        with open(cand_path, "w", encoding="utf-8") as f:
            for n in sorted(name_candidates):
                f.write(n + "\n")
        print(f"Name candidates: {len(name_candidates)} unique -> {cand_path}")

    print(f"Done: {translated} completed, {blocked} blocked_on_term, {skipped_english} skipped_english, {qa_failed} qa_failed -> {out_root}")
    if failed_docs:
        print(f"FAILED: {len(failed_docs)} docs still invalid after {fix_rounds} fix rounds: {failed_docs[:5]}", file=sys.stderr)
        print(f"Stop — fix budget exhausted. Inspect QA output and ledger, fix policy/glossary/prompt, retry with --fix-rounds N or --force.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
