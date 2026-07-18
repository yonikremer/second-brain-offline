#!/usr/bin/env python3
"""Vault lint: exits non-zero if any finding is reported."""

import re
import sys
from pathlib import Path

VAULT = Path(__file__).parent.parent
RAW = VAULT / "raw"
WIKI = VAULT / "wiki"
INDEX = VAULT / "index"
DATA_SOURCES = WIKI / "data-sources"
LOG = INDEX / "log.md"
MOC = INDEX / "_map-of-content.md"

# Wikilinks that are intentionally not vault notes (author names, external refs)
KNOWN_EXTERNAL = {"Miguel Otero Pedrido"}

findings = []


def find(msg):
    findings.append(msg)


def all_stems():
    """All valid wikilink targets across raw/, wiki/, wiki/sources/, index/."""
    result = {}
    for folder in [RAW, WIKI, WIKI / "sources", INDEX]:
        if folder.exists():
            for p in folder.glob("*.md"):
                result[p.stem] = p
    return result


def wikilinks_in(text):
    return [m.group(1).strip() for m in re.finditer(r"\[\[([^\]|#]+)", text)]


stems = all_stems()

# 1. Broken wikilinks
for folder in [WIKI, INDEX]:
    for p in folder.rglob("*.md"):
        if DATA_SOURCES in p.parents:
            continue
        for link in wikilinks_in(p.read_text()):
            if link not in stems and link not in KNOWN_EXTERNAL:
                find(f"BROKEN WIKILINK: [[{link}]] in {p.relative_to(VAULT)}")

# 2. Orphan wiki notes (no inbound link from any wiki or index note)
linked_to = set()
for folder in [WIKI, INDEX]:
    for p in folder.rglob("*.md"):
        if DATA_SOURCES in p.parents:
            continue
        for link in wikilinks_in(p.read_text()):
            linked_to.add(link)

for p in WIKI.rglob("*.md"):
    if DATA_SOURCES in p.parents:
        continue
    if p.stem not in linked_to:
        find(f"ORPHAN NOTE: {p.relative_to(VAULT)} has no inbound links")

# 3. Raw clippings never referenced
for p in RAW.glob("*.md"):
    if p.stem not in linked_to:
        find(f"UNREFERENCED RAW: {p.stem}")

# 4. Wiki notes missing sources: frontmatter
#    (index-type, analysis-type, and wiki/sources/ notes are exempt)
for p in WIKI.glob("*.md"):
    text = p.read_text()
    if "type: index" in text or "type: analysis" in text:
        continue
    if "sources:" not in text:
        find(f"MISSING SOURCES: {p.relative_to(VAULT)}")

# 5. Each raw clipping has an ingest entry in log.md
if LOG.exists():
    log_text = LOG.read_text()
    for p in RAW.glob("*.md"):
        if p.stem not in log_text:
            find(f"NO LOG ENTRY: {p.name} missing from index/log.md")
else:
    find("MISSING FILE: index/log.md does not exist")

# 6. Every wiki note reachable from MOC (transitive link closure)
if MOC.exists():
    visited = set()
    queue = [MOC]
    while queue:
        current = queue.pop()
        if current in visited:
            continue
        visited.add(current)
        if not current.exists():
            continue
        for link in wikilinks_in(current.read_text()):
            if link in stems:
                queue.append(stems[link])
    reachable = {p.stem for p in visited}
    for p in WIKI.rglob("*.md"):
        if DATA_SOURCES in p.parents:
            continue
        if p.stem not in reachable:
            find(f"UNREACHABLE FROM MOC: {p.relative_to(VAULT)}")
else:
    find("MISSING FILE: index/_map-of-content.md does not exist")

# 7. Duplicate stems across folders
seen_stems: dict[str, Path] = {}
for folder in [RAW, WIKI, WIKI / "sources", INDEX]:
    if not folder.exists():
        continue
    for p in folder.glob("*.md"):
        if p.stem in seen_stems:
            find(
                f"DUPLICATE STEM: '{p.stem}' in {p.relative_to(VAULT)}"
                f" and {seen_stems[p.stem].relative_to(VAULT)}"
            )
        else:
            seen_stems[p.stem] = p

# 8. Data-source bundle shape and conventions
if DATA_SOURCES.exists():
    for bundle in DATA_SOURCES.iterdir():
        if not bundle.is_dir():
            find(f"DATA SOURCE: non-directory item in wiki/data-sources/: {bundle.name}")
            continue
        rel = bundle.relative_to(VAULT)
        overview_md = bundle / "overview.md"
        connect_py = bundle / "connect.py"
        tables_md = bundle / "tables.md"
        tables_dir = bundle / "tables"
        if not overview_md.exists():
            find(f"DATA SOURCE: missing overview.md in {rel}")
        elif not overview_md.read_text().strip().startswith("---"):
            find(f"DATA SOURCE: overview.md missing frontmatter in {rel}")
        if not connect_py.exists():
            find(f"DATA SOURCE: missing connect.py in {rel}")
        else:
            text = connect_py.read_text().strip()
            if not text.startswith(('"""', "'''", "#")):
                find(f"DATA SOURCE: connect.py missing docstring/comment header in {rel}")
        if not tables_md.exists():
            find(f"DATA SOURCE: missing tables.md in {rel}")
        elif not tables_md.read_text().strip().startswith("---"):
            find(f"DATA SOURCE: tables.md missing frontmatter in {rel}")
        if not tables_dir.exists():
            find(f"DATA SOURCE: missing tables/ directory in {rel}")
        elif not tables_dir.is_dir():
            find(f"DATA SOURCE: tables is not a directory in {rel}")
        else:
            table_files = sorted(tables_dir.glob("*.md"))
            for p in tables_dir.iterdir():
                if p.is_file() and p.suffix != ".md":
                    find(f"DATA SOURCE: non-markdown file in {p.relative_to(VAULT)}")
            for p in table_files:
                if not p.read_text().strip().startswith("---"):
                    find(f"DATA SOURCE: table file missing frontmatter: {p.relative_to(VAULT)}")
            if tables_md.exists():
                tables_text = tables_md.read_text()
                for p in table_files:
                    reference = str(p.relative_to(bundle))
                    if reference not in tables_text:
                        find(
                            f"DATA SOURCE: tables.md does not mention {reference}"
                            f" in {tables_md.relative_to(VAULT)}"
                        )

# Report
total = len(stems)
if findings:
    print(f"\n{'='*60}")
    print(f"VAULT LINT: {len(findings)} finding(s)  [{total} notes checked]")
    print("=" * 60)
    for f in findings:
        print(f"  • {f}")
    print()
    sys.exit(1)
else:
    print(f"VAULT LINT: OK — no findings  [{total} notes checked]")
    sys.exit(0)
