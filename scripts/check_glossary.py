#!/usr/bin/env python3
"""Gate: fail if glossary.csv is missing or has unapproved rows.

Usage:
  python scripts/check_glossary.py data/domain_terms/glossary.csv
  python scripts/check_glossary.py any/path/to/glossary.csv  # reads any CSV path

Exit codes: 0 OK (all rows approved), 1 blocked (missing / empty / invalid).

CSV columns: term_he,english,keep_source,notes,status,example_doc
Required columns: term_he, status (others optional).
Status enum: approved | proposed | keep_source | pending — pending is treated as
  unapproved (blocked) alongside proposed/keep_source; only approved passes.
# comments and empty lines are stripped before parsing (DictReader).
Required columns validated: term_he, status; invalid status values are errors.
Used by translate.py --check and CI.
"""
from __future__ import annotations

import argparse
import csv
try:
    from translation_common import strip_csv_comments
except ImportError:
    from scripts.translation_common import strip_csv_comments
import sys
from pathlib import Path

VALID_STATUSES = {"approved", "proposed", "keep_source", "pending"}
REQUIRED_COLUMNS = {"term_he", "status"}


def check_glossary(path: Path) -> tuple[bool, list[str]]:
    """Return (ok, errors). ok=False means translation is blocked."""
    path = Path(path)
    errors: list[str] = []
    if not path.exists():
        return False, [f"glossary not found: {path}"]
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return False, [f"cannot read glossary: {e}"]

    # Strip empty / comment lines (plan template has '# ...' comment lines) — via translation_common
    lines = strip_csv_comments(text)
    try:
        reader = csv.DictReader(lines)
    except Exception as e:
        return False, [f"csv parse error: {e}"]

    if reader.fieldnames is None:
        return False, ["empty glossary (no header)"]
    missing = REQUIRED_COLUMNS - set(reader.fieldnames)
    if missing:
        return False, [f"missing columns: {', '.join(sorted(missing))}"]

    unapproved: list[str] = []
    invalid: list[str] = []
    total = 0
    for i, row in enumerate(reader, 2):
        total += 1
        term = (row.get("term_he") or "").strip()
        status = (row.get("status") or "").strip()
        if status not in VALID_STATUSES:
            invalid.append(f"row {i} term={term!r}: invalid status {status!r}")
        elif status != "approved":
            unapproved.append(f"row {i} term={term!r}: status={status}")

    if total == 0:
        errors.append("glossary has no rows")
    errors.extend(invalid)
    if unapproved:
        errors.append(f"{len(unapproved)} unapproved row(s):")
        errors.extend(unapproved)
    return len(errors) == 0, errors


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check glossary.csv is fully approved")
    ap.add_argument("glossary", type=Path, help="path to glossary.csv")
    args = ap.parse_args(argv)
    ok, errors = check_glossary(args.glossary)
    if ok:
        print(f"glossary OK: {args.glossary}")
        return
    print(f"glossary BLOCKED: {args.glossary}", file=sys.stderr)
    for e in errors:
        print(f"  - {e}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
