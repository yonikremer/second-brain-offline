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


def check_review_queue(db_path: Path, total_files: int, high_rate_threshold: float = 0.25) -> dict[str, Any]:
    conn = _db_conn(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT stage, trigger_type, status FROM review_queue")
        rows = cursor.fetchall()
    finally:
        conn.close()

    pending = [r for r in rows if r["status"] == "pending"]
    stale = [r for r in rows if r["status"] == "stale"]
    by_trigger = Counter((r["stage"], r["trigger_type"]) for r in pending)
    review_rate = len(pending) / total_files if total_files else 0.0
    high_triggers = [
        {"stage": s, "trigger": t, "count": c}
        for (s, t), c in by_trigger.items()
        if c / total_files > high_rate_threshold
    ] if total_files else []

    return {
        "name": "Review queue signal",
        "ok": review_rate <= high_rate_threshold,
        "critical": False,
        "details": {
            "pending_count": len(pending),
            "stale_count": len(stale),
            "review_rate": review_rate,
            "by_trigger": dict(by_trigger),
            "high_triggers": high_triggers,
        },
    }


def sample_files(processed_paths: list[Path], sample_size: int, seed: int | None = None) -> list[Path]:
    if seed is not None:
        random.seed(seed)
    if len(processed_paths) <= sample_size:
        return processed_paths
    return random.sample(processed_paths, sample_size)


def get_file_hash(db_path: Path, filepath: Path) -> str | None:
    conn = _db_conn(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT file_hash FROM files WHERE filepath = ?", (str(filepath),))
        row = cursor.fetchone()
        return row["file_hash"] if row else None
    finally:
        conn.close()


def get_stage_outputs(db_path: Path, file_hash: str) -> dict[str, dict]:
    conn = _db_conn(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT stage_name, output_text, model_name, instructions_hash "
            "FROM stage_outputs WHERE file_hash = ?",
            (file_hash,),
        )
        return {row["stage_name"]: dict(row) for row in cursor.fetchall()}
    finally:
        conn.close()


def build_forensics(
    raw_path: Path,
    db_path: Path,
    raw_root: Path,
    processed_md_root: Path,
    review_dir: Path | None = None,
) -> dict:
    file_hash = get_file_hash(db_path, raw_path)
    rel_path = raw_path.relative_to(raw_root.resolve())
    out_path = processed_md_root / rel_path.with_suffix(".md")
    fm, _ = _extract_frontmatter(out_path) if out_path.exists() else (None, "")

    review_files = []
    if file_hash and review_dir is not None and review_dir.exists():
        short_hash = file_hash[:8]
        review_files = [str(p) for p in review_dir.glob(f"*{short_hash}*.md")]

    return {
        "raw_path": str(raw_path),
        "processed_path": str(out_path),
        "rel_path": str(rel_path.as_posix()),
        "current_hash": compute_file_hash(raw_path) if raw_path.exists() else None,
        "db_hash": file_hash,
        "stage_outputs": get_stage_outputs(db_path, file_hash) if file_hash else {},
        "processed_frontmatter": fm,
        "review_files": review_files,
    }


def main():
    pass


if __name__ == "__main__":
    main()
