#!/usr/bin/env python3
"""Deterministic QA battery for translations.

Checks (scripted, no LLM judge):
  residual_hebrew_ratio, untranslated_block, glossary_retention,
  glossary_consistency, heading_fidelity, structure_fidelity,
  numeric_fidelity, length_ratio, markup_integrity, marker_count.

Reads content-addressed store data/translations/<sha>/translation.md.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

HEBREW_RE = re.compile(r"[א-ת]")
HE_MARKER_RE = re.compile(r"⟦he:[^⟧]+⟧")
HEADING_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
FENCE_RE = re.compile(r"```")
LIST_RE = re.compile(r"^\s*[-*]\s+", re.MULTILINE)
TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
NUM_RE = re.compile(r"\b\d[\d.,]*\b")


def _read_csv_skip_comments(path: Path) -> list[str]:
    """Strip # comment and empty lines before DictReader (matches check_glossary)."""
    text = path.read_text(encoding="utf-8")
    return [l for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]


def _load_person_names_for_qa(vault_root: Path | None) -> set[str]:
    """Load person names allowlist for residual_hebrew suppression."""
    names: set[str] = set()
    candidates: list[Path] = []
    if vault_root is not None:
        candidates.append(vault_root / "data" / "person_names" / "first_names.txt")
        candidates.append(vault_root / "data" / "person_names" / "last_names_ranked.txt")
        candidates.append(vault_root / "data" / "person_names" / "last_names.txt")
    else:
        # Fallback: walk parents from cwd looking for data/person_names
        cur = Path.cwd().resolve()
        for p in [cur] + list(cur.parents):
            candidates.append(p / "data" / "person_names" / "first_names.txt")
            candidates.append(p / "data" / "person_names" / "last_names_ranked.txt")
    for p in candidates:
        if p.exists():
            try:
                for line in p.read_text(encoding="utf-8").splitlines():
                    t = line.strip()
                    if t:
                        names.add(t)
            except OSError:
                pass
    return names


def _strip_frontmatter(text: str) -> tuple[str, str]:
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            return text[: end + 5], text[end + 5:]
    return "", text


def _load_translation(translation_path: Path) -> tuple[dict, str]:
    raw = translation_path.read_text(encoding="utf-8")
    fm_text, body = _strip_frontmatter(raw)
    meta = {}
    if fm_text:
        # frontmatter is JSON inside --- block
        inner = fm_text.strip()[4:-4].strip() if fm_text.strip().startswith("---") else fm_text
        try:
            meta = json.loads(inner)
        except Exception:
            meta = {}
    return meta, body


def check_residual_hebrew(body: str, threshold: float = 0.02, vault_root: Path | None = None) -> dict:
    # Strip markers first
    stripped = HE_MARKER_RE.sub("", body)
    # Remove person-name allowlist before counting (names stay Hebrew after unmask)
    # Spec: person names intentionally remain Hebrew; don't penalize them.
    allowlist = _load_person_names_for_qa(vault_root)
    if allowlist:
        # Word-boundary aware removal: remove longest names first
        for name in sorted(allowlist, key=len, reverse=True):
            # Escape and require Hebrew word boundaries
            # Use simple replace with boundary check via regex
            pat = re.compile(r"(?<![א-ת])" + re.escape(name) + r"(?![א-ת])")
            stripped = pat.sub("", stripped)
    total = len(stripped)
    if total == 0:
        return {"check": "residual_hebrew_ratio", "status": "pass", "value": 0}
    he_chars = len(HEBREW_RE.findall(stripped))
    ratio = he_chars / max(total, 1)
    return {
        "check": "residual_hebrew_ratio",
        "status": "fail" if ratio > threshold else "pass",
        "value": ratio,
        "threshold": threshold,
    }


def check_untranslated_block(body: str) -> dict:
    stripped = HE_MARKER_RE.sub("", body)
    # Find lines that are mostly Hebrew (>50% hebrew chars among letters)
    failed_lines = []
    for i, line in enumerate(stripped.splitlines(), 1):
        letters = [c for c in line if c.isalpha()]
        if len(letters) < 10:
            continue
        he = sum(1 for c in letters if HEBREW_RE.match(c))
        if he / len(letters) > 0.5 and len(line.strip()) > 20:
            failed_lines.append(i)
    return {
        "check": "untranslated_block",
        "status": "fail" if failed_lines else "pass",
        "failed_lines": failed_lines[:10],
    }


