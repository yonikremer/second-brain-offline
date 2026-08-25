#!/usr/bin/env python3
"""Deterministic QA battery for translations.

Checks (scripted, no LLM judge):
  residual_hebrew_ratio, untranslated_block, glossary_retention,
  heading_fidelity, structure_fidelity, numeric_fidelity,
  length_ratio, markup_integrity, marker_count,
  preserved_invariants (names + English spans + URLs/paths — extracted from source, verified verbatim in output)
  (glossary_consistency lives in reviewer via marker-only sweep, not here)

Thresholds (pre-calibration placeholders, fit from Phase-0 references):
  residual_hebrew_ratio threshold 0.02 (person-name allowlist suppressed)
  length_ratio band [0.5, 2.5] — to be fitted from 3-5 approved reference translations

CLI:
  python scripts/translation_qa.py <store_dir> [--glossary PATH] [--vault-root PATH] [--json-out PATH]
  store_dir positional: data/translations root (content-addressed <sha>/translation.md)
  --glossary PATH       glossary.json (optional, enables glossary_retention)
  --vault-root PATH     vault root for locating source docs and person_names allowlist
  --json-out PATH       write aggregated JSON array [{file, meta, checks}] per doc

Usage example:
  python scripts/translation_qa.py data/translations --glossary data/domain_terms/glossary.json --json-out qa.json

Reads content-addressed store data/translations/<sha>/translation.md.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path


try:
    from .translation_common import _valid_translation_option, _filter_translations  # DRY
    _HAS_COMMON_FILTER = True
except ImportError:
    try:
        from translation_common import _valid_translation_option, _filter_translations
        _HAS_COMMON_FILTER = True
    except ImportError:
        _HAS_COMMON_FILTER = False
        def _valid_translation_option(t: str) -> bool:
            return bool(t and t.strip())
        def _filter_translations(raw):
            return [str(o).strip() for o in (raw or []) if str(o).strip()]

# Shared helpers (deduped) — translation_common is single source of truth
try:
    from .translation_common import read_csv_lines_skip_comments as _shared_read_csv, strip_frontmatter as _shared_strip_fm, split_table_cells as _shared_split_cells
    _USE_SHARED = True
except ImportError:
    try:
        from .translation_common import read_csv_lines_skip_comments as _shared_read_csv, strip_frontmatter as _shared_strip_fm, split_table_cells as _shared_split_cells
        _USE_SHARED = True
    except ImportError:
        _USE_SHARED = False

HEBREW_RE = re.compile(r"[א-ת]")
HE_MARKER_RE = re.compile(r"⟦he:[^⟧]+⟧")
HEADING_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
FENCE_RE = re.compile(r"```")
LIST_RE = re.compile(r"^\s*[-*]\s+", re.MULTILINE)
TABLE_ROW_RE = re.compile(r"^\s*.*\|.*$", re.MULTILINE)
NUM_RE = re.compile(r"\b\d[\d.,]*\b")

# Table fidelity: strict GFM table invariants (highest-risk construct)
# Keep in sync with scripts/md_mask.py (_TABLE_SEP_RE, _split_table_cells).
_TABLE_SEP_QA_RE = re.compile(r"^\s*\|?(\s*:?-+:?\s*\|)+\s*:?-+:?\s*\|?\s*$")


def _split_row_cells(row: str) -> list[str]:
    """Split GFM row on unescaped pipes — via translation_common (kept for compat)."""
    if _USE_SHARED:
        return _shared_split_cells(row)
    parts: list[str] = []
    cur = ""
    j = 0
    while j < len(row):
        if row[j] == "\\" and j + 1 < len(row) and row[j + 1] == "|":
            cur += "\\|"
            j += 2
            continue
        if row[j] == "|":
            parts.append(cur)
            cur = ""
            j += 1
            continue
        cur += row[j]
        j += 1
    parts.append(cur)
    if parts and parts[0].strip() == "":
        parts = parts[1:]
    if parts and parts[-1].strip() == "":
        parts = parts[:-1]
    return parts
def _parse_tables(text: str) -> list[list[list[str]]]:
    """Parse GFM tables into list of tables, each table is list of rows, each row is list of cells."""
    lines = text.split("\n")
    tables: list[list[list[str]]] = []
    i = 0
    while i < len(lines):
        if i + 1 < len(lines) and re.match(r"^\s*.*\|.*$", lines[i]) and _TABLE_SEP_QA_RE.match(lines[i + 1]):
            rows: list[list[str]] = []
            rows.append([c.strip() for c in _split_row_cells(lines[i])])
            i += 2
            while i < len(lines) and re.match(r"^\s*.*\|.*$", lines[i]) and not _TABLE_SEP_QA_RE.match(lines[i]):
                rows.append([c.strip() for c in _split_row_cells(lines[i])])
                i += 1
            tables.append(rows)
            continue
        i += 1
    return tables


def _collect_separators(text: str) -> list[str]:
    return [ln.strip() for ln in text.split("\n") if _TABLE_SEP_QA_RE.match(ln)]


def check_table_fidelity(source_body: str, trans_body: str) -> dict:
    """Fail if table column counts, row counts, or separator alignment drift."""
    src_tables = _parse_tables(source_body)
    trans_tables = _parse_tables(trans_body)
    issues: list[str] = []
    if len(src_tables) != len(trans_tables):
        return {
            "check": "table_fidelity",
            "status": "fail",
            "issues": [f"table count: source {len(src_tables)} vs translation {len(trans_tables)}"],
        }
    src_seps = _collect_separators(source_body)
    trans_seps = _collect_separators(trans_body)
    if len(src_seps) != len(trans_seps):
        issues.append(f"separator rows: source {len(src_seps)} vs translation {len(trans_seps)}")
    else:
        for idx, (s, t) in enumerate(zip(src_seps, trans_seps)):
            if s != t:
                if s.replace(" ", "") != t.replace(" ", ""):
                    issues.append(f"table {idx} separator changed: {s!r} -> {t!r}")
    for ti, (sr, tr) in enumerate(zip(src_tables, trans_tables)):
        if len(sr) != len(tr):
            issues.append(f"table {ti} row count: source {len(sr)} vs translation {len(tr)}")
            continue
        for ri, (src_row, trans_row) in enumerate(zip(sr, tr)):
            if len(src_row) != len(trans_row):
                issues.append(
                    f"table {ti} row {ri} column count: source {len(src_row)} vs translation {len(trans_row)}: {src_row!r} vs {trans_row!r}"
                )
    return {"check": "table_fidelity", "status": "fail" if issues else "pass", "issues": issues}


def _read_csv_skip_comments(path: Path) -> list[str]:
    """Strip # comment and empty lines before DictReader (matches check_glossary) — via translation_common."""
    if _USE_SHARED:
        return _shared_read_csv(path)
    text = path.read_text(encoding="utf-8")
    return [l for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]


