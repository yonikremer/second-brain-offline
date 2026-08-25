#!/usr/bin/env python3
"""Translate Hebrew markdown chunks to English with glossary + name guard.

- Structural chunking at heading/paragraph boundaries (never mid-sentence/table/code).
- English-only docs are skipped (no Hebrew → ledger skipped_english, no output file).
- Preservation by verification (not masking): person names, English spans, and
  URLs/file-paths are extracted from the source chunk, passed to the LLM as
  explicit verbatim-context, and verified to appear in the output.
- Filtered glossary: only terms occurring in chunk are injected.
- Person-name guard: exact-match against data/person_names/ (593 first + 818 last),
  extracted + verified (not masked).
- Structured output {translation, unknown_terms, notes} via response_format=json_object.
- Zero-guessing: unknown terms → ⟦he:<term>⟧ markers, blocked_on_term ledger.
- Content-addressed store data/translations/<sha>/translation.md + ledger.jsonl.
- Chunk-level checkpoints data/translations/chunks/<key>.json: a failure part-way
  through a long document resumes from the last good chunk instead of restarting.
- Bounded retries (max 3).

Config: convert_config.json translation block:
  translation {base_url (\"\"), reviewer_base_url (\"\"), api_key_env (TRANSLATE_API_KEY),
               model (minimax-m2.7), reviewer_model (kimi-k2.7), chunk_chars (6000),
               review_sample (0.2), glossary_path (data/domain_terms/glossary.json),
               fix_rounds (3), chunk_retries (2)}
  Defaults: base_url \"\", reviewer_base_url \"\", chunk_chars 6000, review_sample 0.2,
            glossary_path data/domain_terms/glossary.json, fix_rounds 3, model minimax-m2.7,
            reviewer_model kimi-k2.7, api_key_env TRANSLATE_API_KEY.
  Env precedence: TRANSLATE_BASE_URL primary, QMD_OPENAI_BASE_URL fallback;
  reviewer uses TRANSLATE_REVIEWER_BASE_URL override (see translation_reviewer.py).
  fix_rounds precedence: CLI --fix-rounds > TRANSLATE_FIX_ROUNDS env > config > 3 (0=disable).
  chunk_retries precedence: CLI --chunk-retries > TRANSLATE_CHUNK_RETRIES env > config > 2
    (0=disable). Retries cover model non-compliance (lost segment/cell
    delimiter) only; environment faults such as a missing YAP stay fail-closed.
Fail-fast if base_url missing. --mock for CI (mock is PERSON-sentinel aware: splits by
  ⟦PERSON_n⟧, only wraps remaining [א-ת]{2,} as ⟦he:…⟧ so sentinels are not marked).

CLI:
  python scripts/translate.py [vault_root] [--input DIR] [--glossary PATH] [--out DIR]
                              [--check] [--mock] [--force] [--resume] [--limit N] [--fix-rounds N]
                              [--chunk-retries N]
  vault_root positional (default ".")
  --input DIR     corpus dir (default raw_md/raw auto-detect)
  --glossary PATH glossary.json override (default translation.glossary_path or vault/data/domain_terms/glossary.json)
  --out DIR       output store dir (default vault/data/translations, canonical ledger vault/data/translations/ledger.jsonl)
  --check         only check glossary gate, exit 1 if blocked
  --mock          offline mock (Hebrew marking outside invariants)
  --force         retranslate even if cached (ignores both the document store and
                  the chunk checkpoints)
  --resume        same as default (resume by hash, kept for docs compat)
  --limit N       limit files (0=all)
  --fix-rounds N  max LLM fix rounds per doc after QA failures (default 3, 0=disable, env TRANSLATE_FIX_ROUNDS overrides config)
  --chunk-retries N  retries per chunk on delimiter loss (default 2, 0=disable)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

from . import md_mask
from .translation_checkpoint import (
    chunk_checkpoint_key, load_chunk_checkpoint, save_chunk_checkpoint,
    pipeline_code_fingerprint, names_fingerprint,
)
from .translation_invariants import (
    PERSON_OPEN, PERSON_CLOSE, EN_OPEN, EN_CLOSE, HE_MARKER_FMT,
    HEBREW_WORD_RE,
    mask_person_names, _mask_via_tokens, unmask_person_names,
    is_english_only_doc, mask_english_spans, unmask_english_spans,
    extract_english_spans, extract_urls_and_paths, extract_person_names,
    extract_yaml_frontmatter, extract_code_sections,
    extract_preservation_invariants,
    verify_preserved, verify_all_preserved, verify_ordered, verify_all_ordered, verify_global_order,
)
# Keep private regex names re-exported for tests that patch via translate.* if any
from . import translation_invariants as _invariants
_EN_SPAN_RE = _invariants._EN_SPAN_RE
_COMMON_ENGLISH = _invariants._COMMON_ENGLISH
_URL_RE = _invariants._URL_RE
_FILEPATH_RE = _invariants._FILEPATH_RE
_YAML_RE = _invariants._YAML_RE
_CODE_RE = _invariants._CODE_RE


def get_ledger_path(vault_root: Path, out_root: Path | None = None) -> Path:
    """Canonical ledger path: vault_root/data/translations/ledger.jsonl.

    If out_root is an explicit custom dir outside the vault, use out_root/ledger.jsonl
    so --out tests still write locally. Otherwise always canonical.
    """
    canonical = vault_root / "data" / "translations" / "ledger.jsonl"
    if out_root is None:
        return canonical
    try:
        # If out_root is inside vault_root, prefer canonical to avoid split ledgers
        out_root.resolve().relative_to(vault_root.resolve())
        return canonical
    except ValueError:
        return out_root / "ledger.jsonl"


def load_config(vault_root: Path) -> dict:
    p = vault_root / "convert_config.json"
    if p.exists():
        raw = p.read_text(encoding="utf-8")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"ERROR: invalid convert_config.json ({p}): {e}", file=sys.stderr)
            raise RuntimeError(f"invalid convert_config.json: {e}") from e
    return {}


def resolve_fix_rounds(cfg: dict, cli_value: int | None) -> int:
    """Resolve fix_rounds: CLI > env TRANSLATE_FIX_ROUNDS > config > default 3."""
    if cli_value is not None:
        try:
            v = int(cli_value)
            return max(0, v)
        except (TypeError, ValueError):
            pass
    env = os.environ.get("TRANSLATE_FIX_ROUNDS")
    if env is not None:
        try:
            return max(0, int(env.strip()))
        except (TypeError, ValueError):
            pass
    tcfg = cfg.get("translation", {}) if isinstance(cfg, dict) else {}
    raw = tcfg.get("fix_rounds", 3)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 3


def resolve_chunk_retries(cfg: dict, cli_value: int | None) -> int:
    """Resolve chunk_retries: CLI > env TRANSLATE_CHUNK_RETRIES > config > default 2.

    A retry here costs one chunk; not retrying costs the document, so the default
    is on. Environment faults are still not retried (see _is_retryable_chunk_error).
    """
    if cli_value is not None:
        try:
            return max(0, int(cli_value))
        except (TypeError, ValueError):
            pass
    env = os.environ.get("TRANSLATE_CHUNK_RETRIES")
    if env is not None:
        try:
            return max(0, int(env.strip()))
        except (TypeError, ValueError):
            pass
    tcfg = cfg.get("translation", {}) if isinstance(cfg, dict) else {}
    try:
        return max(0, int(tcfg.get("chunk_retries", 2)))
    except (TypeError, ValueError):
        return 2


def resolve_corpus_dir(vault_root: Path, explicit: Path | None = None) -> Path:
    if explicit:
        p = Path(explicit)
        if not p.is_dir():
            print(f"ERROR: input dir not found: {p}", file=sys.stderr)
            sys.exit(1)
        return p
    for name in ("raw_md", "raw"):
        p = vault_root / name
        if p.is_dir() and any(p.rglob("*.md")):
            return p
    print(f"ERROR: neither raw_md/ nor raw/ with *.md under {vault_root}", file=sys.stderr)
    sys.exit(1)


def _read_csv_skip_comments(path: Path) -> list[str]:
    """Deprecated: CSV helper kept for compat — glossary is now JSON."""
    return []


def load_glossary(glossary_path: Path) -> list[dict]:
    if not glossary_path.exists():
        return []
    if glossary_path.suffix != ".json":
        raise RuntimeError(f"glossary must be .json, got {glossary_path.suffix}: {glossary_path}")
    try:
        rows = json.loads(glossary_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise RuntimeError(f"cannot read glossary: {e}") from e
    if not isinstance(rows, list):
        raise RuntimeError("glossary must be a JSON array")
    return rows


# Person-name helpers — single source in translation_common.py (fail-closed)
from .translation_common import load_codenames, load_person_names  # re-export


from .translation_chunking import chunk_markdown, glossary_for_chunk


from .translation_masking import (
    HEBREW_RANGE, PROCLITICS, RAW_WORD_RE, MIXED_SPLIT_RE,
    _require_yap, _heuristic_split, _roots_for_token, _analyze_with_fallback,
    detect_glossary_terms,
)
# Compat aliases — old masking API removed, glossary is prompt-only
def mask_glossary_terms(chunk_text: str, glossary_rows: list[dict]) -> tuple[str, list[dict]]:  # type: ignore[misc]
    return chunk_text, detect_glossary_terms(chunk_text, glossary_rows)
def unmask_glossary_terms(text: str, term_map: list[dict]) -> str:  # type: ignore[misc]
    return text
from . import translation_masking as _masking
_YAP_AVAILABLE = _masking._YAP_AVAILABLE

# Proxy YAP mock targets so `mock.patch("translate._yap_root_keys")` propagates to masking
import types as _types_mask
class _TranslateMaskingProxy(_types_mask.ModuleType):
    def __setattr__(self, name, value):
        if name in ("_yap_root_keys", "_yap_analyze", "_YAP_AVAILABLE"):
            try:
                setattr(_masking, name, value)
            except Exception:
                pass
        super().__setattr__(name, value)
try:
    import sys as _sys_mask
    _sys_mask.modules[__name__].__class__ = _TranslateMaskingProxy
except Exception:
    pass
def __getattr__(name):  # PEP 562
    if name in ("_yap_root_keys", "_yap_analyze", "_YAP_AVAILABLE"):
        return getattr(_masking, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


from .translation_llm import call_llm, mock_translate

from .translation_prompt import build_prompt, build_fix_prompt, _build_chunked_fix_prompts, format_qa_failures
from . import translation_qa as qa_mod
from .translation_common import compute_glossary_version as _compute_gv, _normalize_en_for_collision


def run_qa_for_doc(source_path: Path, trans_body: str, trans_meta: dict,
                   glossary: list[dict], vault_root: Path | None,
                   term_map: list[dict] | None = None) -> list[dict]:
    """Run scripted QA battery; returns list of check dicts. Falls back gracefully.

    term_map is threaded explicitly so document-level glossary check gates the doc.
    If term_map is None, falls back to trans_meta.get("term_map") for compat.
    """
    try:
        return qa_mod.run_all(source_path, trans_body, trans_meta, glossary, vault_root=vault_root, term_map=term_map)
    except Exception as e:
        return [{"check": "qa_runner", "status": "fail", "error": str(e)[:500]}]


def _translate_chunks(raw_text: str, first_names: set[str], last_names: set[str],
                      glossary: list[dict], base_url: str, api_key: str, model: str,
                      mock: bool, chunk_chars: int, no_mask: bool,
                      name_candidates: set[str] | None) -> tuple[str, list[str], list[dict]]:
    """Translate raw_text chunk by chunk. Returns (full_translation, doc_unknown, chunk_notes).

    Also collects term_map across chunks for ledger determinism (Task 5).
    The fourth element (term_map list) is available via _translate_chunks_with_term_map;
    this wrapper keeps back-compat and attaches term_map via attribute.
    """
    # Delegate to version that also returns term_map
    full, unknown, notes, _tm = _translate_chunks_with_term_map(
        raw_text, first_names, last_names, glossary, base_url, api_key, model, mock, chunk_chars, no_mask, name_candidates
    )
    # Stash term_map on the function for caller inspection without breaking tuple unpack
    _translate_chunks.last_term_map = _tm  # type: ignore[attr-defined]
    return full, unknown, notes


def _glossary_fingerprint(glossary: list[dict]) -> str:
    """Stable digest of the glossary rows that can affect a translation.

    Part of the chunk checkpoint key: editing the glossary must invalidate every
    cached chunk, because the injected term list changes.
    """
    rows = sorted([
        (str(r.get("term_he", "")), tuple(sorted(_normalize_en_for_collision(str(o)) for o in (r.get("translations") or []))),
         str(r.get("keep_source", "")), str(r.get("status", "")))
        for r in glossary
    ], key=lambda x: x[0])
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()[:16]


# Chunk failures the model can plausibly get right on a second attempt. Everything
# else (missing YAP, missing md_mask) is an environment fault where retrying just
# burns LLM budget and hides the real cause, so it stays fail-closed.
_RETRYABLE_CHUNK_ERRORS = ("Segment count mismatch", "Cell count mismatch")


def _is_retryable_chunk_error(exc: Exception) -> bool:
    return any(m in str(exc) for m in _RETRYABLE_CHUNK_ERRORS)


def _count_option(body: str, option: str) -> int:
    """Word-boundary count for glossary option in translated body."""
    pat = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(option) + r"(?![A-Za-z0-9_])")
    return len(pat.findall(body))


def _translate_one_chunk(chunk_text: str, section_path: str, prev_tail: str,
                         first_names: set[str], last_names: set[str],
                         glossary: list[dict], base_url: str, api_key: str,
                         model: str, mock: bool, no_mask: bool,
                         previous_choices: dict[str, str] | None = None) -> dict:
    """Translate exactly one chunk. Returns a JSON-serialisable checkpoint payload.

    Raises RuntimeError on delimiter loss — scoped to this chunk,
    so the caller can retry it instead of losing the document.
    """
    chunk_unknown: list[str] = []
    invariants = extract_preservation_invariants(chunk_text, first_names, last_names)
    g_rows = glossary_for_chunk(chunk_text, glossary)
    # Deterministic detection: use full approved glossary to catch inflected forms (הDBים, המערכות)
    detect_rows = [r for r in glossary if (r.get("status") or "approved").strip() in ("approved", "keep_source") and (r.get("term_he") or "").strip()]
    chunk_term_map: list[dict] = []
    if detect_rows:
        try:
            chunk_term_map = detect_glossary_terms(chunk_text, detect_rows)
        except RuntimeError:
            raise
        except FileNotFoundError as e:
            raise RuntimeError(f"YAP required for glossary detection — fail-closed: {e}") from e
    use_mask = not no_mask
    if use_mask and md_mask is None:
        raise RuntimeError("md_mask missing — restore scripts/md_mask.py (table/placeholder masking required)")
    if use_mask:
        opts = md_mask.MdOptions(
            translate_frontmatter=False,
            translate_multiline_code=False,
            translate_latex=False,
            translate_link_text=True,
        )
        filt = md_mask.filter_markdown_lines(chunk_text.split("\n"), opts)
        segs = md_mask.split_markdown_segments(filt.content_lines, filt.source_line_numbers)
        cell_texts = md_mask.get_table_cell_texts(filt.maps)
        SEG_DELIM = "⟦SEG⟧"
        if segs.texts_to_translate:
            seg_prompt = build_prompt(
                SEG_DELIM.join(segs.texts_to_translate),
                section_path,
                g_rows,
                prev_tail,
                invariants,
                term_map=chunk_term_map,
                previous_choices=previous_choices,
            )
            if mock:
                # Mock without glossary markers — only protect invariants + SEG delims
                _he_single = re.compile(r"[א-ת]+")
                protected: list[str] = []
                if invariants:
                    for cat in ("yaml_frontmatter", "code_sections", "person_names", "english_spans", "urls_and_paths"):
                        for v in invariants.get(cat, []):
                            if v and v not in protected:
                                protected.append(v)
                for delim in ("⟦SEG⟧", "⟦CELL⟧"):
                    if delim in SEG_DELIM.join(segs.texts_to_translate) and delim not in protected:
                        protected.append(delim)
                masked_payload = SEG_DELIM.join(segs.texts_to_translate)
                if protected:
                    protected_sorted = sorted(protected, key=len, reverse=True)
                    pat = re.compile("|".join(re.escape(p) for p in protected_sorted))
                    parts = pat.split(masked_payload)
                    hits = pat.findall(masked_payload)
                    wrapped_parts: list[str] = []
                    for i, seg in enumerate(parts):
                        wrapped_parts.append(_he_single.sub(lambda m: HE_MARKER_FMT.format(term=m.group(0)), seg))
                        if i < len(hits):
                            wrapped_parts.append(hits[i])
                    simulated = "".join(wrapped_parts)
                else:
                    simulated = _he_single.sub(lambda m: HE_MARKER_FMT.format(term=m.group(0)), masked_payload)
                translated_seg_text = simulated
                res_seg = {"translation": translated_seg_text, "unknown_terms": [], "notes": ["mock"]}
            else:
                res_seg = call_llm(base_url, api_key, model, seg_prompt)
                translated_seg_text = res_seg["translation"]
            translated_segments = translated_seg_text.split(SEG_DELIM)
            if len(translated_segments) != len(segs.texts_to_translate):
                raise RuntimeError(
                    f"Segment count mismatch: sent {len(segs.texts_to_translate)}, "
                    f"got {len(translated_segments)} — model did not preserve delimiters"
                )
        else:
            translated_segments = []
            res_seg = {"unknown_terms": [], "notes": []}
        if cell_texts:
            if mock:
                cell_delim = "⟦CELL⟧"
                joined_cells = cell_delim.join(cell_texts)
                _he_single2 = re.compile(r"[א-ת]+")
                protected2: list[str] = []
                for delim in ("⟦SEG⟧", "⟦CELL⟧"):
                    if delim in joined_cells and delim not in protected2:
                        protected2.append(delim)
                if protected2:
                    protected_sorted2 = sorted(protected2, key=len, reverse=True)
                    pat2 = re.compile("|".join(re.escape(p) for p in protected_sorted2))
                    parts2 = pat2.split(joined_cells)
                    hits2 = pat2.findall(joined_cells)
                    wrapped2: list[str] = []
                    for i, seg in enumerate(parts2):
                        wrapped2.append(_he_single2.sub(lambda m: HE_MARKER_FMT.format(term=m.group(0)), seg))
                        if i < len(hits2):
                            wrapped2.append(hits2[i])
                    simulated_cells = "".join(wrapped2)
                else:
                    simulated_cells = _he_single2.sub(lambda m: HE_MARKER_FMT.format(term=m.group(0)), joined_cells)
                translated_cells_text = simulated_cells
                translated_cells = translated_cells_text.split(cell_delim)
                if len(translated_cells) != len(cell_texts):
                    raise RuntimeError(
                        f"Cell count mismatch: sent {len(cell_texts)}, got {len(translated_cells)}"
                    )
            else:
                cell_delim = "⟦CELL⟧"
                joined_cells = cell_delim.join(cell_texts)
                cell_prompt = build_prompt(joined_cells, section_path, g_rows, "", None, term_map=chunk_term_map, previous_choices=previous_choices)
                cr = call_llm(base_url, api_key, model, cell_prompt)
                translated_cells_text = cr["translation"]
                translated_cells = translated_cells_text.split(cell_delim)
                if len(translated_cells) != len(cell_texts):
                    raise RuntimeError(
                        f"Cell count mismatch: sent {len(cell_texts)}, got {len(translated_cells)} — model did not preserve delimiters"
                    )
            md_mask.inject_translated_table_cells(filt.maps, translated_cells)
        merged_lines = md_mask.merge_markdown_segments(segs.line_segments, translated_segments)
        trans = md_mask.restore_placeholders("\n".join(merged_lines), filt.maps)
        res = {
            "translation": trans,
            "unknown_terms": res_seg.get("unknown_terms", []),
            "notes": res_seg.get("notes", []),
        }
    else:
        prompt = build_prompt(chunk_text, section_path, g_rows, prev_tail, invariants, term_map=chunk_term_map, previous_choices=previous_choices)
        if mock:
            _he_single3 = re.compile(r"[א-ת]+")
            protected3: list[str] = []
            if invariants:
                for cat in ("yaml_frontmatter", "code_sections", "person_names", "english_spans", "urls_and_paths"):
                    for v in invariants.get(cat, []):
                        if v and v not in protected3:
                            protected3.append(v)
            for delim in ("⟦SEG⟧", "⟦CELL⟧"):
                if delim in chunk_text and delim not in protected3:
                    protected3.append(delim)
            if protected3:
                protected_sorted3 = sorted(protected3, key=len, reverse=True)
                pat3 = re.compile("|".join(re.escape(p) for p in protected_sorted3))
                parts3 = pat3.split(chunk_text)
                hits3 = pat3.findall(chunk_text)
                wrapped3: list[str] = []
                for i, seg in enumerate(parts3):
                    wrapped3.append(_he_single3.sub(lambda m: HE_MARKER_FMT.format(term=m.group(0)), seg))
                    if i < len(hits3):
                        wrapped3.append(hits3[i])
                simulated = "".join(wrapped3)
            else:
                simulated = _he_single3.sub(lambda m: HE_MARKER_FMT.format(term=m.group(0)), chunk_text)
            trans = simulated
            res = {"translation": trans, "unknown_terms": [], "notes": ["mock"]}
        else:
            res = call_llm(base_url, api_key, model, prompt)
            trans = res["translation"]
            res["translation"] = trans
    missing = verify_all_preserved(invariants, trans)
    if missing:
        for cat, items in missing.items():
            res.setdefault("notes", []).append(f"preserve_fail:{cat}:{items}")
        for items in missing.values():
            chunk_unknown.extend(items)
    order_bad = verify_all_ordered(invariants, trans)
    global_bad = verify_global_order(chunk_text, invariants, trans)
    if order_bad:
        for cat, items in order_bad.items():
            res.setdefault("notes", []).append(f"order_fail:{cat}:{items}")
        for items in order_bad.values():
            chunk_unknown.extend(items)
    if global_bad:
        res.setdefault("notes", []).append(f"global_order_fail:{global_bad}")
        chunk_unknown.extend(global_bad)
    for ut in res.get("unknown_terms", []):
        ut = str(ut).strip()
        if ut and ut not in trans and ut in chunk_text:
            marker = HE_MARKER_FMT.format(term=ut)
            if marker not in trans:
                trans = trans.rstrip() + f" {marker}"
        if ut:
            chunk_unknown.append(ut)
    return {
        "translation": trans,
        "term_map": chunk_term_map,
        "unknown": chunk_unknown,
        "notes": list(res.get("notes") or []),
        "person_names": sorted(invariants.get("person_names") or []),
    }


def _translate_chunk_with_retry(chunk_text: str, section_path: str, prev_tail: str,
                                first_names: set[str], last_names: set[str],
                                glossary: list[dict], base_url: str, api_key: str,
                                model: str, mock: bool, no_mask: bool,
                                retries: int,
                                previous_choices: dict[str, str] | None = None) -> dict:
    """_translate_one_chunk plus bounded retries for model non-compliance.

    Scoping the retry to the chunk is the second half of HANDOFF §4.1: a dropped
    delimiter used to raise out of the whole document.
    """
    attempt = 0
    while True:
        try:
            return _translate_one_chunk(chunk_text, section_path, prev_tail, first_names,
                                        last_names, glossary, base_url, api_key, model,
                                        mock, no_mask, previous_choices)
        except RuntimeError as e:
            if attempt >= retries or not _is_retryable_chunk_error(e):
                raise
            attempt += 1
            print(f"  chunk retry {attempt}/{retries} [{section_path or 'no heading'}]: {e}",
                  file=sys.stderr)


def _translate_chunks_with_term_map(raw_text: str, first_names: set[str], last_names: set[str],
                      glossary: list[dict], base_url: str, api_key: str, model: str,
                      mock: bool, chunk_chars: int, no_mask: bool,
                      name_candidates: set[str] | None,
                      out_root: Path | None = None, chunk_retries: int = 0,
                      force: bool = False) -> tuple[str, list[str], list[dict], list[dict]]:
    """Inner impl that also returns aggregated term_map for ledger.

    When out_root is given, every chunk that translates cleanly is checkpointed to
    out_root/chunks/, so a failure part-way through a 67-chunk document costs one
    chunk on the next run instead of the whole document (HANDOFF §4.1).
    """
    chunks = chunk_markdown(raw_text, max_chars=chunk_chars)
    chunk_translations: list[str] = []
    doc_unknown: list[str] = []
    all_notes: list[dict] = []
    # Aggregated term_map keyed by (term_he, tuple(sorted(translations)), keep_source) with summed occurrences
    agg_term_map: dict[tuple[str, tuple[str, ...], bool], dict] = {}
    glossary_fp = _glossary_fingerprint(glossary) if out_root is not None else ""
    names_fp = names_fingerprint(first_names, last_names) if out_root is not None else ""
    code_fp = pipeline_code_fingerprint() if out_root is not None else ""
    reused = 0
    prev_tail = ""
    doc_choices: dict[str, str] = {}
    for ch in chunks:
        chunk_text = ch["chunk_text"]
        section_path = ch["section_path"]
        key = None
        payload = None
        if out_root is not None:
            key = chunk_checkpoint_key(chunk_text, section_path, prev_tail,
                                       glossary_fp, model, mock, no_mask,
                                       names_fp, code_fp)
            if not force:
                payload = load_chunk_checkpoint(out_root, key)
                if payload is not None:
                    reused += 1
        if payload is None:
            payload = _translate_chunk_with_retry(
                chunk_text, section_path, prev_tail, first_names, last_names, glossary,
                base_url, api_key, model, mock, no_mask, chunk_retries, doc_choices if doc_choices else None)
            if key is not None:
                try:
                    save_chunk_checkpoint(out_root, key, payload)
                except (OSError, ValueError, TypeError) as e:
                    # A store we cannot write to must not abort a run that is
                    # otherwise succeeding; it only costs resumability. ValueError
                    # covers UnicodeEncodeError from a lone surrogate in an LLM
                    # response, which is not an OSError and which main() — guarding
                    # only RuntimeError — would otherwise let end the whole batch.
                    print(f"warn: cannot checkpoint chunk [{section_path}]: {e}", file=sys.stderr)

        trans = payload["translation"]
        chunk_term_map = payload.get("term_map") or []
        # Track chosen translations for cross-chunk consistency
        for e in chunk_term_map:
            term_he = e.get("term_he", "") or ""
            if not term_he or term_he in doc_choices:
                # keep first choice — do not overwrite
                if term_he in doc_choices:
                    continue
            translations = e.get("translations") or []
            if isinstance(translations, str):
                translations = [translations] if translations.strip() else []
            translations = [str(o).strip() for o in translations if str(o).strip()]
            chosen = None
            if e.get("keep_source"):
                if term_he and term_he in trans:
                    chosen = term_he
            else:
                for opt in translations:
                    if _count_option(trans, opt) > 0:
                        chosen = opt
                        break
                # fallback: simple substring if word-boundary missed
                if chosen is None:
                    for opt in translations:
                        if opt in trans:
                            chosen = opt
                            break
            if chosen is not None:
                doc_choices[term_he] = chosen
        for e in chunk_term_map:
            translations = e.get("translations") or []
            if isinstance(translations, str):
                translations = [translations] if translations.strip() else []
            translations = tuple(sorted(str(o).strip() for o in translations if str(o).strip()))
            key_tm = (e.get("term_he", ""), translations, bool(e.get("keep_source")))
            if key_tm not in agg_term_map:
                agg_term_map[key_tm] = {
                    "term_he": e["term_he"],
                    "translations": list(translations),
                    "keep_source": bool(e.get("keep_source")),
                    "occurrences": int(e.get("occurrences", 0)),
                    "src_order": int(e.get("src_order", 0)),
                }
            else:
                agg_term_map[key_tm]["occurrences"] += int(e.get("occurrences", 0))
                # keep earliest src_order
                if int(e.get("src_order", 0)) < agg_term_map[key_tm]["src_order"]:
                    agg_term_map[key_tm]["src_order"] = int(e.get("src_order", 0))
        if name_candidates is not None and payload.get("person_names"):
            name_candidates.update(payload["person_names"])
        doc_unknown.extend(payload.get("unknown") or [])
        if payload.get("notes"):
            all_notes.append({"chunk": section_path, "notes": payload["notes"]})
        chunk_translations.append(trans)
        prev_tail = trans[-400:] if trans else ""
    if reused:
        print(f"  resumed {reused}/{len(chunks)} chunks from checkpoints", file=sys.stderr)
    full_translation = "\n\n".join(chunk_translations)
    for entry in agg_term_map.values():
        entry["chosen"] = doc_choices.get(entry["term_he"])
    # Preserve deterministic source order
    term_map = sorted(agg_term_map.values(), key=lambda x: (x.get("src_order", 0), x["term_he"]))
    return full_translation, doc_unknown, all_notes, term_map


def translate_one_doc(md_file: Path, vault_root: Path, out_root: Path,
                      glossary: list[dict], first_names: set[str], last_names: set[str],
                      base_url: str, api_key: str, model: str,
                      mock: bool, fix_rounds: int, chunk_chars: int,
                      no_mask: bool = False, chunk_retries: int = 0,
                      force: bool = False) -> dict:
    """Translate single file (no QA fix loop). Returns dict with translation,status etc."""
    _ = fix_rounds  # kept for caller compat; loop is in translate_one_doc_with_fix
    # out_root doubles as the chunk checkpoint store; the document-level
    # content-addressed write is still handled by the caller (main).
    rel = md_file.relative_to(vault_root).as_posix() if md_file.is_relative_to(vault_root) else md_file.name
    raw_text = md_file.read_text(encoding="utf-8")
    if is_english_only_doc(raw_text):
        return {"skipped": True, "rel": rel, "source_hash": hashlib.sha256(raw_text.encode()).hexdigest(), "raw_text": raw_text}
    src_hash = hashlib.sha256(raw_text.encode()).hexdigest()
    name_candidates: set[str] = set()
    full_translation, doc_unknown, _notes, term_map = _translate_chunks_with_term_map(
        raw_text, first_names, last_names, glossary, base_url, api_key, model, mock, chunk_chars,
        no_mask, name_candidates, out_root=out_root, chunk_retries=chunk_retries, force=force)
    has_markers = "⟦he:" in full_translation
    status = "blocked_on_term" if (has_markers or doc_unknown) else "completed"
    return {
        "translation": full_translation,
        "status": status,
        "marker_count": full_translation.count("⟦he:"),
        "unknown_terms": sorted(set(doc_unknown)),
        "source_hash": src_hash,
        "rel": rel,
        "raw_text": raw_text,
        "name_candidates": name_candidates,
        "term_map": term_map,
    }


def translate_one_doc_with_fix(md_file: Path, vault_root: Path, out_root: Path,
                               glossary: list[dict], first_names: set[str], last_names: set[str],
                               base_url: str, api_key: str, model: str,
                               mock: bool, fix_rounds: int, chunk_chars: int,
                               no_mask: bool = False, chunk_retries: int = 0,
                               force: bool = False) -> dict:
    """Full doc translate + QA + bounded LLM fix rounds."""
    result = translate_one_doc(md_file, vault_root, out_root, glossary, first_names, last_names,
                               base_url, api_key, model, mock, fix_rounds, chunk_chars, no_mask,
                               chunk_retries=chunk_retries, force=force)
    if result.get("skipped"):
        return result
    source_path = md_file
    trans_body = result["translation"]
    raw_text = result["raw_text"]
    doc_term_map = result.get("term_map", [])
    meta_stub = {"source_doc": result["rel"], "term_map": doc_term_map}
    full_invariants = extract_preservation_invariants(raw_text, first_names, last_names)
    checks = run_qa_for_doc(source_path, trans_body, meta_stub, glossary, vault_root, term_map=doc_term_map)
    failures = format_qa_failures(checks)
    fix_rounds_used = 0
    all_fix_attempts: list[dict] = []
    fix_unknown_terms: list[str] = []
    while failures and fix_rounds_used < fix_rounds:
        fix_rounds_used += 1
        if mock:
            # Deterministic mock — plain Hebrew marking outside invariants (no glossary markers)
            _he_single = re.compile(r"[א-ת]+")
            protected: list[str] = []
            if full_invariants:
                for cat in ("yaml_frontmatter", "code_sections", "person_names", "english_spans", "urls_and_paths"):
                    for v in full_invariants.get(cat, []):
                        if v and v not in protected:
                            protected.append(v)
            if protected:
                protected_sorted = sorted(protected, key=len, reverse=True)
                pat = re.compile("|".join(re.escape(p) for p in protected_sorted))
                parts = pat.split(raw_text)
                hits = pat.findall(raw_text)
                wrapped: list[str] = []
                for i, seg in enumerate(parts):
                    wrapped.append(_he_single.sub(lambda m: HE_MARKER_FMT.format(term=m.group(0)), seg))
                    if i < len(hits):
                        wrapped.append(hits[i])
                new_body = "".join(wrapped)
            else:
                new_body = _he_single.sub(lambda m: HE_MARKER_FMT.format(term=m.group(0)), raw_text)
            fix_unknown_terms.extend(re.findall(r"⟦he:([^⟧]+)⟧", new_body))
        else:
            # For large docs, avoid silent 12k truncation by chunking the fix
            threshold = max(12000, chunk_chars * 2)
            is_large = len(raw_text) > threshold or len(trans_body) > threshold
            if is_large:
                prompts = _build_chunked_fix_prompts(raw_text, trans_body, failures,
                                                     glossary_for_chunk(raw_text, glossary),
                                                     full_invariants, chunk_chars,
                                                     first_names, last_names)
                # Log truncation avoidance
                print(f"  fix round {fix_rounds_used}: large doc ({len(raw_text)} src, {len(trans_body)} trans) — chunked into {len(prompts)} prompts", file=sys.stderr)
                chunk_translations: list[str] = []
                chunk_unknown: list[str] = []
                chunk_failed = False
                last_err = None
                for p_idx, p in enumerate(prompts):
                    try:
                        resp = call_llm(base_url, api_key, model, p)
                        ct = resp.get("translation", "")
                        chunk_translations.append(ct)
                        chunk_unknown.extend([str(x).strip() for x in resp.get("unknown_terms", []) if str(x).strip()])
                        chunk_unknown.extend(re.findall(r"⟦he:([^⟧]+)⟧", ct))
                    except Exception as e:
                        last_err = str(e)[:500]
                        chunk_failed = True
                        print(f"  fix chunk {p_idx+1}/{len(prompts)} failed: {last_err}", file=sys.stderr)
                        break
                if chunk_failed:
                    all_fix_attempts.append({"round": fix_rounds_used, "error": last_err or "chunk fix failed", "failures_before": failures, "chunked": True, "chunks": len(prompts), "src_len": len(raw_text), "trans_len": len(trans_body)})
                    break
                new_body = "\n\n".join(chunk_translations)
                fix_unknown_terms.extend(chunk_unknown)
                # Record that this round was chunked
                all_fix_attempts.append({"round": fix_rounds_used, "failures_before": failures, "chunked": True, "chunks": len(prompts), "src_len": len(raw_text), "trans_len": len(trans_body)})
                trans_body = new_body
                # Normalize ledger schema: also include chunked flag for non-chunked? handled below
                checks = run_qa_for_doc(source_path, trans_body, meta_stub, glossary, vault_root, term_map=doc_term_map)
                failures = format_qa_failures(checks)
                continue
            # Normal whole-doc fix for small docs
            fix_prompt = build_fix_prompt(raw_text, trans_body, failures,
                                          glossary_rows=glossary_for_chunk(raw_text, glossary),
                                          invariants=full_invariants)
            try:
                resp = call_llm(base_url, api_key, model, fix_prompt)
                new_body = resp.get("translation", "")
                fix_unknown_terms.extend([str(x).strip() for x in resp.get("unknown_terms", []) if str(x).strip()])
                fix_unknown_terms.extend(re.findall(r"⟦he:([^⟧]+)⟧", new_body))
            except Exception as e:
                all_fix_attempts.append({"round": fix_rounds_used, "error": str(e)[:500], "failures_before": failures, "chunked": False, "src_len": len(raw_text), "trans_len": len(trans_body)})
                break
        trans_body = new_body
        all_fix_attempts.append({"round": fix_rounds_used, "failures_before": failures, "chunked": False, "src_len": len(raw_text), "trans_len": len(trans_body)})
        checks = run_qa_for_doc(source_path, trans_body, meta_stub, glossary, vault_root, term_map=doc_term_map)
        failures = format_qa_failures(checks)
    if failures:
        final_status = "qa_failed"
    else:
        has_markers = "⟦he:" in trans_body
        final_status = "blocked_on_term" if has_markers else "completed"
    marker_terms = re.findall(r"⟦he:([^⟧]+)⟧", trans_body)
    recomputed_unknown = sorted(set(marker_terms + [str(x).strip() for x in fix_unknown_terms if str(x).strip()]))
    # When fix was attempted, merge original unknown_terms (preservation failures) with recomputed
    # so blocked docs aren't hidden if fix resolves QA but leaves invariant gaps
    if fix_rounds_used > 0:
        orig = result.get("unknown_terms", [])
        final_unknown = sorted(set(orig) | set(recomputed_unknown))
    else:
        final_unknown = result.get("unknown_terms", [])
    result.update({
        "translation": trans_body,
        "status": final_status,
        "marker_count": trans_body.count("⟦he:"),
        "unknown_terms": sorted(set(final_unknown)),
        "fix_rounds_used": fix_rounds_used,
        "fix_attempts": all_fix_attempts,
        "qa_checks": checks,
        "qa_failures": failures,
    })
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description="Translate markdown chunks with glossary + name guard")
    ap.add_argument("vault_root", nargs="?", default=".", help="vault root")
    ap.add_argument("--input", dest="input_dir", default=None, help="corpus dir (default raw_md/raw)")
    ap.add_argument("--glossary", default=None, help="glossary.json path")
    ap.add_argument("--out", dest="out_dir", default=None, help="output store dir")
    ap.add_argument("--check", action="store_true", help="only check glossary gate, exit 1 if blocked")
    ap.add_argument("--mock", action="store_true", help="offline mock (no LLM)")
    ap.add_argument("--force", action="store_true", help="retranslate even if cached")
    ap.add_argument("--resume", action="store_true", help="same as default (kept for docs compat)")
    ap.add_argument("--no-mask", action="store_true", help="disable md_mask placeholder masking (debug)")
    ap.add_argument("--limit", type=int, default=0, help="limit files (0=all)")
    ap.add_argument("--fix-rounds", type=int, default=None, help="max LLM fix rounds per doc after QA failures (default 3, 0=disable)")
    ap.add_argument("--chunk-retries", type=int, default=None, help="retries per chunk on delimiter loss (default 2, 0=disable, env TRANSLATE_CHUNK_RETRIES overrides config)")
    args = ap.parse_args(argv)

    vault_root = Path(args.vault_root).resolve()
    cfg = load_config(vault_root)
    tcfg = cfg.get("translation", {})
    fix_rounds = resolve_fix_rounds(cfg, args.fix_rounds)
    chunk_retries = resolve_chunk_retries(cfg, args.chunk_retries)
    print(f"Fix rounds: {fix_rounds}")
    print(f"Chunk retries: {chunk_retries}")

    # Glossary gate — path from CLI > convert_config.json translation.glossary_path > default
    if args.glossary:
        glossary_path = Path(args.glossary)
    elif tcfg.get("glossary_path"):
        gp = Path(tcfg["glossary_path"])
        glossary_path = gp if gp.is_absolute() else vault_root / gp
    else:
        glossary_path = vault_root / "data" / "domain_terms" / "glossary.json"

    if args.check:
        # Use shared check
        try:
            from check_glossary import check_glossary
        except ImportError:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from check_glossary import check_glossary
        ok, errors = check_glossary(glossary_path)
        if ok:
            print(f"glossary OK: {glossary_path}")
            sys.exit(0)
        print(f"glossary BLOCKED: {glossary_path}", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)

    base_url = os.environ.get("TRANSLATE_BASE_URL") or os.environ.get("QMD_OPENAI_BASE_URL") or tcfg.get("base_url", "")
    api_key = os.environ.get("TRANSLATE_API_KEY") or os.environ.get("QMD_OPENAI_API_KEY") or ""
    model = tcfg.get("model") or os.environ.get("TRANSLATE_MODEL") or "minimax-m2.7"

    if not args.mock and not base_url:
        print("ERROR: translation base_url missing. Set TRANSLATE_BASE_URL or QMD_OPENAI_BASE_URL or convert_config.json translation.base_url", file=sys.stderr)
        sys.exit(1)

    glossary = load_glossary(glossary_path)
    if glossary_path.exists():
        print(f"Glossary: {len(glossary)} rows from {glossary_path}")
    else:
        print(f"Glossary: none ({glossary_path} not found) — translating without glossary")

    # Codename-aware person names: exclude org codenames (static file + glossary terms)
    # so that a codename like ברק/דניאל is translated via glossary, not masked as PERSON.
    glossary_terms = {r.get("term_he", "").strip() for r in glossary if (r.get("status") or "").strip() in ("approved", "keep_source") and r.get("term_he", "").strip()}
    # Load with glossary exclusion (primary); codenames.txt is optional manual override
    # Keep separate counts for audit (glossary_terms may be hundreds of domain terms,
    # only those overlapping the allowlist are true codename exclusions).
    raw_first, raw_last = load_person_names(vault_root)
    raw_all = raw_first | raw_last
    codenames_file = load_codenames(vault_root)
    glossary_overlap = glossary_terms & raw_all
    codenames_overlap = codenames_file & raw_all
    first_names, last_names = load_person_names(vault_root, exclude=glossary_terms)
    total_excluded = codenames_overlap | glossary_overlap
    if total_excluded:
        print(f"Codenames excluded from PERSON guard: {len(total_excluded)} (file:{len(codenames_overlap)} glossary:{len(glossary_overlap)} — {', '.join(sorted(list(total_excluded))[:5])}{' ...' if len(total_excluded) > 5 else ''})")
    print(f"Person names: {len(first_names)} first, {len(last_names)} last (raw {len(raw_first)}/{len(raw_last)}, excluded {len(total_excluded)})")

    corpus_dir = resolve_corpus_dir(vault_root, Path(args.input_dir) if args.input_dir else None)
    md_files = sorted(corpus_dir.rglob("*.md"))
    if args.limit:
        md_files = md_files[:args.limit]
    print(f"Translating {len(md_files)} files from {corpus_dir}")

    out_root = Path(args.out_dir) if args.out_dir else vault_root / "data" / "translations"
    out_root.mkdir(parents=True, exist_ok=True)

    # Ledger (canonical: vault_root/data/translations/ledger.jsonl)
    ledger_path = get_ledger_path(vault_root, out_root)
    try:
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"warn: cannot create ledger dir {ledger_path.parent}: {e}", file=sys.stderr)
    # Task 5: deterministic ledger glossary_version via shared helper (12-char hash or no-glossary)
    try:
        glossary_version = _compute_gv(glossary_path)
    except Exception:
        # Fallback to legacy inline (should not happen — translation_common is stdlib)
        glossary_version = ""
        if glossary_path.exists():
            try:
                glossary_version = hashlib.sha256(glossary_path.read_bytes()).hexdigest()[:12]
            except OSError:
                glossary_version = "no-glossary"
        else:
            glossary_version = "no-glossary"

    name_candidates: set[str] = set()
    translated = 0
    blocked = 0
    skipped_english = 0
    failed_docs: list[str] = []
    qa_failed = 0

    try:
        chunk_chars = int(tcfg.get("chunk_chars", 6000))
    except (TypeError, ValueError):
        chunk_chars = 6000

    for md_file in md_files:
        rel = md_file.relative_to(vault_root).as_posix() if md_file.is_relative_to(vault_root) else md_file.name
        try:
            raw_text = md_file.read_text(encoding="utf-8")
        except OSError as e:
            print(f" skip {rel}: {e}", file=sys.stderr)
            continue

        # Cache check (content-addressed) — do before any LLM work
        src_hash_pre = hashlib.sha256(raw_text.encode()).hexdigest()
        store_dir_pre = out_root / src_hash_pre[:2] / src_hash_pre
        out_file_pre = store_dir_pre / "translation.md"
        if out_file_pre.exists() and not args.force:
            # Fail-closed: cached qa_failed must still count toward exit 1
            try:
                cached_text = out_file_pre.read_text(encoding="utf-8")
                is_qa_failed = False
                # Robust frontmatter parse (not substring) — extract JSON between --- markers
                if cached_text.startswith("---\n"):
                    end = cached_text.find("\n---\n", 4)
                    if end != -1:
                        try:
                            fm = json.loads(cached_text[4:end].strip())
                            is_qa_failed = fm.get("status") == "qa_failed"
                        except (json.JSONDecodeError, ValueError):
                            is_qa_failed = bool(re.search(r'"status"\s*:\s*"qa_failed"', cached_text))
                    else:
                        is_qa_failed = bool(re.search(r'"status"\s*:\s*"qa_failed"', cached_text))
                else:
                    is_qa_failed = bool(re.search(r'"status"\s*:\s*"qa_failed"', cached_text))
                if is_qa_failed:
                    failed_docs.append(rel)
                    qa_failed += 1
                    print(f"  {rel}: qa_failed (cached)", file=sys.stderr)
            except OSError as e:
                print(f"warn: cannot read cached {out_file_pre}: {e}", file=sys.stderr)
            continue

        # Translate with QA fix loop (handles english-only internally)
        try:
            result = translate_one_doc_with_fix(
                md_file, vault_root, out_root, glossary, first_names, last_names,
                base_url, api_key, model, args.mock, fix_rounds, chunk_chars,
                no_mask=args.no_mask, chunk_retries=chunk_retries, force=args.force)
        except RuntimeError as e:
            # Hard failure like segment mismatch
            print(f"  {rel}: error {e}", file=sys.stderr)
            failed_docs.append(rel)
            event = {
                "event": "translation_error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "source_doc": rel,
                "source_hash": src_hash_pre,
                "model": model,
                "model_id": model,
                "glossary_version": glossary_version,
                "term_map": [],
                "status": "error",
                "error": str(e)[:500],
            }
            with open(ledger_path, "a", encoding="utf-8") as lf:
                lf.write(json.dumps(event, ensure_ascii=False) + "\n")
            continue

        if result.get("skipped"):
            src_hash = result["source_hash"]
            event = {
                "event": "skipped_english",
                "ts": datetime.now(timezone.utc).isoformat(),
                "source_doc": rel,
                "source_hash": src_hash,
                "model": model,
                "model_id": model,
                "glossary_version": glossary_version,
                "term_map": [],
                "status": "skipped_english",
            }
            with open(ledger_path, "a", encoding="utf-8") as lf:
                lf.write(json.dumps(event, ensure_ascii=False) + "\n")
            skipped_english += 1
            print(f"  {rel}: skipped_english (no Hebrew)")
            continue

        # Aggregate name candidates
        if result.get("name_candidates"):
            name_candidates.update(result["name_candidates"])

        full_translation = result["translation"]
        status = result["status"]
        src_hash = result["source_hash"]
        fix_used = result.get("fix_rounds_used", 0)
        qa_failures = result.get("qa_failures", [])
        # Task 5: deterministic ledger term_map + model_id
        doc_term_map = result.get("term_map", [])
        # qa_checks retained for debugging but not written to frontmatter (failures are)
        _qa_checks = result.get("qa_checks", [])

        # Write content-addressed store
        store_dir = out_root / src_hash[:2] / src_hash
        store_dir.mkdir(parents=True, exist_ok=True)
        out_file = store_dir / "translation.md"
        frontmatter = {
            "source_doc": rel,
            "source_hash": src_hash,
            "model": model,
            "model_id": model,
            "glossary_version": glossary_version,
            "term_map": doc_term_map,
            "status": status,
            "marker_count": full_translation.count("⟦he:"),
            "unknown_terms": sorted(set(result.get("unknown_terms", []))),
            "fix_rounds_used": fix_used,
        }
        if qa_failures:
            frontmatter["qa_failures"] = qa_failures[:5]
        fm_text = "---\n" + json.dumps(frontmatter, ensure_ascii=False, indent=2) + "\n---\n\n"
        out_file.write_text(fm_text + full_translation, encoding="utf-8")

        # Ledger: fix attempts
        for attempt in result.get("fix_attempts", []):
            evt = {
                "event": "fix_attempt",
                "ts": datetime.now(timezone.utc).isoformat(),
                "source_doc": rel,
                "source_hash": src_hash,
                "model": model,
                "model_id": model,
                "glossary_version": glossary_version,
                "term_map": doc_term_map,
                "round": attempt.get("round"),
                "failures_before": attempt.get("failures_before", [])[:3],
            }
            if "error" in attempt:
                evt["error"] = attempt["error"]
            for k in ("chunked", "src_len", "trans_len", "chunks"):
                if k in attempt:
                    evt[k] = attempt[k]
            # Ensure chunked is always present for schema consistency
            if "chunked" not in evt:
                evt["chunked"] = False
            with open(ledger_path, "a", encoding="utf-8") as lf:
                lf.write(json.dumps(evt, ensure_ascii=False) + "\n")

        # Ledger: qa_result
        qa_event = {
            "event": "qa_result",
            "ts": datetime.now(timezone.utc).isoformat(),
            "source_doc": rel,
            "source_hash": src_hash,
            "model": model,
            "model_id": model,
            "glossary_version": glossary_version,
            "term_map": doc_term_map,
            "status": status,
            "fix_rounds_used": fix_used,
            "qa_failures": qa_failures[:5] if qa_failures else [],
        }
        with open(ledger_path, "a", encoding="utf-8") as lf:
            lf.write(json.dumps(qa_event, ensure_ascii=False) + "\n")

        # Ledger: translation event
        if status == "qa_failed":
            event = {
                "event": "qa_failed",
                "ts": datetime.now(timezone.utc).isoformat(),
                "source_doc": rel,
                "source_hash": src_hash,
                "model": model,
                "model_id": model,
                "glossary_version": glossary_version,
                "term_map": doc_term_map,
                "status": status,
                "marker_count": frontmatter["marker_count"],
                "unknown_terms": frontmatter["unknown_terms"],
                "fix_rounds_used": fix_used,
                "qa_failures": qa_failures[:5],
            }
            failed_docs.append(rel)
            qa_failed += 1
        elif status == "blocked_on_term":
            event = {
                "event": "blocked_on_term",
                "ts": datetime.now(timezone.utc).isoformat(),
                "source_doc": rel,
                "source_hash": src_hash,
                "model": model,
                "model_id": model,
                "glossary_version": glossary_version,
                "term_map": doc_term_map,
                "status": status,
                "marker_count": frontmatter["marker_count"],
                "unknown_terms": frontmatter["unknown_terms"],
                "fix_rounds_used": fix_used,
            }
            blocked += 1
        else:
            event = {
                "event": "translation_completed",
                "ts": datetime.now(timezone.utc).isoformat(),
                "source_doc": rel,
                "source_hash": src_hash,
                "model": model,
                "model_id": model,
                "glossary_version": glossary_version,
                "term_map": doc_term_map,
                "status": status,
                "marker_count": frontmatter["marker_count"],
                "unknown_terms": frontmatter["unknown_terms"],
                "fix_rounds_used": fix_used,
            }
            translated += 1
        with open(ledger_path, "a", encoding="utf-8") as lf:
            lf.write(json.dumps(event, ensure_ascii=False) + "\n")

        # Console
        if status == "qa_failed":
            print(f"  {rel}: qa_failed after {fix_used} fix rounds: {qa_failures[:1]}", file=sys.stderr)
        else:
            chunk_info = f" ({fix_used} fix rounds)" if fix_used else ""
            print(f"  {rel}: {status}{chunk_info} ({frontmatter['marker_count']} markers)")

    # Log name candidates
    if name_candidates:
        cand_path = out_root / "name_candidates.txt"
        with open(cand_path, "w", encoding="utf-8") as f:
            for n in sorted(name_candidates):
                f.write(n + "\n")
        print(f"Name candidates: {len(name_candidates)} unique -> {cand_path}")

    print(f"Done: {translated} completed, {blocked} blocked_on_term, {skipped_english} skipped_english, {qa_failed} qa_failed -> {out_root}")
    if failed_docs:
        print(f"FAILED: {len(failed_docs)} docs still invalid after {fix_rounds} fix rounds: {failed_docs[:5]}", file=sys.stderr)
        print(f"Stop — fix budget exhausted. Inspect QA output and ledger, fix policy/glossary/prompt, retry with --fix-rounds N or --force.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()


# Re-exports for `import translate; translate.X` compat (pure refactor, no new API)
__all__ = [
    "get_ledger_path","load_config","resolve_fix_rounds","resolve_corpus_dir",
    "load_glossary","load_codenames","load_person_names",
    "mask_person_names","unmask_person_names","is_english_only_doc",
    "mask_english_spans","unmask_english_spans",
    "extract_english_spans","extract_urls_and_paths","extract_person_names",
    "extract_yaml_frontmatter","extract_code_sections","extract_preservation_invariants",
    "verify_preserved","verify_all_preserved","verify_ordered","verify_all_ordered","verify_global_order",
    "chunk_markdown","glossary_for_chunk",
    "detect_glossary_terms","mask_glossary_terms","unmask_glossary_terms",
    "build_prompt","build_fix_prompt","_build_chunked_fix_prompts","format_qa_failures",
    "call_llm","mock_translate",
    "run_qa_for_doc","translate_one_doc","translate_one_doc_with_fix","main",
    # constants
    "PERSON_OPEN","PERSON_CLOSE","EN_OPEN","EN_CLOSE","HE_MARKER_FMT",
    "HEBREW_RANGE","PROCLITICS","RAW_WORD_RE","MIXED_SPLIT_RE",
]
