#!/usr/bin/env python3
"""Single human review queue (CSV source of truth).

CSV path: data/review_queue/review_queue.csv
Markdown packets under data/review_queue/<batch>.md are rendered views, not parsed back.

Schema:
  FIELDNAMES = [term_he, english, keep_source, notes, status, example_doc,
                context_snippets, occurrences, blocked_docs, question_id]
  # NOTE: keep_source is intentionally string "0"/"1" in glossary CSV (not bool) — keep as is for CSV compat.
VALID_STATUSES = {approved, proposed, keep_source, pending}

Commands:
  list       csv                              # list pending rows
  gen-packets csv [--out-dir DIR] [--batch N] # render batch-*.md packets (default batch 20, ordered by blocked_docs desc)
  parse      csv [--ledger PATH] [--dry-run]  # validate edited CSV, append approved/keep_source to ledger
  clean      csv                              # report if queue is clean (all approved)

Ledger (canonical): vault/data/translations/ledger.jsonl
  --dry-run validates without writing ledger.
  --ledger overrides canonical path; default derives from csv parents.
"""
from __future__ import annotations

import argparse
import csv
try:
    from translation_common import read_csv_lines_skip_comments
except ImportError:
    from scripts.translation_common import read_csv_lines_skip_comments
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# NOTE: keep_source is intentionally string "0"/"1" in glossary CSV (not bool) — keep as is for CSV compat.
VALID_STATUSES = {"approved", "proposed", "keep_source", "pending"}
FIELDNAMES = ["term_he", "english", "keep_source", "notes", "status", "example_doc",
              "context_snippets", "occurrences", "blocked_docs", "question_id"]


def get_ledger_path(vault_root: Path) -> Path:
    """Canonical ledger path: vault_root/data/translations/ledger.jsonl."""
    return vault_root / "data" / "translations" / "ledger.jsonl"


