"""Prompt builders — extracted from translate.py (pure move)."""
from __future__ import annotations

import json
import re

from .translation_common import _valid_translation_option, _filter_translations
from .translation_chunking import chunk_markdown, glossary_for_chunk
from .translation_invariants import extract_preservation_invariants






def build_prompt(chunk_text: str, section_path: str, glossary_rows: list[dict],
                 prev_tail: str = "", invariants: dict | None = None, term_map: list[dict] | None = None,
                 previous_choices: dict[str, str] | None = None) -> str:
    glossary_block = ""
    if term_map is not None:
        if term_map:
            lines: list[str] = []
            for e in term_map:
                term_he = e.get("term_he", "") or ""
                translations = _filter_translations(e.get("translations"))
                occ = e.get("occurrences", 1)
                keep = e.get("keep_source", False)
                is_keep = keep is True or keep == 1 or str(keep) == "1"
                if is_keep:
                    lines.append(f"  - \"{term_he}\" → KEEP exactly as \"{term_he}\" (appears {occ}×) — do not translate")
                elif translations:
                    # Small-model-friendly: explicit choice list with quoted options
                    opts_display = " OR ".join(f'"{o}"' for o in translations)
                    lines.append(f"  - \"{term_he}\" → choose {opts_display} (appears {occ}×)")
            if lines:
                glossary_block = "Glossary — REQUIRED. For each Hebrew term below, you MUST use exactly one of the listed English options. Copy the option character-for-character:\n" + "\n".join(lines) + "\n\n"
    elif glossary_rows:
        lines = []
        for r in glossary_rows:
            term = r.get("term_he", "")
            translations = _filter_translations(r.get("translations"))
            ks = r.get("keep_source", "0")
            is_keep = ks == "1" or ks is True or str(ks) == "1" or r.get("status") == "keep_source"
            if is_keep:
                lines.append(f"\"{term}\"→KEEP \"{term}\"")
            elif translations:
                opts = " OR ".join(f'"{o}"' for o in translations)
                lines.append(f"\"{term}\"→{opts}")
            # invalid-only terms are dropped entirely — do not emit "translate per context"
        if lines:
            glossary_block = "Glossary — REQUIRED. Use exactly one allowed translation per term:\n" + "\n".join(f"  - {l}" for l in lines) + "\n\n"

    prev_block = ""
    if prev_tail:
        prev_block = f"Previous chunk tail (context only, do not re-emit):\n{prev_tail[:400]}\n\n"

    # previous_choices is kept for back-compat but ignored (mixing allowed — no consistency block emitted)
    _ = previous_choices

    # Preservation context — trimmed for small models (≤12 per category, shorter preview)
    preserve_block = ""
    if invariants:
        parts: list[str] = []
        for cat, label in [("yaml_frontmatter", "YAML frontmatter (copy exactly)"),
                           ("code_sections", "Code blocks (copy exactly)"),
                           ("person_names", "Person names (copy exactly)"),
                           ("english_spans", "English terms (copy exactly)"),
                           ("urls_and_paths", "URLs/paths (copy exactly)")]:
            items = invariants.get(cat) or []
            if items:
                shown = items[:12]
                def _short(s: str) -> str:
                    # Keep preview short — 120 chars is enough for model to recognise
                    return s[:120] + ("…" if len(s) > 120 else "")
                shown_short = [_short(s) for s in shown]
                parts.append(f"{label}: {json.dumps(shown_short, ensure_ascii=False)}")
                if len(items) > 12:
                    parts[-1] += f" (+{len(items)-12} more)"
        if parts:
            preserve_block = "MUST preserve verbatim (copy exactly, keep order):\n" + "\n".join(f"- {p}" for p in parts) + "\n\n"

    rules_block = (
        f"You are a Hebrew→English translator. Translate ONLY the Chunk below.\n"
        f"RULES (follow all):\n"
        f"1. GLOSSARY: For each term above, use EXACTLY one listed option, same spelling/case. Never invent another.\n"
        f"2. If a word is unknown (not in glossary and not everyday Hebrew), emit ⟦he:word⟧ and list it in unknown_terms.\n"
        f"3. PRESERVE: Copy every item under 'MUST preserve' exactly, same order. Do not translate or reformat them.\n"
        f"4. MARKDOWN: Keep headings (#), lists (-/*), tables (|), code fences (```) counts and order identical.\n"
        f"5. DELIMITERS: If you see ⟦SEG⟧ or ⟦CELL⟧, you MUST keep every occurrence and count. Never add or remove one.\n"
        f"6. Output JSON only: {{\"translation\": \"...\", \"unknown_terms\": [], \"notes\": []}}\n"
        f"   Example: {{\"translation\": \"# Title\\nTranslated paragraph.\", \"unknown_terms\": [], \"notes\": []}}\n\n"
    )

    return (
        f"{rules_block}"
        f"{glossary_block}"
        f"{preserve_block}"
        f"{prev_block}"
        f"Section: {section_path}\n\n"
        f"Chunk to translate:\n{chunk_text}\n"
    )


def format_qa_failures(checks: list[dict]) -> list[dict]:
    """Filter QA checks to failures only (status==fail)."""
    return [c for c in checks if c.get("status") == "fail"]