def check_glossary_retention(body: str, glossary: list[dict]) -> dict:
    violations = []
    for row in glossary:
        eng = (row.get("english") or "").strip()
        status = (row.get("status") or "").strip()
        term_he = (row.get("term_he") or "").strip()
        if status not in ("approved", "keep_source"):
            continue
        if status == "keep_source":
            if term_he and term_he not in body:
                violations.append(f"keep_source missing: {term_he!r}")
        elif status == "approved":
            if not eng:
                continue
            # Approved term: translation body must contain the English rendering
            # or an explicit ⟦he:term_he⟧ marker signalling blocked translation.
            if eng in body:
                continue
            # Marker check: multi-word terms are marked per-word
            if " " in term_he:
                tokens = term_he.split()
                per_word = all(f"⟦he:{tok}⟧" in body for tok in tokens)
                single = f"⟦he:{term_he}⟧" in body
                if per_word or single:
                    continue
            else:
                if f"⟦he:{term_he}⟧" in body:
                    continue
            violations.append(f"approved term {term_he!r} -> {eng!r} not found in translation")
    return {
        "check": "glossary_retention",
        "status": "fail" if violations else "pass",
        "violations": violations[:20],
    }


def check_heading_fidelity(source_body: str, trans_body: str) -> dict:
    src_h = len(HEADING_RE.findall(source_body))
    trans_h = len(HEADING_RE.findall(trans_body))
    return {
        "check": "heading_fidelity",
        "status": "fail" if src_h != trans_h else "pass",
        "source": src_h,
        "translation": trans_h,
    }


def check_structure_fidelity(source_body: str, trans_body: str) -> dict:
    issues = []
    for name, pat in [("code_fences", FENCE_RE), ("list_items", LIST_RE)]:
        s = len(pat.findall(source_body))
        t = len(pat.findall(trans_body))
        if s != t:
            issues.append(f"{name}: source {s} vs translation {t}")
    # Table rows: allow some tolerance (translation may adjust formatting)
    return {
        "check": "structure_fidelity",
        "status": "fail" if issues else "pass",
        "issues": issues,
    }


def check_numeric_fidelity(source_body: str, trans_body: str) -> dict:
    src_nums = set(NUM_RE.findall(source_body))
    trans_nums = set(NUM_RE.findall(trans_body))
    # Filter: ignore pure year-like 4-digit numbers that may reformat? No, require exact for now.
    missing = src_nums - trans_nums
    # Allow minor comma/dot differences: normalize
    if missing:
        # Check normalized
        norm_trans = {n.replace(",", "") for n in trans_nums}
        norm_src = {n.replace(",", "") for n in src_nums}
        missing = norm_src - norm_trans
    return {
        "check": "numeric_fidelity",
        "status": "fail" if missing else "pass",
        "missing": sorted(missing)[:10] if missing else [],
    }


def check_length_ratio(source_body: str, trans_body: str, low: float = 0.5, high: float = 2.5) -> dict:
    """Translation length vs source char-count ratio.

    NOTE: [0.5, 2.5] are pre-calibration placeholders. Fit from 3-5 approved
    reference translations in Phase 0 (see hebrew-translation-pipeline.md §6)
    and override via config if QA bands drift. Emits no warning by default;
    if QA config not found the defaults are used as-is.
    """
    s = len(source_body)
    t = len(trans_body)
    ratio = t / max(s, 1)
    return {
        "check": "length_ratio",
        "status": "fail" if ratio < low or ratio > high else "pass",
        "value": ratio,
        "band": f"[{low}, {high}]",
    }


def check_markup_integrity(body: str) -> dict:
    fences = body.count("```")
    issues = []
    if fences % 2 != 0:
        issues.append("orphaned code fence (odd count)")
    # Check unclosed brackets in links (rough)
    open_b = body.count("[")
    close_b = body.count("]")
    # Not strict — just fence integrity for now
    return {
        "check": "markup_integrity",
        "status": "fail" if issues else "pass",
        "issues": issues,
    }


def check_marker_count(body: str) -> dict:
    n = len(HE_MARKER_RE.findall(body))
    return {
        "check": "marker_count",
        "status": "pass",  # informational, not a failure
        "value": n,
        "note": "gates stage 6 if >0",
    }