def _load_person_names_for_qa(vault_root: Path | None) -> set[str]:
    """Load person names allowlist for residual_hebrew suppression.

    Codename-aware: org codenames that collide with Hebrew names are removed
    so they are not suppressed. Primary source is the domain glossary
    (data/domain_terms/glossary.json — term_he with status approved/keep_source);
    an optional data/person_names/codenames.txt is also read for manual overrides.
    """
    import csv

    names: set[str] = set()
    candidates: list[Path] = []
    codename_candidates: list[Path] = []
    glossary_candidates: list[Path] = []
    if vault_root is not None:
        candidates.append(vault_root / "data" / "person_names" / "first_names.txt")
        candidates.append(vault_root / "data" / "person_names" / "last_names_ranked.txt")
        codename_candidates.append(vault_root / "data" / "person_names" / "codenames.txt")
        glossary_candidates.append(vault_root / "data" / "domain_terms" / "glossary.json")
    else:
        cur = Path.cwd().resolve()
        for p in [cur] + list(cur.parents):
            candidates.append(p / "data" / "person_names" / "first_names.txt")
            candidates.append(p / "data" / "person_names" / "last_names_ranked.txt")
            codename_candidates.append(p / "data" / "person_names" / "codenames.txt")
            glossary_candidates.append(p / "data" / "domain_terms" / "glossary.json")
    for p in candidates:
        if p.exists():
            try:
                for line in p.read_text(encoding="utf-8").splitlines():
                    t = line.strip()
                    if t:
                        names.add(t)
            except OSError:
                pass
    # Remove codenames — glossary terms take precedence (domain dictionary)
    codenames: set[str] = set()
    for p in codename_candidates:
        if p.exists():
            try:
                for line in p.read_text(encoding="utf-8").splitlines():
                    t = line.strip()
                    if t and not t.lstrip().startswith("#"):
                        codenames.add(t)
            except OSError:
                pass
    # Also load glossary term_he for approved/keep_source — primary codename source
    for gp in glossary_candidates:
        if gp.exists():
            try:
                rows = json.loads(gp.read_text(encoding="utf-8"))
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    term = (row.get("term_he") or "").strip()
                    status = (row.get("status") or "").strip()
                    if term and status in ("approved", "keep_source"):
                        codenames.add(term)
            except (OSError, json.JSONDecodeError):
                pass
            break  # only first found glossary
    if codenames:
        names -= codenames
    return names


