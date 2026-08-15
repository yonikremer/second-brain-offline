#!/usr/bin/env python3
"""Sampled reviewer: glossary consistency + AskQE + structure spot-check.

Uses Kimi K2.7 (limited budget) at ~10-20% sampling. Flags, not auto-rejections.
Output: data/translations/review_report.json + per-flag cards under review_cards/.

CLI:
  python scripts/translation_reviewer.py [store_dir] [--vault-root PATH] [--glossary PATH] [--sample 0.2] [--seed 0] [--mock] [--out PATH]
  store_dir positional (default data/translations)
  --vault-root PATH   vault root (for convert_config.json + glossary resolution)
  --glossary PATH     glossary.csv override (default vault/data/domain_terms/glossary.csv)
  --sample 0.2        sampling rate 0..1 (fractions via random.sample)
  --seed 0            random seed for deterministic sampling
  --mock              offline mock (no LLM, only glossary + structure sweep)
  --out PATH          review_report.json path (default store_dir/review_report.json)

Config: convert_config.json translation.reviewer_model + TRANSLATE_REVIEWER_* / QMD_OPENAI_*.
Base URL inheritance (highest precedence first):
  TRANSLATE_REVIEWER_BASE_URL → TRANSLATE_BASE_URL → QMD_OPENAI_BASE_URL → translation.reviewer_base_url → translation.base_url
Glossary consistency is marker-only: flags unresolved ⟦he:term⟧ for approved terms
  appearing in >=2 docs; divergent-English detection TODO (requires per-occurrence variant comparison).
Sampling is random.seed(seed) + random.sample deterministic.
AskQE prompt shape: "generate 2 factual questions that translation should answer, {questions:[{q,a,answerable}]}"; unanswerable flags dropped content.

--mock for CI (no LLM).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import sys
import urllib.request
import urllib.error
from pathlib import Path

# Shared helpers — translation_common is single source of truth
try:
    from translation_common import read_csv_lines_skip_comments as _shared_read_csv, strip_frontmatter as _shared_strip_fm
    _USE_SHARED = True
except ImportError:
    try:
        from scripts.translation_common import read_csv_lines_skip_comments as _shared_read_csv, strip_frontmatter as _shared_strip_fm
        _USE_SHARED = True
    except ImportError:
        _USE_SHARED = False

HE_MARKER_RE = re.compile(r"⟦he:[^⟧]+⟧")


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


def _strip_frontmatter(text: str) -> tuple[str, str]:
    """Via translation_common (single source of truth)."""
    if _USE_SHARED:
        return _shared_strip_fm(text)
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            return text[: end + 5], text[end + 5:]
    return "", text


def _load_translation(path: Path) -> tuple[dict, str]:
    raw = path.read_text(encoding="utf-8")
    fm, body = _strip_frontmatter(raw)
    meta: dict = {}
    if fm:
        inner = fm.strip()[4:-4].strip() if "---" in fm.strip() else fm
        try:
            meta = json.loads(inner)
        except Exception:
            pass
    return meta, body


def _read_csv_skip_comments(path: Path) -> list[str]:
    """Strip # comment and empty lines before DictReader (matches check_glossary) — via translation_common."""
    if _USE_SHARED:
        return _shared_read_csv(path)
    text = path.read_text(encoding="utf-8")
    return [l for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]


def load_glossary_terms(glossary_path: Path) -> dict[str, str]:
    """term_he -> english (approved only)."""
    terms: dict[str, str] = {}
    if glossary_path.exists():
        lines = _read_csv_skip_comments(glossary_path)
        if not lines:
            return terms
        reader = csv.DictReader(lines)
        if reader.fieldnames:
            for row in reader:
                if (row.get("status") or "").strip() not in ("approved", "keep_source"):
                    continue
                he = (row.get("term_he") or "").strip()
                en = (row.get("english") or "").strip()
                if he and en:
                    terms[he] = en
    return terms