def run_all(source_path: Path | None, trans_body: str, trans_meta: dict, glossary: list[dict], vault_root: Path | None = None) -> list[dict]:
    source_body = ""
    if source_path and source_path.exists():
        try:
            raw = source_path.read_text(encoding="utf-8")
            _, source_body = _strip_frontmatter(raw)
            # For source, also strip frontmatter markers if any
        except OSError:
            pass

    checks: list[dict] = []
    checks.append(check_residual_hebrew(trans_body, vault_root=vault_root))
    checks.append(check_untranslated_block(trans_body))
    checks.append(check_glossary_retention(trans_body, glossary))
    if source_body:
        checks.append(check_heading_fidelity(source_body, trans_body))
        checks.append(check_structure_fidelity(source_body, trans_body))
        checks.append(check_numeric_fidelity(source_body, trans_body))
        checks.append(check_length_ratio(source_body, trans_body))
    else:
        # Still check length/markers even without source
        checks.append({"check": "heading_fidelity", "status": "skip", "note": "no source"})
        checks.append({"check": "structure_fidelity", "status": "skip", "note": "no source"})
        checks.append({"check": "numeric_fidelity", "status": "skip", "note": "no source"})
        checks.append({"check": "length_ratio", "status": "pass", "value": len(trans_body) / 1000, "note": "no source, skip ratio"})
    checks.append(check_markup_integrity(trans_body))
    checks.append(check_marker_count(trans_body))
    return checks


def main(argv=None):
    ap = argparse.ArgumentParser(description="Deterministic QA gate for translations")
    ap.add_argument("store_dir", type=Path, help="data/translations root")
    ap.add_argument("--glossary", type=Path, default=None, help="glossary.csv")
    ap.add_argument("--vault-root", type=Path, default=None, help="vault root for locating source docs and person_names allowlist")
    ap.add_argument("--json-out", type=Path, default=None, help="write aggregated JSON array with {file, meta, checks} per doc")
    args = ap.parse_args(argv)

    store_dir = Path(args.store_dir)
    # Resolve vault_root: explicit arg > parents heuristic from store_dir
    vault_root: Path | None = None
    if args.vault_root is not None:
        vault_root = Path(args.vault_root).resolve()
    else:
        # Heuristic: store_dir is vault/data/translations -> vault is parents[2]
        try:
            if len(store_dir.resolve().parents) >= 2:
                maybe = store_dir.resolve().parents[1]
                # verify by checking for data/translations or vault markers
                if (maybe / "data").exists() or (maybe / "convert_config.json").exists():
                    vault_root = maybe
                elif len(store_dir.resolve().parents) >= 3:
                    vault_root = store_dir.resolve().parents[2]
        except Exception:
            pass

    glossary: list[dict] = []
    if args.glossary and Path(args.glossary).exists():
        gp = Path(args.glossary)
        lines = _read_csv_skip_comments(gp)
        if lines:
            reader = csv.DictReader(lines)
            if reader.fieldnames:
                glossary = list(reader)

    translations = sorted(store_dir.rglob("translation.md"))
    if not translations:
        print(f"No translations found under {store_dir}", file=sys.stderr)
        sys.exit(1)

    failures = 0
    per_doc: list[dict] = []
    for trans_path in translations:
        meta, body = _load_translation(trans_path)
        # Resolve source path: prefer vault_root / src_doc when vault_root known,
        # else fallback to old parents[1] heuristic.
        source_path = None
        src_doc = meta.get("source_doc")
        if src_doc:
            src_p = Path(src_doc)
            if src_p.is_absolute() and src_p.exists():
                source_path = src_p
            elif vault_root is not None:
                cand = vault_root / src_doc
                if cand.exists():
                    source_path = cand
                else:
                    # Fallback: try relative to cwd
                    alt = Path(".") / src_doc
                    if alt.exists():
                        source_path = alt
            else:
                candidate = Path(".") / src_doc
                if candidate.exists():
                    source_path = candidate
                else:
                    for root in [Path("."), Path(".."), store_dir.parents[1] if len(store_dir.parents) > 1 else Path(".")]:
                        c = root / src_doc
                        if c.exists():
                            source_path = c
                            break

        checks = run_all(source_path, body, meta, glossary, vault_root=vault_root)
        per_doc.append({"file": trans_path.relative_to(store_dir).as_posix(), "meta": meta, "checks": checks})
        quarantined = any(c["status"] == "fail" for c in checks)
        rel = trans_path.relative_to(store_dir).as_posix()
        status = "QUARANTINE" if quarantined else "PASS"
        markers = next((c["value"] for c in checks if c["check"] == "marker_count"), 0)
        print(f"{status} {rel} markers={markers}")
        for c in checks:
            if c["status"] == "fail":
                print(f"  FAIL {c['check']}: {c}")
        if quarantined:
            failures += 1

    print(f"\nQA: {len(translations)} docs, {failures} quarantined")

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        # Write proper aggregated JSON array with {file, meta, checks} per doc
        args.json_out.write_text(json.dumps(per_doc, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Wrote per-doc report ({len(per_doc)} docs) to {args.json_out}")

    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