def _strip_frontmatter(text: str) -> tuple[str, str]:
    """Via translation_common (single source of truth)."""
    if _USE_SHARED:
        return _shared_strip_fm(text)
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
        translations = _filter_translations(row.get("translations"))
        status = (row.get("status") or "").strip()
        term_he = (row.get("term_he") or "").strip()
        if status not in ("approved", "keep_source"):
            continue
        if status == "keep_source":
            if term_he and term_he not in body:
                violations.append(f"keep_source missing: {term_he!r}")
        elif status == "approved":
            if not translations:
                continue
            if any(_count_option(body, opt) > 0 for opt in translations):
                continue
            if " " in term_he:
                tokens = term_he.split()
                per_word = all(f"⟦he:{tok}⟧" in body for tok in tokens)
                single = f"⟦he:{term_he}⟧" in body
                if per_word or single:
                    continue
            else:
                if f"⟦he:{term_he}⟧" in body:
                    continue
            violations.append(f"approved term {term_he!r} -> {translations!r} not found in translation")
    return {
        "check": "glossary_retention",
        "status": "fail" if violations else "pass",
        "violations": violations[:20],
    }


def verify_ordered(source_items: list[str], translation: str) -> list[str]:
    """Return items that are out of order (present but monotonic violation)."""
    positions: list[int | None] = []
    for item in source_items:
        try:
            positions.append(translation.index(item))
        except ValueError:
            positions.append(None)
    present = [(i, p) for i, p in enumerate(positions) if p is not None]
    out: list[str] = []
    for k in range(1, len(present)):
        if present[k][1] < present[k - 1][1]:
            out.append(source_items[present[k][0]])
    return out


def _count_option(body: str, option: str) -> int:
    """Count occurrences of option in body with simple word boundaries."""
    pat = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(option) + r"(?![A-Za-z0-9_])")
    return len(pat.findall(body))


def _first_pos_of_option(body: str, option: str) -> int | None:
    pat = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(option) + r"(?![A-Za-z0-9_])")
    m = pat.search(body)
    return m.start() if m else None


