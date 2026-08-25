#!/usr/bin/env python3
"""Extract domain-specific terms (Hebrew + English + mixed like הAPI) from raw_md.

Deterministic, no deep learning.  Uses wordfreq for internet baselines (same
ratio as hot_words.py) and extends to bigrams/trigrams + mixed-token
normalization.

Mixed-token rule: Hebrew proclitic prefix (ה/ל/ב/מ/ו/ש/כ and combos like וה)
optionally hyphenated, attached to an English stem (e.g. הAPI -> api).  The
original surface is preserved in variants.json for correctness.

Outputs (under data/domain_terms/):
  terms.csv, variants.json, translation_seed.csv, code_words.txt/.csv,
  subdomain_keywords.json, report.json

All dependencies are required — script fails fast if wordfreq/YAP is missing.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Fail-fast dependency checks
# ---------------------------------------------------------------------------
try:
    import wordfreq  # noqa: F401
    from wordfreq import word_frequency
except ImportError:
    print("ERROR: wordfreq 3.1.1 required — pip install wordfreq==3.1.1", file=sys.stderr)
    sys.exit(1)

import hebrew_yap_stemmer as _hys  # noqa: E402 — needed for _strip_hb_suffix

# YAP — hard dependency, checked lazily in scan_corpus() so that importing
# this module for pure-logic tests (classify_token / RAW_WORD_RE) does not
# require the binary.  Import symbols without probing.
from hebrew_yap_stemmer import root_keys as _hb_root_keys  # noqa: E402
from hebrew_yap_stemmer import analyze_tokens as _hb_analyze  # noqa: E402
from hebrew_yap_stemmer import _find_yap_exe as _yap_find  # noqa: E402 — for runtime check

from hot_words import _ENGLISH_STOP_WORDS, _HB_STOP_WORDS  # noqa: E402

# sklearn is optional unless subdomain clustering requested
# TODO: Gate sklearn import only when subdomain clustering is requested via CLI
# flag (e.g. --cluster). Plan says "sklearn import gated only if subdomain
# clustering is requested; otherwise warn and skip." Currently imported at
# load to set _SKLEARN_AVAILABLE; defer to lazy import inside
# write_subdomain_keywords when clustering is actually needed.
_SKLEARN_AVAILABLE = False
try:
    import sklearn  # noqa: F401
    _SKLEARN_AVAILABLE = True
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# Narrow Hebrew range to א-ת (U+05D0..U+05EA) — covers all letters + final
# forms (ךםןףץ) while excluding cantillation/punctuation in 0590..05FF that
# would add noise via RAW_WORD_RE.  Original plan used ֐-׿; we tighten here.
HEBREW_RANGE = "א-ת"
HEBREW_CHAR_RE = re.compile(f"[{HEBREW_RANGE}]")

# Extraction: keeps optional hyphen so ה-API stays one token.
# Single-char proclitic + hyphen + English (ה-API) must be kept, so allow
# 1-char prefix before hyphen as alternative. Include digits/underscore for
# K8s, snake_case etc.
RAW_WORD_RE = re.compile(rf"(?:[A-Za-z0-9_{HEBREW_RANGE}]{{2,}}(?:-[A-Za-z0-9_{HEBREW_RANGE}]{{1,}})?|[A-Za-z0-9_{HEBREW_RANGE}]-[A-Za-z0-9_{HEBREW_RANGE}]{{1,}})")

# Proclitic letters that can attach to English stems
PROCLITICS = set("הלבמושכ")
# Mixed: leading Hebrew run + optional hyphen + English stem
MIXED_SPLIT_RE = re.compile(rf"^([{HEBREW_RANGE}]+)-?([A-Za-z][A-Za-z0-9_\-]*)")

# Code-word patterns
_RE_ACRONYM = re.compile(r"^[A-Z]{2,}$")
_RE_CAMEL = re.compile(r"^[a-z]+([A-Z][a-z]+)+$")
_RE_SNAKE = re.compile(r"^[a-z]+_[a-z_]+$")
_RE_KEBAB = re.compile(r"^[a-z]+-[a-z\-]+$")

OOV_FLOOR = 1e-9

# ---------------------------------------------------------------------------
# Token classification & normalization
# ---------------------------------------------------------------------------

def classify_token(tok: str) -> str:
    """Return 'he' | 'en' | 'mixed' for a raw token."""
    he = sum(1 for c in tok if HEBREW_CHAR_RE.match(c))
    en = sum(1 for c in tok if ("A" <= c <= "Z" or "a" <= c <= "z"))
    if he > 0 and en > 0:
        return "mixed"
    letters = [c for c in tok if c.isalpha()]
    if not letters:
        return "en"
    he_ratio = sum(1 for c in letters if HEBREW_CHAR_RE.match(c)) / len(letters)
    return "he" if he_ratio >= 0.6 else "en"


def normalize_en(tok: str) -> str:
    return tok.lower()


def normalize_mixed(tok: str) -> tuple[str, str]:
    """Return (normalized_en, he_prefix) for a mixed token.

    Strips only if prefix is exclusively proclitic letters and remainder is
    a valid English stem.  Otherwise returns (tok.lower(), '') so surface is
    preserved without corrupting.
    """
    m = MIXED_SPLIT_RE.match(tok)
    if not m:
        return tok.lower(), ""
    he_prefix, en_stem = m.group(1), m.group(2)
    if len(en_stem) < 2:
        return tok.lower(), ""
    # Validate prefix chars are all proclitics
    if any(c not in PROCLITICS for c in he_prefix):
        return tok.lower(), ""
    return en_stem.lower(), he_prefix


# ---------------------------------------------------------------------------
# Corpus helpers
# ---------------------------------------------------------------------------

def resolve_corpus_dir(vault_root: Path, explicit: Path | None = None) -> Path:
    if explicit is not None:
        p = Path(explicit)
        if not p.is_dir():
            print(f"ERROR: explicit input dir not found: {p}", file=sys.stderr)
            sys.exit(1)
        return p
    raw_md = vault_root / "raw_md"
    raw = vault_root / "raw"
    if raw_md.is_dir() and any(raw_md.rglob("*.md")):
        return raw_md
    if raw.is_dir() and any(raw.rglob("*.md")):
        return raw
    print(f"ERROR: neither raw_md/ nor raw/ with *.md found under {vault_root}", file=sys.stderr)
    sys.exit(1)


def _strip_frontmatter(text: str) -> str:
    lines = text.split("\n")
    body_lines: list[str] = []
    in_fm = False
    for line in lines:
        if line.strip() == "---":
            in_fm = not in_fm
            continue
        if not in_fm:
            body_lines.append(line)
    return "\n".join(body_lines)


def _clean_body(body: str) -> str:
    body = re.sub(r"!\[[^]]*\]\([^)]*\)", "", body)
    body = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", body)
    body = re.sub(r"<[^>]+>", " ", body)
    body = re.sub(r"[^\s]+@[^\s]+", "", body)
    return body


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

def scan_corpus(corpus_dir: Path):
    """Scan corpus_dir recursively, return counts + metadata.

    Returns dict with:
      unigram_counts, bigram_counts, trigram_counts, variant_map,
      he_prefix_map, doc_freq, bigram_doc_freq, trigram_doc_freq,
      doc_terms, doc_term_counts, doc_names, backtick_terms, file_count,
      total_chars, input_dir
    """
    md_files = sorted(corpus_dir.rglob("*.md"))
    if not md_files:
        print(f"ERROR: no markdown files in {corpus_dir}", file=sys.stderr)
        sys.exit(1)

    unigram_counts: Counter = Counter()
    bigram_counts: Counter = Counter()
    trigram_counts: Counter = Counter()
    variant_map: dict[str, Counter] = defaultdict(Counter)
    he_prefix_map: dict[str, Counter] = defaultdict(Counter)
    doc_freq: Counter = Counter()
    bigram_doc_freq: Counter = Counter()
    trigram_doc_freq: Counter = Counter()
    doc_terms: list[set[str]] = []
    doc_term_counts: list[Counter] = []
    doc_names: list[str] = []
    backtick_terms: set[str] = set()

    # Phase 1: collect per-doc streams with Hebrew placeholders and global heb set
    total_chars = 0
    per_doc: list[dict] = []
    all_he_surfaces: set[str] = set()

    for md_file in md_files:
        try:
            rel = str(md_file.relative_to(corpus_dir))
        except ValueError:
            rel = md_file.name
        text = md_file.read_text(encoding="utf-8")
        body = _clean_body(_strip_frontmatter(text))
        total_chars += len(body)

        bt_raw = re.findall(r"`([^`]+)`", body)
        bt_norms: set[str] = set()
        for bt in bt_raw:
            for w in re.findall(r"[A-Za-z0-9_]{2,}", bt):
                bt_norms.add(w.lower())

        raw_tokens = RAW_WORD_RE.findall(body)
        if not raw_tokens:
            per_doc.append({"full_stream": [], "bt_norms": bt_norms, "rel": rel,
                            "he_surfaces": [], "he_positions": [], "he_by_pos": {}})
            continue

        full_stream: list[str | None] = []
        he_surfaces: list[str] = []
        he_positions: list[int] = []
        he_by_pos: dict[int, str] = {}
        for tok in raw_tokens:
            cls = classify_token(tok)
            if cls == "en":
                norm = normalize_en(tok)
                if norm in _ENGLISH_STOP_WORDS or len(norm) <= 1 or norm.isdigit():
                    full_stream.append(None)
                else:
                    full_stream.append(norm)
                    variant_map[norm][tok] += 1
            elif cls == "mixed":
                norm, prefix = normalize_mixed(tok)
                if prefix:
                    if norm in _ENGLISH_STOP_WORDS or len(norm) <= 1:
                        full_stream.append(None)
                    else:
                        full_stream.append(norm)
                        variant_map[norm][tok] += 1
                        he_prefix_map[norm][prefix] += 1
                else:
                    if len(norm) <= 1:
                        full_stream.append(None)
                    else:
                        full_stream.append(norm)
                        variant_map[norm][tok] += 1
            else:  # he
                if tok in _HB_STOP_WORDS or len(tok) <= 1:
                    full_stream.append(None)
                else:
                    pos = len(full_stream)
                    full_stream.append(None)  # placeholder
                    he_surfaces.append(tok)
                    he_positions.append(pos)
                    he_by_pos[pos] = tok
                    all_he_surfaces.add(tok)

        per_doc.append({"full_stream": full_stream, "bt_norms": bt_norms, "rel": rel,
                        "he_surfaces": he_surfaces, "he_positions": he_positions, "he_by_pos": he_by_pos})

    # Phase 2: global YAP batch (1 subprocess for all Hebrew surfaces, ~6k docs → 1 call)
    global_per_surface_norm: dict[str, str] = {}
    if all_he_surfaces:
        try:
            _yap_find()
        except FileNotFoundError as _e:
            print(f"ERROR: YAP binary missing — {_e}", file=sys.stderr)
            sys.exit(1)
        unique_list = list(all_he_surfaces)
        # Chunk to avoid timeout on huge corpora (500 tokens per chunk, 30s timeout each)
        chunk_size = 500
        pairs_all: list[tuple[str, str]] = []
        try:
            for i in range(0, len(unique_list), chunk_size):
                chunk = unique_list[i:i + chunk_size]
                pairs_all.extend(_hb_analyze(chunk))
        except Exception as e:
            print(f"ERROR: YAP analysis failed: {e}", file=sys.stderr)
            sys.exit(1)
        lemma_by_surface: dict[str, str] = {}
        for orig, lemma in pairs_all:
            # keep first occurrence for deduped list
            if orig not in lemma_by_surface:
                lemma_by_surface[orig] = lemma
        # also cover any surface not returned (fallback)
        for surf in unique_list:
            lemma = lemma_by_surface.get(surf, surf)
            # (2) YAP-aware definite-article strip: only for longer lemmas
            # (len>4) to avoid false merge of בין (between, score 3.6) and
            # הבין (understood, 0.46) — both would map to בין with naive strip.
            # Longer terms like המדינה→מדינה, הסייבר→סייבר, הממשלה→ממשלה
            # are safe (754 terms, ~24% of glossary_3223).
            lemma_for_norm = lemma
            if lemma.startswith("ה") and len(lemma) > 4:
                cand = lemma[1:]
                if cand and "א" <= cand[0] <= "ת":
                    def _strong(s: str) -> str:
                        red = _hys._strip_hb_suffix(s)
                        weak = {"א", "ה", "ו", "י"}
                        st = [c for c in red if "א" <= c <= "ת" and c not in weak]
                        return "".join(st[:3]) if len(st) >= 3 else red
                    if _strong(cand) == _strong(lemma):
                        lemma_for_norm = cand
            reduced = _hys._strip_hb_suffix(lemma_for_norm)
            weak = {"א", "ה", "ו", "י"}
            strong = [c for c in reduced if "א" <= c <= "ת" and c not in weak]
            if len(strong) >= 3:
                global_per_surface_norm[surf] = "".join(strong[:3])
            else:
                global_per_surface_norm[surf] = reduced

    # Phase 3: resolve Hebrew placeholders and count n-grams per doc
    for doc in per_doc:
        full_stream = doc["full_stream"]
        bt_norms = doc["bt_norms"]
        rel = doc["rel"]
        he_positions = doc["he_positions"]
        he_by_pos = doc["he_by_pos"]
        if he_positions:
            # Use global mapping; if global batch had no entry (e.g. YAP failed), fallback to surface
            for pos in he_positions:
                surf = he_by_pos[pos]
                norm = global_per_surface_norm.get(surf, surf)
                if norm in _HB_STOP_WORDS or len(norm) <= 1:
                    full_stream[pos] = None
                else:
                    full_stream[pos] = norm
                    variant_map[norm][surf] += 1
        # Fallback if YAP warned per surface: already handled via global_per_surface_norm

        normalized_stream = [t for t in full_stream if t is not None]
        seen_in_doc: set[str] = set()
        doc_counter: Counter = Counter()
        for norm in normalized_stream:
            unigram_counts[norm] += 1
            doc_counter[norm] += 1
            seen_in_doc.add(norm)
            if norm in bt_norms:
                backtick_terms.add(norm)
        for norm in seen_in_doc:
            doc_freq[norm] += 1
        doc_terms.append(seen_in_doc)
        doc_term_counts.append(doc_counter)
        doc_names.append(rel)

        seen_bigrams: set[str] = set()
        for i in range(len(full_stream) - 1):
            a, b = full_stream[i], full_stream[i + 1]
            if a is None or b is None:
                continue
            bg = f"{a} {b}"
            bigram_counts[bg] += 1
            seen_bigrams.add(bg)
        for bg in seen_bigrams:
            bigram_doc_freq[bg] += 1

        seen_trigrams: set[str] = set()
        for i in range(len(full_stream) - 2):
            a, b, c = full_stream[i], full_stream[i + 1], full_stream[i + 2]
            if a is None or b is None or c is None:
                continue
            tg = f"{a} {b} {c}"
            trigram_counts[tg] += 1
            seen_trigrams.add(tg)
        for tg in seen_trigrams:
            trigram_doc_freq[tg] += 1

    return {
        "unigram_counts": unigram_counts,
        "bigram_counts": bigram_counts,
        "trigram_counts": trigram_counts,
        "variant_map": variant_map,
        "he_prefix_map": he_prefix_map,
        "doc_freq": doc_freq,
        "bigram_doc_freq": bigram_doc_freq,
        "trigram_doc_freq": trigram_doc_freq,
        "doc_terms": doc_terms,
        "doc_term_counts": doc_term_counts,
        "doc_names": doc_names,
        "backtick_terms": backtick_terms,
        "file_count": len(md_files),
        "total_chars": total_chars,
        "input_dir": str(corpus_dir),
    }


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _base_freq(term: str, lang: str) -> float:
    """wordfreq baseline for a single term, with OOV floor."""
    try:
        f = word_frequency(term, lang)
    except (KeyError, ValueError):
        f = 0.0
    return f if f > 0 else OOV_FLOOR


def base_ngram_freq(terms: list[str], langs: list[str]) -> float:
    """Geometric mean of constituent wordfreq baselines."""
    freqs: list[float] = []
    for w, lang in zip(terms, langs):
        eff_lang = "en" if lang == "mixed" else lang
        # For n-gram constituents that are Hebrew root keys, wordfreq with 'he'
        # may be valid; try he first, fall back to floor
        f = _base_freq(w, eff_lang)
        freqs.append(f)
    # geometric mean
    log_sum = sum(math.log(f) for f in freqs)
    return math.exp(log_sum / len(freqs))


def _detect_lang(term: str, variant_map, he_prefix_map) -> str:
    """Detect lang for a normalized term (unigram) via variant evidence."""
    if term in he_prefix_map and he_prefix_map[term]:
        return "mixed"
    # Check if any variant contains Hebrew
    for surf in variant_map.get(term, {}):
        if HEBREW_CHAR_RE.search(surf):
            # If term itself is Hebrew chars -> he, else mixed
            if HEBREW_CHAR_RE.search(term):
                return "he"
            return "mixed"
    if HEBREW_CHAR_RE.search(term):
        return "he"
    return "en"


def _ngram_lang(ngram: str, variant_map, he_prefix_map) -> str:
    parts = ngram.split()
    langs = [_detect_lang(p, variant_map, he_prefix_map) for p in parts]
    if len(set(langs)) == 1:
        return langs[0]
    if "mixed" in langs:
        return "mixed"
    return "multi"


def score_terms(scan: dict, top_n: int = 1000, min_count_uni: int = 3,
                min_count_bi: int = 2, min_count_tri: int = 2):
    scored: list[dict] = []

    variant_map = scan["variant_map"]
    he_prefix_map = scan["he_prefix_map"]
    doc_freq = scan["doc_freq"]
    bigram_doc_freq = scan.get("bigram_doc_freq", Counter())
    trigram_doc_freq = scan.get("trigram_doc_freq", Counter())

    # Unigrams
    for term, cnt in scan["unigram_counts"].items():
        if cnt < min_count_uni:
            continue
        lang = _detect_lang(term, variant_map, he_prefix_map)
        eff_lang = "en" if lang == "mixed" else lang
        base = _base_freq(term, eff_lang)
        ratio = cnt / base
        log_ratio = math.log10(ratio) if ratio > 0 else 0
        scored.append({
            "term": term, "n": 1, "lang": lang, "corpus_count": cnt,
            "doc_freq": doc_freq.get(term, 0), "base_freq": base,
            "ratio": ratio, "log_ratio": log_ratio,
        })

    # Bigrams
    for term, cnt in scan["bigram_counts"].items():
        if cnt < min_count_bi:
            continue
        parts = term.split()
        langs = [_detect_lang(p, variant_map, he_prefix_map) for p in parts]
        base = base_ngram_freq(parts, langs)
        ratio = cnt / base
        log_ratio = math.log10(ratio) if ratio > 0 else 0
        scored.append({
            "term": term, "n": 2, "lang": _ngram_lang(term, variant_map, he_prefix_map),
            "corpus_count": cnt, "doc_freq": bigram_doc_freq.get(term, 0), "base_freq": base,
            "ratio": ratio, "log_ratio": log_ratio,
        })

    # Trigrams
    for term, cnt in scan["trigram_counts"].items():
        if cnt < min_count_tri:
            continue
        parts = term.split()
        langs = [_detect_lang(p, variant_map, he_prefix_map) for p in parts]
        base = base_ngram_freq(parts, langs)
        ratio = cnt / base
        log_ratio = math.log10(ratio) if ratio > 0 else 0
        scored.append({
            "term": term, "n": 3, "lang": _ngram_lang(term, variant_map, he_prefix_map),
            "corpus_count": cnt, "doc_freq": trigram_doc_freq.get(term, 0), "base_freq": base,
            "ratio": ratio, "log_ratio": log_ratio,
        })

    scored.sort(key=lambda x: (-x["log_ratio"], -x["corpus_count"], x["term"]))
    return scored[:top_n]


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

def write_terms_csv(scored: list[dict], variant_map, path: Path):
    fieldnames = ["rank", "term", "n", "lang", "corpus_count", "doc_freq", "base_freq", "ratio", "log_ratio", "variants"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for i, row in enumerate(scored, 1):
            if row["n"] == 1:
                variants = ";".join(sorted(variant_map.get(row["term"], {}).keys()))
            else:
                variants = ""
            w.writerow({
                "rank": i, "term": row["term"], "n": row["n"], "lang": row["lang"],
                "corpus_count": row["corpus_count"], "doc_freq": row["doc_freq"],
                "base_freq": row["base_freq"], "ratio": row["ratio"], "log_ratio": row["log_ratio"],
                "variants": variants,
            })


def write_variants_json(scored: list[dict], variant_map, he_prefix_map, path: Path):
    out: dict = {}
    for row in scored:
        term = row["term"]
        if row["n"] != 1:
            continue
        variants = variant_map.get(term, {})
        if not variants:
            continue
        entry: dict = {
            "normalized": term,
            "lang": row["lang"],
            "corpus_count": row["corpus_count"],
            "variants": dict(variants),
        }
        if term in he_prefix_map and he_prefix_map[term]:
            entry["he_prefixes"] = dict(he_prefix_map[term])
        out[term] = entry
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)


def write_translation_seed(scored: list[dict], variant_map, scan: dict, path: Path):
    fieldnames = ["term", "lang", "surface_variants", "corpus_count", "log_ratio", "needs_translation", "suggested_en", "example_doc"]
    # Build example_doc map: first doc containing term
    term_to_doc: dict[str, str] = {}
    for term in [r["term"] for r in scored if r["n"] == 1 and r["lang"] in ("he", "mixed")]:
        for idx, doc_set in enumerate(scan["doc_terms"]):
            if term in doc_set:
                term_to_doc[term] = scan["doc_names"][idx]
                break
    rows = []
    for row in scored:
        if row["n"] != 1 or row["lang"] not in ("he", "mixed"):
            continue
        term = row["term"]
        variants = ";".join(sorted(variant_map.get(term, {}).keys()))
        suggested = term if row["lang"] == "mixed" else ""
        rows.append({
            "term": term, "lang": row["lang"], "surface_variants": variants,
            "corpus_count": row["corpus_count"], "log_ratio": row["log_ratio"],
            "needs_translation": "true", "suggested_en": suggested,
            "example_doc": term_to_doc.get(term, ""),
        })
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def write_code_words(scored: list[dict], variant_map, backtick_terms: set[str], out_txt: Path, out_csv: Path):
    code_rows: list[dict] = []
    for row in scored:
        if row["n"] != 1:
            continue
        term = row["term"]
        lang = row["lang"]
        base = row["base_freq"]
        cnt = row["corpus_count"]
        matched = ""
        is_code = False
        if lang == "mixed":
            is_code = True
            matched = "mixed"
        elif base < 1e-7 and cnt >= 3:
            is_code = True
            matched = "rare"
        elif _RE_ACRONYM.match(term):
            is_code = True
            matched = "acronym"
        elif _RE_CAMEL.match(term):
            is_code = True
            matched = "camelCase"
        elif _RE_SNAKE.match(term):
            is_code = True
            matched = "snake"
        elif _RE_KEBAB.match(term):
            is_code = True
            matched = "kebab"
        elif term in backtick_terms:
            is_code = True
            matched = "backtick"
        if is_code:
            example = next(iter(sorted(variant_map.get(term, {}).keys())), term)
            code_rows.append({
                "term": term, "corpus_count": cnt, "log_ratio": row["log_ratio"],
                "pattern_matched": matched, "example_surface": example,
            })
    code_rows.sort(key=lambda x: -x["log_ratio"])
    with open(out_txt, "w", encoding="utf-8") as f:
        for r in code_rows:
            f.write(r["term"] + "\n")
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["term", "corpus_count", "log_ratio", "pattern_matched", "example_surface"])
        w.writeheader()
        w.writerows(code_rows)


def write_subdomain_keywords(scored: list[dict], scan: dict, path: Path):
    top_terms = [r["term"] for r in scored if r["n"] == 1][:500]
    if not top_terms or len(scan["doc_terms"]) < 2:
        result = {
            "num_clusters": 0,
            "clusters": [],
            "unclustered_keywords": top_terms[:20],
            "note": "not enough docs for clustering",
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        return

    if not _SKLEARN_AVAILABLE:
        result = {
            "num_clusters": 0,
            "clusters": [],
            "unclustered_keywords": top_terms[:50],
            "note": "sklearn not available — quartile fallback",
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        return

    from sklearn.feature_extraction.text import TfidfTransformer
    import numpy as _np

    n_docs = len(scan["doc_terms"])
    n_terms = len(top_terms)
    term_idx = {t: i for i, t in enumerate(top_terms)}
    mat = _np.zeros((n_docs, n_terms), dtype=float)
    doc_term_counts = scan.get("doc_term_counts", [Counter() for _ in range(n_docs)])
    for doc_i, counter in enumerate(doc_term_counts):
        for t, c in counter.items():
            if t in term_idx:
                mat[doc_i, term_idx[t]] = float(c)

    transformer = TfidfTransformer()
    tfidf = transformer.fit_transform(mat).toarray()

    from sklearn.cluster import KMeans
    n_clusters = min(5, n_docs)
    if n_clusters < 2:
        n_clusters = 2 if n_docs >= 2 else 1
    kmeans = KMeans(n_clusters=n_clusters, random_state=0, n_init=1)
    labels = kmeans.fit_predict(tfidf)

    clusters = []
    for cid in range(n_clusters):
        doc_indices = [i for i, l in enumerate(labels) if l == cid]
        mean_tfidf = tfidf[doc_indices].mean(axis=0) if doc_indices else _np.zeros(n_terms)
        top_idx = _np.argsort(mean_tfidf)[::-1][:8]
        keywords = [top_terms[i] for i in top_idx if mean_tfidf[i] > 0][:8]
        docs = [scan["doc_names"][i] for i in doc_indices]
        label_hint = "-".join(keywords[:3]) if keywords else f"cluster-{cid}"
        clusters.append({
            "id": f"subdomain_{cid}",
            "label_hint": label_hint,
            "keywords": keywords,
            "doc_count": len(doc_indices),
            "docs": docs,
        })

    # Unclustered: terms not in any cluster top keywords
    clustered_terms = {k for c in clusters for k in c["keywords"]}
    unclustered = [t for t in top_terms if t not in clustered_terms][:20]

    result = {"num_clusters": n_clusters, "clusters": clusters, "unclustered_keywords": unclustered}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Extract domain terms (he/en/mixed) from raw_md")
    parser.add_argument("vault_root", nargs="?", default=".", help="Vault root (contains raw/ and raw_md/)")
    parser.add_argument("--input", dest="input_dir", default=None, help="Explicit corpus dir override")
    parser.add_argument("--output-dir", default=None, help="Output dir (default: <vault>/data/domain_terms)")
    parser.add_argument("--top-n", type=int, default=1000)
    parser.add_argument("--min-count", type=int, default=3, help="Unigram min count (bigram/trigram use 2)")
    parser.add_argument("--min-count-bi", type=int, default=2, help="Bigram min count")
    parser.add_argument("--min-count-tri", type=int, default=2, help="Trigram min count")
    parser.add_argument("--ngrams", default="1,2,3", help="Comma-separated n values, e.g. 1,2")
    parser.add_argument("--quota", default=None, help="Per-n quota e.g. 700,200,100 for unigram,bigram,trigram")
    args = parser.parse_args()

    vault_root = Path(args.vault_root).resolve()
    corpus_dir = resolve_corpus_dir(vault_root, Path(args.input_dir) if args.input_dir else None)

    output_dir = Path(args.output_dir) if args.output_dir else vault_root / "data" / "domain_terms"
    output_dir.mkdir(parents=True, exist_ok=True)

    n_vals = {int(x.strip()) for x in args.ngrams.split(",") if x.strip()}
    print(f"=== Scanning corpus: {corpus_dir} ===")
    scan = scan_corpus(corpus_dir)
    print(f"Processed {scan['file_count']} markdown files ({scan['total_chars']:,} chars)")
    print(f"Unique unigrams: {len(scan['unigram_counts'])}  bigrams: {len(scan['bigram_counts'])}  trigrams: {len(scan['trigram_counts'])}")

    if not scan["unigram_counts"]:
        print("No terms found after filtering.", file=sys.stderr)
        sys.exit(1)

    print("\n=== Scoring ===")
    scored = score_terms(scan, top_n=args.top_n * 3, min_count_uni=args.min_count,
                         min_count_bi=args.min_count_bi, min_count_tri=args.min_count_tri)
    # Filter by requested n
    scored = [r for r in scored if r["n"] in n_vals]
    # Apply per-n quota if requested (e.g. --quota 700,200,100)
    if args.quota:
        try:
            quotas = [int(x.strip()) for x in args.quota.split(",")]
            quota_map = {1: quotas[0] if len(quotas) > 0 else args.top_n,
                         2: quotas[1] if len(quotas) > 1 else args.top_n,
                         3: quotas[2] if len(quotas) > 2 else args.top_n}
            by_n: dict[int, list[dict]] = {1: [], 2: [], 3: []}
            for r in scored:
                by_n[r["n"]].append(r)
            quota_scored = []
            for n in sorted(n_vals):
                quota_scored.extend(by_n[n][:quota_map.get(n, args.top_n)])
            quota_scored.sort(key=lambda x: (-x["log_ratio"], -x["corpus_count"], x["term"]))
            scored = quota_scored[:args.top_n]
        except Exception as e:
            print(f"[WARN] invalid --quota {args.quota}: {e}", file=sys.stderr)
    scored = scored[:args.top_n]
    print(f"Scored {len(scored)} terms (top {min(len(scored), 10)} shown):")
    for i, r in enumerate(scored[:10], 1):
        print(f"  {i:>3} {r['term']:<30} n={r['n']} {r['lang']:>6} cnt={r['corpus_count']:>4} log_ratio={r['log_ratio']:.2f}")

    # Write outputs
    write_terms_csv(scored, scan["variant_map"], output_dir / "terms.csv")
    write_variants_json(scored, scan["variant_map"], scan["he_prefix_map"], output_dir / "variants.json")
    write_translation_seed(scored, scan["variant_map"], scan, output_dir / "translation_seed.csv")
    write_code_words(scored, scan["variant_map"], scan["backtick_terms"], output_dir / "code_words.txt", output_dir / "code_words.csv")
    write_subdomain_keywords(scored, scan, output_dir / "subdomain_keywords.json")

    # Report
    ngram_counts = {
        "unigram": len(scan["unigram_counts"]),
        "bigram": len(scan["bigram_counts"]),
        "trigram": len(scan["trigram_counts"]),
    }
    # Report schema aligns with plan: input_dir, files, total_chars,
    # total_tokens, unique_terms, ngram_counts, warnings, errors + extras
    report = {
        "input_dir": scan["input_dir"],
        "files": scan["file_count"],
        "file_count": scan["file_count"],
        "total_chars": scan["total_chars"],
        "total_tokens": sum(scan["unigram_counts"].values()),
        "unique_terms": len(scan["unigram_counts"]),
        "ngram_counts": ngram_counts,
        "top_n": args.top_n,
        "min_count": args.min_count,
        "min_count_bi": args.min_count_bi,
        "min_count_tri": args.min_count_tri,
        "ngrams": sorted(n_vals),
        "scored_count": len(scored),
        "warnings": [],
        "errors": [],
        "sklearn_version": None,
    }
    try:
        import sklearn
        report["sklearn_version"] = sklearn.__version__
    except ImportError:
        pass
    if ngram_counts["trigram"] < 10:
        report["warnings"].append(f"only {ngram_counts['trigram']} trigrams — corpus may be small")
    with open(output_dir / "report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"\nOutputs written to: {output_dir}")
    print(f"  terms.csv, variants.json, translation_seed.csv, code_words.txt/.csv, subdomain_keywords.json, report.json")


if __name__ == "__main__":
    main()