def build_fix_prompt(source_text: str, prev_translation: str, failures: list[dict],
                     glossary_rows: list[dict] | None = None,
                     invariants: dict | None = None, term_map: list[dict] | None = None,
                     previous_choices: dict[str, str] | None = None) -> str:
    """Prompt for LLM to repair previous translation given QA failures."""
    src_cap = source_text[:12000]
    if len(source_text) > 12000:
        src_cap += f"\n…(truncated {len(source_text) - 12000} chars omitted — chunked fix should have been used)"
    prev_cap = prev_translation[:12000]
    if len(prev_translation) > 12000:
        prev_cap += f"\n…(truncated {len(prev_translation) - 12000} chars omitted — chunked fix should have been used)"
    _full_failure = json.dumps(failures, ensure_ascii=False, indent=2)
    failure_block = _full_failure[:6000]
    if len(_full_failure) > 6000:
        failure_block += "\n…(truncated)"
    glossary_block = ""
    if term_map is not None:
        if term_map:
            lines: list[str] = []
            for e in term_map[:20]:
                term_he = e.get("term_he", "") or ""
                translations = _filter_translations(e.get("translations"))
                occ = e.get("occurrences", 1)
                keep = e.get("keep_source", False)
                is_keep = keep is True or keep == 1 or str(keep) == "1"
                if is_keep:
                    lines.append(f"  [{term_he}:KEEP as Hebrew] (appears {occ}×)")
                elif translations:
                    opts = "|".join(translations)
                    lines.append(f"  [{term_he}:{opts}] (appears {occ}×)")
            if lines:
                glossary_block = "Glossary (use one of the allowed translations):\n" + "\n".join(lines)
                if len(term_map) > 20:
                    glossary_block += f"\n(+{len(term_map) - 20} more)"
                glossary_block += "\n\n"
    elif glossary_rows:
        lines = []
        for r in glossary_rows[:20]:
            term = r.get("term_he", "")
            translations = _filter_translations(r.get("translations"))
            if term and translations:
                opts = "|".join(translations)
                lines.append(f"[{term}:{opts}]")
        if lines:
            glossary_block = "Glossary (must use exactly one of the allowed):\n" + "\n".join(lines)
            if glossary_rows and len(glossary_rows) > 20:
                glossary_block += f"\n(+{len(glossary_rows) - 20} more)"
            glossary_block += "\n\n"
    # previous_choices is kept for back-compat but ignored (mixing allowed)
    _ = previous_choices
    invariants_block = ""
    if invariants:
        parts = []
        for cat, items in invariants.items():
            if items:
                shown = items[:10]
                part = f"{cat}: {json.dumps(shown, ensure_ascii=False)}"
                if len(items) > 10:
                    part += f" (+{len(items) - 10} more)"
                parts.append(part)
        if parts:
            invariants_block = "Preserve verbatim in order:\n" + "\n".join(f"- {p}" for p in parts) + "\n\n"
    return (
        "You are repairing a Hebrew→English markdown translation that FAILED scripted QA checks.\n"
        "Fix ONLY the reported failures. Keep everything else identical.\n"
        "Rules:\n"
        "- For each glossary term, use exactly one of its allowed translations — do not invent alternatives.\n"
        "- Choose the translation that best fits the surrounding context; you may use different allowed renderings for different occurrences.\n"
        "- Preserve headings, lists, tables, code fences exactly (same counts) and in order.\n"
        "- Person names, English/URLs/code/YAML below must be copied verbatim and in order.\n"
        "- Never invent translations for unknown terms — use ⟦he:term⟧ and list in unknown_terms.\n"
        "- Output JSON: {\"translation\": string, \"unknown_terms\": [string], \"notes\": [string]}\n\n"
        f"{glossary_block}"
        f"{invariants_block}"
        f"QA failures to fix:\n{failure_block}\n\n"
        f"Original Hebrew source:\n{src_cap}\n\n"
        f"Previous translation (to repair):\n{prev_cap}\n"
    )


def _build_chunked_fix_prompts(source_text: str, prev_translation: str, failures: list[dict],
                               glossary_rows: list[dict] | None, invariants: dict | None,
                               chunk_chars: int,
                               first_names: set[str] | None = None, last_names: set[str] | None = None,
                               previous_choices: dict[str, str] | None = None) -> list[str]:
    """Split large-doc fix into per-chunk prompts to avoid 12k truncation loss.

    previous_choices is kept for back-compat but is a no-op (mixing allowed).
    """
    src_chunks = chunk_markdown(source_text, max_chars=chunk_chars)
    prev_chunks = chunk_markdown(prev_translation, max_chars=chunk_chars) if prev_translation.strip() else []
    n = max(len(src_chunks), len(prev_chunks), 1)
    fn = first_names if first_names is not None else set()
    ln = last_names if last_names is not None else set()
    prompts: list[str] = []
    for i in range(n):
        src = src_chunks[i]["chunk_text"] if i < len(src_chunks) else ""
        prev = prev_chunks[i]["chunk_text"] if i < len(prev_chunks) else ""
        section = src_chunks[i].get("section_path", "") if i < len(src_chunks) else f"chunk {i+1}/{n}"
        # Per-chunk filtering: only inject glossary terms that occur in this chunk
        if src and glossary_rows:
            cg = glossary_for_chunk(src, glossary_rows)
            chunk_glossary: list[dict] | None = cg  # [] means no terms in this chunk — keep empty
        elif src:
            chunk_glossary = None
        else:
            chunk_glossary = glossary_rows
        # Chunk-specific invariants with real person-name allowlist
        chunk_invariants = extract_preservation_invariants(src, fn, ln) if src else None
        if chunk_invariants is None:
            chunk_invariants = invariants
            if chunk_glossary is None:
                chunk_glossary = glossary_rows
        p = build_fix_prompt(src, prev, failures, chunk_glossary, chunk_invariants, previous_choices=previous_choices)
        # Annotate that failures are global — model should only fix those affecting its chunk
        global_note = "Note: QA failures above are global for the whole document — fix only those that affect your chunk's section, keep rest identical.\n\n"
        p = f"Chunk {i+1}/{n} — Section: {section}\n{global_note}" + p
        prompts.append(p)
    return prompts
