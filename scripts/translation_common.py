"""Shared helpers for translation pipeline.

Deduped from 5 places (check_glossary.py, glossary_translate.py,
translation_qa.py, translation_reviewer.py, translate.py).

This module is the single source of truth for:
- CSV comment stripping (strip # / empty lines before DictReader)
- Frontmatter stripping (--- block)
- GFM table cell splitting (escaped pipes)

Import from here instead of copy-pasting.
"""
from __future__ import annotations

from pathlib import Path


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
