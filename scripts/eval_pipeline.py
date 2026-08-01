import argparse
import hashlib
import random
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from helpers import compute_file_hash, parse_allowed_values


REQUIRED_FRONTMATTER_KEYS = [
    "original_path",
    "file_hash",
    "subdomain",
    "document_type",
    "truthness_score",
    "truthness_justification",
    "language",
    "model",
]


def load_allowed_categories(instruction_path: Path) -> set[str]:
    return set(parse_allowed_values(instruction_path))


def _extract_frontmatter(out_path: Path) -> tuple[dict | None, str]:
    content = out_path.read_text(encoding="utf-8")
    if not content.startswith("---"):
        return None, content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return None, content
    try:
        fm = yaml.safe_load(parts[1])
    except Exception:
        return None, content
    return (fm if isinstance(fm, dict) else None), parts[2]


def check_processed_file(
    out_path: Path,
    raw_root: Path,
    allowed_subdomains: set[str],
    allowed_doc_types: set[str],
) -> dict:
    result = {"path": str(out_path), "errors": [], "warnings": []}
    fm, body = _extract_frontmatter(out_path)
    if fm is None:
        result["errors"].append("Missing or malformed YAML frontmatter")
        return result

    for key in REQUIRED_FRONTMATTER_KEYS:
        if key not in fm:
            result["errors"].append(f"Missing frontmatter key: {key}")

    if not body.strip():
        result["warnings"].append("Empty body")

    subdomain = fm.get("subdomain")
    if subdomain not in allowed_subdomains:
        result["errors"].append(f"Unknown subdomain: {subdomain}")

    doc_type = fm.get("document_type")
    if doc_type not in allowed_doc_types:
        result["errors"].append(f"Unknown document_type: {doc_type}")

    score = fm.get("truthness_score")
    if not isinstance(score, (int, float)) or score < 0 or score > 10:
        result["errors"].append(f"Invalid truthness_score: {score}")

    original_path = fm.get("original_path")
    file_hash = fm.get("file_hash")
    if original_path and file_hash:
        src = raw_root / original_path
        if not src.exists():
            result["errors"].append(f"original_path not found: {original_path}")
        elif compute_file_hash(src) != str(file_hash):
            result["errors"].append("file_hash mismatch (stale output)")

    return result


def check_output_health(
    raw_root: Path,
    processed_md_root: Path,
    db_path: Path,
    instructions_root: Path,
) -> dict[str, Any]:
    allowed_subdomains = load_allowed_categories(instructions_root / "subdomains.md")
    allowed_doc_types = load_allowed_categories(instructions_root / "document_types.md")
    statuses = get_file_statuses(db_path)
    processed_paths = [
        Path(fp) for fp, info in statuses.items() if info.get("status") == "processed"
    ]

    checked = []
    missing_outputs = []
    raw_root_resolved = raw_root.resolve()
    for raw_path in processed_paths:
        try:
            rel = raw_path.relative_to(raw_root_resolved)
        except ValueError:
            checked.append(
                {
                    "path": str(raw_path),
                    "errors": ["Raw path is not under raw_root"],
                    "warnings": [],
                }
            )
            continue
        out_path = processed_md_root / rel.with_suffix(".md")
        if not out_path.exists():
            missing_outputs.append(str(rel.as_posix()))
            continue
        checked.append(
            check_processed_file(
                out_path, raw_root_resolved, allowed_subdomains, allowed_doc_types
            )
        )

    errors = missing_outputs + [
        f"{c['path']}: {e}" for c in checked for e in c["errors"]
    ]
    warnings = [f"{c['path']}: {w}" for c in checked for w in c["warnings"]]

    return {
        "name": "Output health",
        "ok": not errors,
        "critical": True,
        "details": {
            "processed_count": len(processed_paths),
            "missing_outputs": missing_outputs,
            "file_errors": checked,
        },
        "errors": errors,
        "warnings": warnings,
    }


TERMINAL_STATUSES = {"processed", "filtered", "error", "needs_review", "skipped"}


def _db_conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def get_raw_files(raw_root: Path) -> list[Path]:
    files = []
    for p in raw_root.rglob("*"):
        if p.is_file() and not p.name.startswith("."):
            files.append(p.resolve())
    return sorted(files)


def get_file_statuses(db_path: Path) -> dict[str, dict]:
    conn = _db_conn(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT filepath, file_hash, status, error_message FROM files")
        return {row["filepath"]: dict(row) for row in cursor.fetchall()}
    finally:
        conn.close()


def check_coverage(raw_files: list[Path], file_statuses: dict[str, dict]) -> dict[str, Any]:
    # Normalize paths to POSIX so Windows raw paths match POSIX DB entries.
    raw_strs = {Path(p).as_posix() for p in raw_files}
    normalized_statuses = {Path(k).as_posix(): v for k, v in file_statuses.items()}
    missing = sorted(raw_strs - set(normalized_statuses))
    statuses = list(normalized_statuses.values())
    pending = [s for s in statuses if s.get("status") == "pending"]
    unknown = [s for s in statuses if s.get("status") not in TERMINAL_STATUSES | {"pending"}]
    terminal_count = sum(1 for s in statuses if s.get("status") in TERMINAL_STATUSES)
    ok = (
        not missing
        and not pending
        and not unknown
        and terminal_count == len(raw_files)
    )
    return {
        "name": "Coverage",
        "ok": ok,
        "critical": True,
        "details": {
            "raw_count": len(raw_files),
            "db_count": len(file_statuses),
            "missing": missing,
            "pending_count": len(pending),
            "unknown_status_count": len(unknown),
            "terminal_count": terminal_count,
        },
    }


def main():
    pass


if __name__ == "__main__":
    main()