def cmd_list(csv_path: Path):
    if not csv_path.exists():
        print(f"queue not found: {csv_path}", file=sys.stderr)
        sys.exit(1)
    with open(csv_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    pending = [r for r in rows if r.get("status") != "approved"]
    print(f"{len(rows)} rows, {len(pending)} pending approval")
    for r in pending[:30]:
        print(f"  {r.get('term_he',''):<20} -> {r.get('english',''):<20} [{r.get('status','')}] {r.get('occurrences','')} occ")


def cmd_gen_packets(csv_path: Path, out_dir: Path, batch: int = 20):
    if not csv_path.exists():
        print(f"queue not found: {csv_path}", file=sys.stderr)
        sys.exit(1)
    lines = read_csv_lines_skip_comments(csv_path)
    rows = list(csv.DictReader(lines)) if lines else []
    pending = [r for r in rows if r.get("status") != "approved"]
    # Order: blocked_docs desc
    pending.sort(key=lambda r: (-int(r.get("blocked_docs") or 0), r.get("term_he") or ""))
    out_dir.mkdir(parents=True, exist_ok=True)
    # Clear old packets
    for old in out_dir.glob("batch-*.md"):
        old.unlink()
    for i in range(0, len(pending), batch):
        chunk = pending[i: i + batch]
        pkt = out_dir / f"batch-{i // batch:02d}.md"
        lines = [f"# Review Batch {i // batch} ({len(chunk)} terms)\n", "Edit `review_queue.csv` status column to `approved`/`keep_source`.\n"]
        for r in chunk:
            lines.append(f"## {r.get('term_he','')} -> {r.get('english','')}")
            lines.append(f"- Status: {r.get('status','')}")
            lines.append(f"- Occurrences: {r.get('occurrences','')}, Blocked docs: {r.get('blocked_docs','')}")
            if r.get("context_snippets"):
                lines.append(f"- Context: {r.get('context_snippets','')[:200]}")
            if r.get("example_doc"):
                lines.append(f"- File: {r.get('example_doc','')}")
            lines.append("")
        pkt.write_text("\n".join(lines), encoding="utf-8")
        print(f"Wrote {pkt} ({len(chunk)} terms)")


def cmd_parse(csv_path: Path, ledger_path: Path | None, dry_run: bool = False):
    if not csv_path.exists():
        print(f"queue not found: {csv_path}", file=sys.stderr)
        sys.exit(1)
    # Strip # comments like glossary loaders (review_queue.py:95 fix)
    lines = read_csv_lines_skip_comments(csv_path)
    if not lines:
        print("empty queue", file=sys.stderr)
        sys.exit(1)
    reader = csv.DictReader(lines)
    if not reader.fieldnames:
        print("empty queue", file=sys.stderr)
        sys.exit(1)
    rows = list(reader)

    errors: list[str] = []
    for i, r in enumerate(rows, 2):
        status = (r.get("status") or "").strip()
        if status not in VALID_STATUSES and status != "":
            errors.append(f"row {i} term={r.get('term_he','')!r}: invalid status {status!r}")

    if errors:
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        print(f"Parse failed: {len(errors)} errors", file=sys.stderr)
        sys.exit(1)

    approved = [r for r in rows if (r.get("status") or "").strip() == "approved"]
    print(f"Parse OK: {len(approved)}/{len(rows)} approved")
    if dry_run:
        print("(dry-run, not writing ledger)")
        return

    if ledger_path:
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        # Compute glossary_version from glossary.csv next to ledger or sibling
        glossary_version = ""
        glossary_candidates = [
            ledger_path.parents[1] / "domain_terms" / "glossary.csv",
            ledger_path.parent.parent / "domain_terms" / "glossary.csv",
        ]
        # vault_root derived from ledger_path: vault/data/translations/ledger.jsonl -> vault = parents[2]
        try:
            vault_from_ledger = ledger_path.parents[2] if len(ledger_path.parents) >= 3 else None
            if vault_from_ledger is not None:
                glossary_candidates.insert(0, vault_from_ledger / "data" / "domain_terms" / "glossary.csv")
        except Exception:
            pass
        for cand in glossary_candidates:
            if cand.exists():
                try:
                    glossary_version = hashlib.sha256(cand.read_bytes()).hexdigest()[:10]
                except OSError:
                    pass
                break
        # Derive glossary_version if glossary exists next to ledger
        for r in rows:
            if (r.get("status") or "").strip() in ("approved", "keep_source"):
                event = {
                    "event": "question_answered",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "term_he": r.get("term_he", ""),
                    "english": r.get("english", ""),
                    "status": r.get("status", ""),
                    "decided_by": "human",
                    "glossary_version": glossary_version,
                }
                with open(ledger_path, "a", encoding="utf-8") as lf:
                    lf.write(json.dumps(event, ensure_ascii=False) + "\n")
        print(f"Appended to {ledger_path}")


def cmd_clean(csv_path: Path):
    # Remove approved rows that have been fully retranslated? For now just report.
    if not csv_path.exists():
        print(f"queue not found: {csv_path}", file=sys.stderr)
        sys.exit(1)
    lines = read_csv_lines_skip_comments(csv_path)
    rows = list(csv.DictReader(lines)) if lines else []
    pending = [r for r in rows if r.get("status") != "approved"]
    if pending:
        print(f"{len(pending)} pending rows remain, not cleaning.")
        return
    print("All rows approved — queue is clean.")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Human review queue (CSV source of truth)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_list = sub.add_parser("list", help="list pending rows")
    p_list.add_argument("csv", type=Path, help="review_queue.csv")

    p_gen = sub.add_parser("gen-packets", help="render markdown packets from CSV")
    p_gen.add_argument("csv", type=Path)
    p_gen.add_argument("--out-dir", type=Path, default=None)
    p_gen.add_argument("--batch", type=int, default=20)

    p_parse = sub.add_parser("parse", help="validate edited CSV, append to ledger")
    p_parse.add_argument("csv", type=Path)
    p_parse.add_argument("--ledger", type=Path, default=None)
    p_parse.add_argument("--dry-run", action="store_true")

    p_clean = sub.add_parser("clean", help="report if queue is clean")
    p_clean.add_argument("csv", type=Path)

    args = ap.parse_args(argv)
    if args.cmd == "list":
        cmd_list(args.csv)
    elif args.cmd == "gen-packets":
        out_dir = args.out_dir or args.csv.parent
        cmd_gen_packets(args.csv, out_dir, batch=args.batch)
    elif args.cmd == "parse":
        ledger = args.ledger
        # Canonical ledger: <vault>/data/translations/ledger.jsonl
        if ledger is None:
            try:
                vault_root = args.csv.resolve().parents[2]
                candidate = get_ledger_path(vault_root)
                # Fallback to old sibling heuristic if vault layout not detected
                if not candidate.parent.exists():
                    cand2 = args.csv.parents[1] / "translations" / "ledger.jsonl" if len(args.csv.parents) > 1 else None
                    ledger = cand2 if cand2 and cand2.parent.exists() else candidate
                else:
                    ledger = candidate
            except Exception:
                ledger = args.csv.parents[1] / "translations" / "ledger.jsonl" if len(args.csv.parents) > 1 else args.csv.parent / "ledger.jsonl"
        # If ledger parent doesn't exist, don't create ledger silently
        if ledger and not ledger.parent.exists() and not args.dry_run:
            ledger = None
        cmd_parse(args.csv, ledger, dry_run=args.dry_run)
    elif args.cmd == "clean":
        cmd_clean(args.csv)


if __name__ == "__main__":
    main()