def check_glossary_translations(body: str, term_map: list[dict]) -> dict:
    """Plain-text glossary check: each term must use allowed translations with correct total.

    - For keep_source: term_he must appear in body.
    - For approved: any mix of allowed translations that sums to occurrences is valid; mixing is allowed.
    - Invalid options (parenthetical notes etc.) are discarded before checking.
    """
    violations: list[str] = []
    for e in term_map:
        if e.get("keep_source"):
            term_he = e.get("term_he", "") or ""
            if term_he and term_he not in body:
                violations.append(f"keep_source missing: {term_he!r}")
            continue
        translations = _filter_translations(e.get("translations"))
        if not translations:
            continue
        exp = int(e.get("occurrences", 1))
        counts: dict[str, int] = {}
        for opt in translations:
            counts[opt] = _count_option(body, opt)
        total = sum(counts.values())
        # Mixing allowed: valid iff total matches occurrences
        if total != exp:
            violations.append(f"{e.get('term_he')!r}→{translations!r} expected {exp}× got total {total}× counts={counts}")
            continue
    # Order check (only if no count violations)
    if not violations and len(term_map) > 1:
        positions: list[str] = []
        for e in sorted(term_map, key=lambda x: x.get("src_order", 0)):
            if e.get("keep_source"):
                target = e.get("term_he", "") or ""
                pos = body.find(target) if target else None
                if pos is not None and pos != -1:
                    positions.append(target)
                    continue
                positions.append(target)
                continue
            translations = _filter_translations(e.get("translations"))
            # Find which option was used (the one with count > 0)
            used = None
            for opt in translations:
                if _count_option(body, opt) > 0:
                    used = opt
                    break
            ordered_opt = used or (translations[0] if translations else "")
            positions.append(ordered_opt)
        oo = verify_ordered(positions, body)
        if oo:
            violations.append(f"out of order: {oo}")
    return {"check": "glossary_translations", "status": "fail" if violations else "pass", "violations": violations}


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


def check_preserved_invariants(source_body: str, trans_body: str, vault_root: Path | None = None) -> dict:
    """Verify names, English spans, URLs/paths, code and YAML from source appear verbatim and in order.

    Reuses translate.py extractors so QA and translation share one definition.
    Fail is quarantining — a dropped or reordered URL/name/code block must not reach the vault.
    """
    # Use raw source for global order (yaml at top, code blocks in place)
    raw_source = source_body
    # Direct import breaks cycle; translate re-export keeps compat
    try:
        import translation_invariants as tmod
    except ImportError:
        import translate as tmod  # type: ignore  # fallback via re-export

    try:
        if vault_root is not None:
            # Glossary-aware: codenames (domain terms) are excluded from PERSON guard
            # so a correctly translated codename does not trigger a false missing-person failure.
            try:
                glossary_path = vault_root / "data" / "domain_terms" / "glossary.json"
                glossary_terms: set[str] = set()
                if glossary_path.exists():
                    rows = json.loads(glossary_path.read_text(encoding="utf-8"))
                    for row in rows:
                        if not isinstance(row, dict):
                            continue
                        term = (row.get("term_he") or "").strip()
                        status = (row.get("status") or "").strip()
                        if term and status in ("approved", "keep_source"):
                            glossary_terms.add(term)
                first, last = tmod.load_person_names(vault_root, exclude=glossary_terms)
            except Exception:
                first, last = tmod.load_person_names(vault_root)
        else:
            first, last = set(), set()
        invariants = tmod.extract_preservation_invariants(raw_source, first, last)
        missing = tmod.verify_all_preserved(invariants, trans_body)
        order_bad = tmod.verify_all_ordered(invariants, trans_body)
        global_bad = tmod.verify_global_order(raw_source, invariants, trans_body)
    except Exception:
        # Fallback: lightweight technical-span extraction if translate import fails
        _tech_re = re.compile(
            r"\b(?:[A-Z]{2,}(?:\s+[A-Z][a-z]+){0,2}|[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3}"
            r"|[A-Za-z]+[-_/][A-Za-z0-9\-_./]+|[A-Z][a-z]*[A-Z][a-zA-Z]*"
            r"|[A-Za-z]+[0-9][A-Za-z0-9]*|[A-Z][a-z]{2,})\b"
        )
        _common = {"The","This","That","And","Or","But","With","From","For","To","Of","In","On","At","By","A","An","Is","Are","It","As"}
        en_spans = [s.strip().strip(".,;:\"'()") for s in _tech_re.findall(raw_source)]
        en_spans = [s for s in en_spans if len(s) >= 2 and len(re.findall(r"[A-Za-z]", s)) >= 2 and s not in _common]
        en_spans = list(dict.fromkeys(en_spans))
        url_spans = re.findall(r"https?://[^\s<>\[\]()\"']+|www\.[^\s<>\[\]()\"']+", raw_source)
        missing = {}
        for cat, items in [("english_spans", list(dict.fromkeys(en_spans))), ("urls_and_paths", list(dict.fromkeys(url_spans)))]:
            bad = [s for s in items if s not in trans_body]
            if bad:
                missing[cat] = bad[:10]
        invariants = {}
        order_bad = {}
        global_bad = []
    if missing or order_bad or global_bad:
        result: dict = {
            "check": "preserved_invariants",
            "status": "fail",
            "source_counts": {k: len(v) for k, v in invariants.items()} if invariants else {},
        }
        if missing:
            result["missing"] = {k: v[:10] for k, v in missing.items()}
        if order_bad:
            result["out_of_order"] = {k: v[:10] for k, v in order_bad.items()}
        if global_bad:
            result["global_out_of_order"] = global_bad[:10]
        return result
    return {
        "check": "preserved_invariants",
        "status": "pass",
        "source_counts": {k: len(v) for k, v in invariants.items()} if invariants else {},
    }


