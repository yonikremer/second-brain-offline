#!/usr/bin/env python3
"""Hebrew morphological analysis via YAP (Yet Another Parser).

Uses YAP's ``hebma`` subcommand for lexicon-based morphological disambiguation.
Returns lemma forms that collapse inflected / prefixed variants into shared stems.

This is a lightweight alternative to rule-based suffix stripping.  It requires
the YAP Go binary installed alongside this script.

Usage:
    python scripts/hebrew_yap_stemmer.py "שלומך שלומית שלי רצונם"
    # -> {'שלום', 'רצון'}   (one per unique lemma)
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# YAP binary discovery
# ---------------------------------------------------------------------------

def _find_yap_exe() -> str:
    """Find the compiled YAP executable.

    Search order:
      1. ``YAP_DIR/yap.exe`` (or ``yap`` on Unix-like) in config
      2. Directory next to this script file
      3. ``$PATH``

    Raises FileNotFoundError if not found.
    """
    yap_dir = os.environ.get("YAP_DIR")
    if yap_dir:
        exe = os.path.join(yap_dir, "yap.exe")
        if os.path.isfile(exe):
            return exe

    # Next to this module
    script_dir = os.path.dirname(os.path.abspath(__file__))
    parent = os.path.dirname(script_dir)  # project root
    for candidate in [
        os.path.join(parent, "deps", "yap", "yap.exe"),
        os.path.join(parent, "yap", "yap.exe"),
        "yap.exe",          # assume in $PATH
    ]:
        if os.path.isfile(candidate):
            return candidate

    raise FileNotFoundError(
        "YAP binary not found. Set YAP_DIR env var or place \"yap\" "
        "in deps/yap/ next to this repo."
    )


YAP_EXE: str | None = None


def _get_yap_exe() -> str:
    global YAP_EXE
    if YAP_EXE is None:
        YAP_EXE = _find_yap_exe()
    return YAP_EXE


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------

def analyze_tokens(words: list[str]) -> list[tuple[str, str]]:
    """Run YAP hebma on a list of Hebrew tokens and return (word, lemma) pairs.

    For each input token we get ALL possible analyses from the lattice.
    Returns a list of ``(original_word, chosen_lemma)`` where the first
    (most likely) lemma is selected per token.

    If YAP returns no analysis for a token the original word is returned
    unchanged.
    """
    if not words:
        return []

    # Prepare input file: one token per line, blank line between sentences,
    # last line MUST be empty.
    # Treat the entire input as one "sentence" for simplicity.
    content = "\n".join(words) + "\n\n"

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", encoding="utf-8", delete=False
    ) as f:
        f.write(content)
        tmp_input = f.name

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".lattice", encoding="utf-8", delete=False
    ) as f:
        tmp_lattice = f.name

    try:
        yap_exe = _get_yap_exe()
        result = subprocess.run(
            [yap_exe, "hebma", "-raw", tmp_input, "-out", tmp_lattice],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"YAP hebma failed (exit {result.returncode}): {result.stderr[:500].strip()}"
            )

        lattice_text = Path(tmp_lattice).read_text(encoding="utf-8")
        return _parse_lattice(lattice_text, words)
    except FileNotFoundError:
        raise
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"YAP hebma timeout after {exc.timeout}s") from exc
    finally:
        try:
            os.unlink(tmp_input)
            os.unlink(tmp_lattice)
        except OSError:
            pass


def _is_short_spurious(lemma: str) -> bool:
    """Return True if lemma looks like a short spurious extraction (< 3 chars).

    Hebrew triconsonantal roots are typically 3+ characters. Single-character
    lemmas from hebma often represent prefix particles (ו/ב/כ/ל/ש) extracted
    incorrectly rather than genuine root forms.
    """
    hebrew_chars = sum(1 for c in lemma if 'א' <= c <= 'ת')
    return hebrew_chars < 3


def _pick_best_lemma(analyses: list[dict], original: str) -> str:
    """Choose the best lemma from multiple YAP analyses.

    Heuristic preference (in order):
      1. Original form itself (if it appears as an analysis — preserves spelling)
      2. Longest lemma matching/truncating to ≥ 3 Hebrew chars
      3. First analysis if nothing better found
    """
    if not analyses:
        return original

    # Collect valid options (skip single-char / 2-char spuriae)
    valid = [a for a in analyses if not _is_short_spurious(a["lemma"])]

    # Prefer original form when it's a valid analysis (preserves proper nouns)
    for a in valid:
        if a["lemma"] == original:
            return original

    # Otherwise pick longest valid lemma
    if valid:
        return max(valid, key=lambda a: sum(1 for c in a["lemma"] if 'א' <= c <= 'ת'))["lemma"]

    # Fallback: first analysis even if short
    return analyses[0]["lemma"]


def _parse_lattice(lattice_text: str, expected_words: list[str]) -> list[tuple[str, str]]:
    """Parse YAP lattice output into (word, best_lemma) pairs.

    Lattice format per edge:
      FROM  TO  FORM  LEMMA  CPOSTAG  POSTAG  FEATURES  TOKEN_ID

    Each token may appear multiple times with different analyses.
    We pick the best lemma per token using heuristics.
    """
    edges_by_token: dict[int, list[dict]] = {}
    current_token_id: Optional[int] = None

    for line in lattice_text.splitlines():
        parts = line.strip().split("\t")
        if len(parts) < 7:
            continue

        try:
            token_id = int(parts[-1])
        except ValueError:
            continue

        if current_token_id != token_id:
            current_token_id = token_id
            edges_by_token[token_id] = []

        edges_by_token[token_id].append({
            "form": parts[2],
            "lemma": parts[3],
            "postag": parts[5],
        })

    # Build results preserving input order
    results = []
    word_idx = 0
    for tid in sorted(edges_by_token.keys()):
        analyses = edges_by_token[tid]
        if word_idx < len(expected_words):
            orig = expected_words[word_idx]
            lemma = _pick_best_lemma(analyses, orig)
            results.append((orig, lemma))
            word_idx += 1

    # Fill in any missing tokens that didn't appear in lattice
    while word_idx < len(expected_words):
        results.append((expected_words[word_idx], expected_words[word_idx]))
        word_idx += 1

    return results


def get_lemmas(words: list[str]) -> set[str]:
    """Return unique lemma forms from YAP's lexicon analysis.

    This is the default level: inflected variants may still appear as
    distinct strings (e.g. רוצה vs רצון), but prefixed prepositions
    (ב/כ/ל/ו/ש) are already stripped by YAP.
    """
    if not words:
        return set()
    pairs = analyze_tokens(words)
    return {lemma for _, lemma in pairs}


def root_keys(words: list[str], min_root_chars: int = 3) -> set[str]:
    """Return root-like fingerprints for grouping deeply inflected Hebrew words.

    Pipeline:
      1. YAP hebma → lemma forms (lexicon-based morphological analysis)
      2. One layer of suffix stripping (plurals, possessives, past tense)
      3. Consonant skeleton extraction (keep strong consonants only)
      4. Truncate to ``min_root_chars`` strong consonants

    This merges words like שמירה↔שומרים↔שימרת→שמר (root ש-מ-ר).

    Note: In unpointed Hebrew (no niqqud), letters א/ה/ו/י serve dual roles
    as consonants or vowel carriers, so this heuristic may over-group some
    homographic but unrelated roots. It works well for high-frequency text.
    """
    if not words:
        return set()
    pairs = analyze_tokens(words)
    stems = {lemma for _, lemma in pairs}
    results = set()
    for stem in stems:
        reduced = _strip_hb_suffix(stem)
        weak = {'א', 'ה', 'ו', 'י'}
        strong = [c for c in reduced if 'א' <= c <= 'ת' and c not in weak]
        if len(strong) >= min_root_chars:
            results.add(''.join(strong[:min_root_chars]))
        else:
            results.add(reduced)
    return results


# ---------------------------------------------------------------------------
# Lightweight suffix stripping (works on lemma output from YAP)
# ---------------------------------------------------------------------------

def _strip_hb_suffix(word: str) -> str:
    """Strip one layer of Hebrew suffixes from an already-lemmatized word.

    This handles clearly predictable endings after YAP's lexicon lookup:
      - Plurals: ים / ות / ית / יום
      - Possessives: י(י) / ך(ך) / ה / נו / ךם / ךן
      - Past-tense fused markers: ת(ת) / תם(תם)
    """
    w = word
    rules = [
        (r'ינו$', 2),   (r'כם$', 2),   (r'כן$', 2),
        (r'יים$', 2),   (r'יות$', 2),  (r'ותם$', 3),
        (r'ותן$', 3),   (r'ות$', 2),   (r'ים$', 2),   (r'ית$', 2),
        (r'ת$', 1),     (r'ך$', 1),    (r'ה$', 1),
        (r'י$', 1),     (r'ם$', 1),    (r'ן$', 1),
        (r'ו$', 1),
    ]
    for pattern, plen in rules:
        if len(w) <= plen + 2:
            continue
        match = re.search(pattern, w)
        if match:
            candidate = w[:match.start()]
            hb_chars = sum(1 for c in candidate if 'א' <= c <= 'ת')
            if hb_chars >= 2:
                w = candidate
                break
    return w


# ---------------------------------------------------------------------------
# CLI demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sample_words = sys.argv[1:] if len(sys.argv) > 1 else [
        "שלום", "שלומך", "שלומית", "עליכם", "רצון", "רוצים",
        "ספר", "ספרים", "חישוב", "חישובים", "כתיבה", "כתובים",
        "שמירה", "שומרים", "שימרתי", "הגיעו", "הגעת",
    ]

    print(f"Input ({len(sample_words)} tokens):")
    for w in sample_words:
        print(f"  {w}")
    print()

    results = analyze_tokens(sample_words)
    print("Results:")
    for orig, lemma in results:
        print(f"  {orig:>10} -> {lemma}")
    print()

    unique_lemmas = get_lemmas(sample_words)
    print(f"Unique lemmas ({len(unique_lemmas)}): {sorted(unique_lemmas)}")
    print()

    rkeys = root_keys(sample_words)
    print(f"Root keys ({len(rkeys)}): {sorted(rkeys)}")
    print()

    # Also test the original rule-based stemmer for comparison
    from hot_words import _hb_root_key as _old_root_key
    old_keys = {_old_root_key(w) for w in sample_words}
    print(f"Old rules root keys ({len(old_keys)}): {sorted(old_keys)}")