def glossary_consistency(translations: list[tuple[Path, str]], glossary_terms: dict[str, str]) -> list[dict]:
    """Flag leftover ⟦he:...⟧ markers for approved terms (not divergent-English detection).

    Currently only detects approved glossary terms still present as unresolved
    markers in multiple docs. Detecting divergent English renderings of the
    same Hebrew term across docs is TODO — requires storing the chosen English
    per occurrence and comparing variants.

    Config: translation.base_url is the canonical key; reviewer inherits it
    unless translation.reviewer_base_url (or TRANSLATE_REVIEWER_BASE_URL env)
    is explicitly set (see main()).
    """
    flags: list[dict] = []
    # For each glossary term, collect how it was rendered across translations
    # Heuristic: check if glossary English appears where Hebrew would have been
    # Simplified: flag if same English gloss appears inconsistently (e.g. two variants)
    # For now: only detect if marker still present for a term that has an approved gloss
    for he, en in glossary_terms.items():
        docs_with_marker = []
        for path, body in translations:
            # Multi-word terms are marked per-word (⟦he:בינה⟧ ⟦he:מלאכותית⟧), so check all tokens
            if " " in he:
                tokens = he.split()
                single_marker = f"⟦he:{he}⟧"
                per_word_present = all(f"⟦he:{tok}⟧" in body for tok in tokens)
                if per_word_present or single_marker in body or f"⟦he:{he}" in body:
                    docs_with_marker.append(path.name)
            else:
                if f"⟦he:{he}⟧" in body or f"⟦he:{he}" in body:
                    docs_with_marker.append(path.name)
        if len(docs_with_marker) >= 2:
            flags.append({
                "type": "glossary_consistency",
                "term_he": he,
                "expected_en": en,
                "note": f"{len(docs_with_marker)} docs still have unresolved marker for approved term",
                "docs": docs_with_marker[:5],
            })
    return flags


def call_llm(base_url: str, api_key: str, model: str, prompt: str) -> dict:
    url = base_url.rstrip("/") + "/chat/completions"
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }).encode()
    req = urllib.request.Request(url, data=payload, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode())
        choices = data.get("choices") if isinstance(data, dict) else None
        choice = choices[0] if isinstance(choices, list) and choices else {}
        if isinstance(choice, dict) and choice.get("finish_reason") == "length":
            raise RuntimeError("reviewer LLM response truncated (finish_reason=length)")
        msg = choice.get("message") if isinstance(choice, dict) else None
        content = msg.get("content") if isinstance(msg, dict) else None
        if not content:
            content = data["choices"][0]["message"]["content"]
        return json.loads(content)
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"reviewer LLM call failed: {e}") from e


