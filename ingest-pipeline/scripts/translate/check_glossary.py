#!/usr/bin/env python3
"""Gate: fail if glossary.json is missing or has unapproved rows.

Usage:
  python scripts/check_glossary.py data/domain_terms/glossary.json
  python scripts/check_glossary.py any/path/to/glossary.json

Exit codes: 0 OK (all rows approved), 1 blocked (missing / empty / invalid).

glossary.json schema: [{term_he, translations:[], keep_source, notes, status, example_doc}]
Required fields: term_he, status, translations.
Status enum: approved | proposed | keep_source | pending — pending is treated as
  unapproved (blocked) alongside proposed; only approved/keep_source passes.
Required columns validated: term_he, status; invalid status values are errors.
Used by translate.py --check and CI.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

VALID_STATUSES = {"approved", "proposed", "keep_source", "pending"}
REQUIRED_FIELDS = {"term_he", "status"}


def check_glossary(path: Path) -> tuple[bool, list[str]]:
    """Return (ok, errors). ok=False means translation is blocked."""
    path = Path(path)
    errors: list[str] = []
    if not path.exists():
        return False, [f"glossary not found: {path}"]
    if path.suffix != ".json":
        return False, [f"glossary must be .json, got {path.suffix}: {path}"]
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return False, [f"cannot read glossary: {e}"]

    try:
        rows = json.loads(text)
    except json.JSONDecodeError as e:
        return False, [f"json parse error: {e}"]

    if not isinstance(rows, list):
        return False, ["glossary must be a JSON array"]
    if not rows:
        return False, ["glossary has no rows"]

    # Header/field checks
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            errors.append(f"row {i}: not an object")
            continue
        missing = REQUIRED_FIELDS - set(row.keys())
        if missing:
            errors.append(f"row {i} term={row.get('term_he','')!r}: missing fields {', '.join(sorted(missing))}")
        if "translations" not in row:
            errors.append(f"row {i} term={row.get('term_he','')!r}: missing field 'translations'")

    unapproved: list[str] = []
    invalid: list[str] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        term = (row.get("term_he") or "").strip()
        status = (row.get("status") or "").strip()
        if status not in VALID_STATUSES:
            invalid.append(f"row {i} term={term!r}: invalid status {status!r}")
        elif status not in ("approved", "keep_source"):
            unapproved.append(f"row {i} term={term!r}: status={status}")

        # Validate translations field (after filtering invalid stubs)
        trans = row.get("translations")
        if trans is not None:
            if not isinstance(trans, list):
                errors.append(f"row {i} term={term!r}: translations must be a list")
            else:
                try:
                    from .translation_common import _filter_translations as _flt
                except ImportError:
                    from translation_common import _filter_translations as _flt
                valid = _flt(trans)
                if status == "approved" and not valid:
                    errors.append(f"row {i} term={term!r}: approved row has no valid translations after filtering (all options were invalid stubs)")
                elif status == "keep_source" and valid:
                    # keep_source rows should have empty translations — warn but don't block
                    pass

    errors.extend(invalid)
    if unapproved:
        errors.append(f"{len(unapproved)} unapproved row(s):")
        errors.extend(unapproved)

    # Gate glossary collisions before any work starts
    from .translation_common import check_glossary_collisions
    try:
        check_glossary_collisions(rows if isinstance(rows, list) else [])
    except RuntimeError as e:
        errors.append(str(e))
    return len(errors) == 0, errors


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check glossary.json is fully approved")
    ap.add_argument("glossary", type=Path, help="path to glossary.json")
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
