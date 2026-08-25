"""Shared helpers for translation pipeline.

Deduped from 5 places (check_glossary.py, glossary_translate.py,
translation_qa.py, translation_reviewer.py, translate.py).

This module is the single source of truth for:
- GFM table cell splitting (escaped pipes)
- Frontmatter stripping (--- block)
- Glossary collision/version helpers

Glossary glossary.json schema: [{term_he, translations:[], keep_source, notes, status, example_doc}]
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

# KEEP sentinel retained only for keep_source fail-closed preservation.
# EN sentinels removed — glossary is now prompt-only plain English.
GLOSSARY_KEEP_RE = re.compile(r"⟦KEEP:[^⟧]+⟧")


def build_keep_sentinel(term_he: str) -> str:
    return f"⟦KEEP:{term_he}⟧"


def compute_glossary_version(glossary_path: Path) -> str:
    if not glossary_path.exists():
        return "no-glossary"
    h = hashlib.sha256(glossary_path.read_bytes()).hexdigest()[:12]
    return h


def _normalize_en_for_collision(en: str) -> str:
    """Lowercase + strip leading 'the ' for collision key (glossary stores bare State, not The State)."""
    return re.sub(r"^the\s+", "", en.strip().lower())


def _valid_translation_option(t: str) -> bool:
    """Filter invalid options before prompt injection (parenthetical truncation stubs).
    Only drops notes like '(likely truncated from ...)' / '(likely ...', not valid glosses
    like 'API (Application Programming Interface)' where '(' does not signal truncation.
    """
    if not t or not t.strip():
        return False
    low = t.lower()
    if "likely" in low or "truncated" in low or "incomplete" in low:
        return False
    # isolated parenthetical stub like '(...)' is a note, not a translation
    if t.strip().startswith("(") and t.strip().endswith(")"):
        return False
    return True


def _filter_translations(raw) -> list[str]:
    if isinstance(raw, str):
        raw = [raw] if raw.strip() else []
    if not raw:
        return []
    return [str(o).strip() for o in raw if _valid_translation_option(str(o))]


def check_glossary_collisions(rows: list[dict]) -> None:
    """M7: fail on any collision.

    - Within a row: translations must be unique (case-insensitive, The-normalized).
    - Across rows: no two term_he may share any allowed rendering; no duplicate term_he.
    """
    seen_en: dict[str, str] = {}
    seen_he: set[str] = set()
    for r in rows:
        he = (r.get("term_he") or "").strip()
        opts = _filter_translations(r.get("translations") or [])
        if opts and len(opts) != len({_normalize_en_for_collision(o) for o in opts}):
            raise RuntimeError(f"duplicate option inside {he!r}: {opts}")
        st = (r.get("status") or "approved").strip()
        if st not in ("approved", "keep_source") or not he:
            continue
        if st == "keep_source":
            if he in seen_he:
                raise RuntimeError(f"duplicate term_he {he!r}")
            seen_he.add(he)
            continue
        if not opts:
            continue
        if he in seen_he:
            raise RuntimeError(f"duplicate term_he {he!r}")
        seen_he.add(he)
        for en in opts:
            key = _normalize_en_for_collision(en)
            if key in seen_en and seen_en[key] != he:
                raise RuntimeError(f"glossary collision: english {en!r} maps to both {seen_en[key]!r} and {he!r}")
            seen_en[key] = he


def strip_csv_comments(text: str) -> list[str]:
    """Strip empty and # comment lines — for in-memory CSV text."""
    return [l for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]


def read_csv_lines_skip_comments(path: Path) -> list[str]:
    """Read file and strip # comment / empty lines before DictReader."""
    text = path.read_text(encoding="utf-8")
    return strip_csv_comments(text)


def strip_frontmatter(text: str) -> tuple[str, str]:
    """Split --- frontmatter block from body.

    Returns (frontmatter_text, body). frontmatter_text includes trailing ---.
    Matches translation_qa.py and translation_reviewer.py implementations.
    """
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            return text[: end + 5], text[end + 5 :]
    return "", text


def split_table_cells(row: str) -> list[str]:
    """Split a GFM row on unescaped pipes.

    Keeps \\ escaped pipes intact. Drops leading/trailing empties from
    outer pipes. Keep in sync — used by md_mask.py and translation_qa.py.
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


# Alias for translation_qa legacy name
split_row_cells = split_table_cells


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
    """Load person-name allowlists (fail-closed).

    Expects data/person_names/first_names.txt (593) and last_names_ranked.txt (818).
    Raises RuntimeError if files missing/empty — empty guard would silently translate names.
    """
    first_p = vault_root / "data" / "person_names" / "first_names.txt"
    last_p = vault_root / "data" / "person_names" / "last_names_ranked.txt"
    if not first_p.exists():
        raise RuntimeError(f"person name file missing: {first_p} — restore data/person_names/first_names.txt — fail-closed, refusing to run with empty guard")
    if not last_p.exists():
        raise RuntimeError(f"person name file missing: {last_p} — restore data/person_names/last_names_ranked.txt — fail-closed")
    first: set[str] = set()
    last: set[str] = set()
    for p, s in [(first_p, first), (last_p, last)]:
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                t = line.strip()
                if t:
                    s.add(t)
        except OSError as e:
            raise RuntimeError(f"cannot read {p}: {e}") from e
    if not first or not last:
        raise RuntimeError(f"person name files empty: {first_p} ({len(first)}), {last_p} ({len(last)}) — expected non-empty, fail-closed")
    codenames = load_codenames(vault_root)
    if exclude:
        codenames = codenames | exclude
    if codenames:
        first -= codenames
        last -= codenames
    return first, last

# --- Deprecated sentinel compat (for tests that still import old names) ---
GLOSSARY_SENTINEL_RE = re.compile(r"⟦EN:\d+(?::[^⟧]+)?⟧")
GLOSSARY_ANY_RE = re.compile(r"⟦(?:EN:\d+(?::[^⟧]+)?|KEEP:[^⟧]+)⟧")

def build_glossary_sentinel(idx: int, english: str = "") -> str:
    if english:
        return f"⟦EN:{idx}:{english}⟧"
    return f"⟦EN:{idx}⟧"

def parse_glossary_sentinel(s: str):
    m = re.match(r"⟦EN:(\d+)(?::([^⟧]+))?⟧", s)
    if not m:
        return None
    if m.group(2) is not None:
        return int(m.group(1)), m.group(2)
    return int(m.group(1)), ""