def askqe_check(trans_body: str, base_url: str, api_key: str, model: str) -> list[dict]:
    """AskQE-style: generate Q from translation, answer divergence = dropped content.
    For mock: skip. For real: call LLM to generate 2 questions, check if answerable.
    Simplified: LLM self-check for completeness.
    """
    prompt = (
        "You are a translation quality reviewer. Given this English translation of a Hebrew document,\n"
        "generate 2 factual questions that the translation should answer. Then answer them from the translation.\n"
        "Output JSON: {\"questions\": [{\"q\": string, \"a\": string, \"answerable\": bool}]}\n\n"
        f"Translation (first 3000 chars):\n{trans_body[:3000]}\n"
    )
    try:
        obj = call_llm(base_url, api_key, model, prompt)
        flags = []
        for item in obj.get("questions", []):
            if not item.get("answerable", True):
                flags.append({
                    "type": "askqe",
                    "question": item.get("q", ""),
                    "note": "unanswerable from translation — possible dropped content",
                })
        return flags
    except Exception as e:
        return [{"type": "askqe_error", "note": str(e)[:300]}]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Sampled translation reviewer (Kimi K2.7)")
    ap.add_argument("store_dir", type=Path, nargs="?", default=Path("data/translations"))
    ap.add_argument("--vault-root", type=Path, default=Path("."), help="vault root")
    ap.add_argument("--glossary", type=Path, default=None)
    ap.add_argument("--sample", type=float, default=0.2, help="sampling rate 0..1")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--mock", action="store_true", help="offline mock (no LLM, only glossary sweep)")
    ap.add_argument("--out", type=Path, default=None, help="review_report.json path")
    args = ap.parse_args(argv)

    vault_root = Path(args.vault_root).resolve()
    cfg = load_config(vault_root)
    tcfg = cfg.get("translation", {})
    store_dir = Path(args.store_dir)
    if not store_dir.is_absolute():
        # If store_dir looks like data/translations relative to vault, resolve vs vault
        if (vault_root / store_dir).exists() or str(store_dir).startswith("data/"):
            store_dir = vault_root / store_dir
        else:
            store_dir = vault_root / store_dir

    if args.glossary is not None:
        glossary_path = Path(args.glossary)
        if not glossary_path.is_absolute():
            glossary_path = vault_root / glossary_path
    else:
        glossary_path = vault_root / "data" / "domain_terms" / "glossary.csv"
        # Fallback: if glossary.csv missing but glossary_proposed exists (pre-approval)
        if not glossary_path.exists():
            alt = vault_root / "data" / "domain_terms" / "glossary_proposed.csv"
            if alt.exists():
                glossary_path = alt

    glossary_terms = load_glossary_terms(glossary_path)
    print(f"Glossary: {len(glossary_terms)} approved terms from {glossary_path}")

    base_url = os.environ.get("TRANSLATE_REVIEWER_BASE_URL") or os.environ.get("TRANSLATE_BASE_URL") or os.environ.get("QMD_OPENAI_BASE_URL") or tcfg.get("reviewer_base_url") or tcfg.get("base_url", "")
    api_key = os.environ.get("TRANSLATE_REVIEWER_API_KEY") or os.environ.get("TRANSLATE_API_KEY") or os.environ.get("QMD_OPENAI_API_KEY") or ""
    reviewer_model = tcfg.get("reviewer_model") or os.environ.get("TRANSLATE_REVIEWER_MODEL") or "kimi-k2.7"

    if not args.mock and not base_url:
        print("ERROR: reviewer base_url missing. Set TRANSLATE_REVIEWER_BASE_URL or TRANSLATE_BASE_URL", file=sys.stderr)
        sys.exit(1)

    translations: list[tuple[Path, str]] = []
    for p in sorted(store_dir.rglob("translation.md")):
        meta, body = _load_translation(p)
        translations.append((p, body))

    if not translations:
        print(f"No translations found under {store_dir}", file=sys.stderr)
        sys.exit(1)

    # Sampling
    random.seed(args.seed)
    if args.sample < 1.0:
        k = max(1, int(len(translations) * args.sample))
        sampled = random.sample(translations, min(k, len(translations)))
    else:
        sampled = translations
    print(f"Reviewing {len(sampled)}/{len(translations)} translations (sample={args.sample}, model={reviewer_model})")

    flags: list[dict] = []

    # 1) Glossary consistency sweep (deterministic, no LLM)
    flags.extend(glossary_consistency(translations, glossary_terms))
    print(f"  glossary_consistency: {len([f for f in flags if f['type']=='glossary_consistency'])} flags")

    # 2) Structure spot-check on sampled set
    for path, body in sampled:
        fences = body.count("```")
        if fences % 2 != 0:
            flags.append({"type": "structure", "file": path.relative_to(store_dir).as_posix(), "note": "orphaned fence"})

    # 3) AskQE on sampled set (LLM)
    if not args.mock:
        for path, body in sampled:
            rel = path.relative_to(store_dir).as_posix()
            q_flags = askqe_check(body, base_url, api_key, reviewer_model)
            for f in q_flags:
                f["file"] = rel
                flags.append(f)
        print(f"  AskQE + structure: {len([f for f in flags if f['type'] in ('askqe','structure')])} flags")
    else:
        print("  AskQE: skipped (--mock)")

    out_path = args.out or store_dir / "review_report.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "sample": args.sample,
        "total_translations": len(translations),
        "sampled": len(sampled),
        "reviewer_model": reviewer_model,
        "flags": flags,
        "flag_count": len(flags),
    }
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {len(flags)} flags to {out_path}")

    # Per-flag cards (optional, for queue integration)
    if flags:
        cards_dir = store_dir / "review_cards"
        cards_dir.mkdir(exist_ok=True)
        for i, f in enumerate(flags):
            card = cards_dir / f"flag-{i:03d}.md"
            card.write_text(f"# Flag {i}\n\n```json\n{json.dumps(f, ensure_ascii=False, indent=2)}\n```\n", encoding="utf-8")


if __name__ == "__main__":
    main()