def run_all(source_path: Path | None, trans_body: str, trans_meta: dict, glossary: list[dict], vault_root: Path | None = None, term_map: list[dict] | None = None) -> list[dict]:
    # Resolve term_map from explicit arg or trans_meta (ledger)
    if term_map is None:
        term_map = trans_meta.get("term_map") if isinstance(trans_meta, dict) else None
    source_body = ""
    raw_source = ""
    if source_path and source_path.exists():
        try:
            raw_source = source_path.read_text(encoding="utf-8")
            _, source_body = _strip_frontmatter(raw_source)
            # For source, also strip frontmatter markers if any
        except OSError:
            pass

    checks: list[dict] = []
    checks.append(check_residual_hebrew(trans_body, vault_root=vault_root))
    checks.append(check_untranslated_block(trans_body))
    if term_map is not None:
        if term_map:
            checks.append(check_glossary_translations(trans_body, term_map))
        else:
            checks.append({"check": "glossary_translations", "status": "pass", "violations": []})
    else:
        checks.append(check_glossary_retention(trans_body, glossary))
    if source_body or raw_source:
        # Use stripped body for structural checks (yaml would pollute them)
        body_for_struct = source_body if source_body else raw_source
        checks.append(check_heading_fidelity(body_for_struct, trans_body))
        checks.append(check_structure_fidelity(body_for_struct, trans_body))
        checks.append(check_numeric_fidelity(body_for_struct, trans_body))
        checks.append(check_length_ratio(body_for_struct, trans_body))
        checks.append(check_table_fidelity(body_for_struct, trans_body))
        # Preserved invariants needs raw source to check yaml/frontmatter + code order
        checks.append(check_preserved_invariants(raw_source if raw_source else source_body, trans_body, vault_root=vault_root))
    else:
        # Still check length/markers even without source
        checks.append({"check": "heading_fidelity", "status": "skip", "note": "no source"})
        checks.append({"check": "structure_fidelity", "status": "skip", "note": "no source"})
        checks.append({"check": "numeric_fidelity", "status": "skip", "note": "no source"})
        checks.append({"check": "length_ratio", "status": "pass", "value": len(trans_body) / 1000, "note": "no source, skip ratio"})
        checks.append({"check": "table_fidelity", "status": "skip", "note": "no source"})
        checks.append({"check": "preserved_invariants", "status": "skip", "note": "no source"})
    checks.append(check_markup_integrity(trans_body))
    checks.append(check_marker_count(trans_body))
    return checks


# Alias for spec — run_all_checks is same as run_all
run_all_checks = run_all


def main(argv=None):
    ap = argparse.ArgumentParser(description="Deterministic QA gate for translations")
    ap.add_argument("store_dir", type=Path, help="data/translations root")
    ap.add_argument("--glossary", type=Path, default=None, help="glossary.json")
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
        if gp.suffix == ".json":
            try:
                glossary = json.loads(gp.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                glossary = []
        else:
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
