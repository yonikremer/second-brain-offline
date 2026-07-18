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
APIS = WIKI / "apis"
LOG = INDEX / "log.md"
MOC = INDEX / "_map-of-content.md"

# Wikilinks that are intentionally not vault notes (author names, external refs)
KNOWN_EXTERNAL = {"Miguel Otero Pedrido"}

findings = []


def find(msg):
    findings.append(msg)


def all_stems():
    """All valid wikilink targets across raw/, wiki/, and index/."""
    result = {}
    for folder in [RAW, WIKI, INDEX]:
        if folder.exists():
            for p in folder.rglob("*.md"):
                # Duplicates are reported separately; keep the first path.
                result.setdefault(p.stem, p)
    return result


def wikilinks_in(text):
    return [m.group(1).strip() for m in re.finditer(r"\[\[([^\]|#]+)", text)]


def has_frontmatter(path: Path) -> bool:
    return path.read_text().strip().startswith("---")


def sources_empty(text: str) -> bool:
    """True if the frontmatter contains an empty sources list."""
    return bool(re.search(r"sources:\s*\[\s*\]", text))


def check_bundle_shape(
    root: Path,
    label: str,
    overview_suffix: str,
    py_suffix: str,
    guide_suffix: str,
    child_dir_name: str,
    child_suffix: str,
):
    """Validate a bundle namespace (data-source or API).

    Each file in a bundle carries a unique prefix so it can live in the
    global wiki namespace. The linter checks the required shape and
    cross-links so the standard orphan/reachability checks also pass.
    """
    if not root.exists():
        return
    for bundle in root.iterdir():
        if not bundle.is_dir():
            find(f"{label}: non-directory item in {root.relative_to(VAULT)}: {bundle.name}")
            continue
        rel = bundle.relative_to(VAULT)
        md_files = list(bundle.rglob("*.md"))
        py_files = list(bundle.rglob("*.py"))

        overviews = [p for p in md_files if p.name.endswith(overview_suffix)]
        if not overviews:
            find(f"{label}: missing *{overview_suffix} in {rel}")
        else:
            for p in overviews:
                if not has_frontmatter(p):
                    find(f"{label}: {p.name} missing frontmatter in {rel}")

        pys = [p for p in py_files if p.name.endswith(py_suffix)]
        if not pys:
            find(f"{label}: missing *{py_suffix} in {rel}")
        else:
            for p in pys:
                text = p.read_text().strip()
                if not text.startswith(('"""', "'''", "#")):
                    find(f"{label}: {p.name} missing docstring/comment header in {rel}")

        guides = [p for p in md_files if p.name.endswith(guide_suffix)]
        if not guides:
            find(f"{label}: missing *{guide_suffix} in {rel}")
        else:
            for p in guides:
                if not has_frontmatter(p):
                    find(f"{label}: {p.name} missing frontmatter in {rel}")

        child_dir = bundle / child_dir_name
        if not child_dir.exists():
            find(f"{label}: missing {child_dir_name}/ directory in {rel}")
        elif not child_dir.is_dir():
            find(f"{label}: {child_dir_name} is not a directory in {rel}")
        else:
            child_files = sorted(p for p in child_dir.iterdir() if p.is_file())
            for p in child_files:
                if p.suffix != ".md":
                    find(f"{label}: non-markdown file in {p.relative_to(VAULT)}")
            child_md = [
                p for p in child_files
                if p.suffix == ".md" and p.name.endswith(child_suffix)
            ]
            for p in child_md:
                if not has_frontmatter(p):
                    find(f"{label}: child file missing frontmatter: {p.relative_to(VAULT)}")
            for guide in guides:
                guide_links = set(wikilinks_in(guide.read_text()))
                for p in child_md:
                    if p.stem not in guide_links:
                        find(
                            f"{label}: {guide.name} does not link to [[{p.stem}]]"
                            f" ({p.relative_to(VAULT)})"
                        )

        # The overview should link to the guide so the guide is reachable.
        for overview in overviews:
            overview_links = set(wikilinks_in(overview.read_text()))
            for guide in guides:
                if guide.stem not in overview_links:
                    find(
                        f"{label}: {overview.name} does not link to [[{guide.stem}]]"
                        f" ({guide.relative_to(VAULT)})"
                    )


stems = all_stems()

# 1. Broken wikilinks
for folder in [WIKI, INDEX]:
    for p in folder.rglob("*.md"):
        for link in wikilinks_in(p.read_text()):
            if link not in stems and link not in KNOWN_EXTERNAL:
                find(f"BROKEN WIKILINK: [[{link}]] in {p.relative_to(VAULT)}")

# 2. Orphan wiki notes (no inbound link from any wiki or index note)
linked_to = set()
for folder in [WIKI, INDEX]:
    for p in folder.rglob("*.md"):
        for link in wikilinks_in(p.read_text()):
            linked_to.add(link)

for p in WIKI.rglob("*.md"):
    if p.stem not in linked_to:
        find(f"ORPHAN NOTE: {p.relative_to(VAULT)} has no inbound links")

# 3. Raw clippings never referenced
for p in RAW.glob("*.md"):
    if p.stem not in linked_to:
        find(f"UNREFERENCED RAW: {p.stem}")

# 4. Wiki notes missing or empty sources: frontmatter
#    (index-type and analysis-type notes are exempt; source-summary notes must cite sources)
for p in WIKI.rglob("*.md"):
    text = p.read_text()
    if "type: index" in text or "type: analysis" in text:
        continue
    if "sources:" not in text:
        find(f"MISSING SOURCES: {p.relative_to(VAULT)}")
    elif sources_empty(text):
        find(f"EMPTY SOURCES: {p.relative_to(VAULT)}")

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
        if p.stem not in reachable:
            find(f"UNREACHABLE FROM MOC: {p.relative_to(VAULT)}")
else:
    find("MISSING FILE: index/_map-of-content.md does not exist")

# 7. Duplicate stems across folders
seen_stems: dict[str, Path] = {}
for folder in [RAW, WIKI, INDEX]:
    if not folder.exists():
        continue
    for p in folder.rglob("*.md"):
        if p.stem in seen_stems:
            find(
                f"DUPLICATE STEM: '{p.stem}' in {p.relative_to(VAULT)}"
                f" and {seen_stems[p.stem].relative_to(VAULT)}"
            )
        else:
            seen_stems[p.stem] = p

# 8. Data-source bundle shape and conventions
check_bundle_shape(
    DATA_SOURCES,
    "DATA SOURCE",
    overview_suffix="-data-source-overview.md",
    py_suffix="-data-source-connect.py",
    guide_suffix="-data-source-tables.md",
    child_dir_name="tables",
    child_suffix="-data-source-table-",
)

# 9. API bundle shape and conventions
check_bundle_shape(
    APIS,
    "API",
    overview_suffix="-api-overview.md",
    py_suffix="-api-auth.py",
    guide_suffix="-api-endpoints.md",
    child_dir_name="endpoints",
    child_suffix="-api-endpoint-",
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
