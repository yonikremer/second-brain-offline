#!/usr/bin/env python3
"""MiniMax M2.7 bulk-proposes English for every term in translation_seed.csv.

Input:  data/domain_terms/translation_seed.csv (columns: term,lang,suggested_en,example_doc,
        surface_variants,corpus_count,log_ratio,... — term/lang/suggested_en/example_doc are primary)
        corpus: raw_md/ (rglob *.md) else raw/ — 2-3 real context sentences per term, not invented.
Output: data/domain_terms/glossary_proposed.csv

Config: convert_config.json translation block:
  translation: { base_url, api_key_env, model }
Env: TRANSLATE_BASE_URL / TRANSLATE_API_KEY or QMD_OPENAI_* fallback.
Fail-fast if keys missing (no silent fallback).  --mock for CI without LLM.
Prompt: JSON {term_he, english, keep_source, notes} — keep_source for internal names/part numbers.
Mock: mixed lang reuses suggested_en; otherwise EN_{term} fixture (notes=mock).

CLI:
  python scripts/glossary_translate.py [vault_root] [--input PATH] [--out PATH] [--limit N] [--model ID] [--mock]
  vault_root positional (default ".")
  --input PATH  translation_seed.csv path (default vault/data/domain_terms/translation_seed.csv)
  --out PATH    output glossary_proposed.csv (default vault/data/domain_terms/glossary_proposed.csv)
  --limit N     limit terms (0=all, for smoke tests)
  --model ID    override model id (default translation.model or minimax-m2.7)
  --mock        offline mock (no LLM call, EN_{term} fixture, for CI)

Pure stdlib except optional openai client (urllib fallback included).
"""
from __future__ import annotations

import argparse
import csv
try:
    from translation_common import strip_csv_comments
    from translation_common import read_csv_lines_skip_comments
except ImportError:
    from scripts.translation_common import strip_csv_comments
    from scripts.translation_common import read_csv_lines_skip_comments
import hashlib
import json
import os
import re
import sys
import urllib.request
import urllib.error
from pathlib import Path

# Allow running as scripts/glossary_translate.py
_scripts_dir = os.path.dirname(os.path.abspath(__file__))
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)


def load_config(vault_root: Path) -> dict:
    path = vault_root / "convert_config.json"
    cfg = {}
    if path.exists():
        raw = path.read_text(encoding="utf-8")
        try:
            cfg = json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"ERROR: invalid convert_config.json ({path}): {e}", file=sys.stderr)
            raise RuntimeError(f"invalid convert_config.json: {e}") from e
    return cfg


def resolve_corpus_dir(vault_root: Path) -> Path | None:
    for name in ("raw_md", "raw"):
        p = vault_root / name
        if p.is_dir() and any(p.rglob("*.md")):
            return p
    print(f"WARN: no corpus dir (raw_md/ or raw/ with *.md) under {vault_root} — proceeding without contexts", file=sys.stderr)
    return None


def _strip_md(text: str) -> str:
    # Frontmatter
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            text = text[end + 5:]
    # Images/links/tags
    text = re.sub(r"!\[[^]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return text


def harvest_contexts(corpus_dir: Path, terms: list[str], per_term: int = 3) -> dict[str, list[str]]:
    """Mine 2-3 real sentences per term from corpus (deterministic, no LLM)."""
    # Deduplicate input terms to avoid collapsing term_lower keys
    unique_terms = list(dict.fromkeys(terms))  # preserves order, deduped
    # Build lowercase term -> original term mapping for matching
    term_lower = {t.lower(): t for t in unique_terms}
    # Also map variant surfaces: we'll just search for normalized term substring in body lower
    result: dict[str, list[str]] = {t: [] for t in terms}

    md_files = sorted(corpus_dir.rglob("*.md"))
    for md_file in md_files:
        try:
            raw = md_file.read_text(encoding="utf-8")
        except OSError:
            continue
        body = _strip_md(raw)
        # Split into sentences (heuristic: split on .!? / newline)
        sentences = re.split(r"(?<=[.!?۔؟])\s+|\n+", body)
        for sent in sentences:
            sent = sent.strip()
            if len(sent) < 10 or len(sent) > 400:
                continue
            low = sent.lower()
            for t_lower, t_orig in term_lower.items():
                if len(result[t_orig]) >= per_term:
                    continue
                # Match as substring (terms are n-grams already)
                if t_lower in low:
                    result[t_orig].append(sent)
        # Early exit if all terms filled
        if all(len(v) >= per_term for v in result.values()):
            break

    return result


def _call_llm(base_url: str, api_key: str, model: str, term: str, contexts: list[str]) -> dict:
    """Call OpenAI-compatible /chat/completions, expect JSON object."""
    ctx_block = "\n".join(f"- {s}" for s in contexts) if contexts else "(no context found)"
    prompt = (
        f"You are a Hebrew→English domain glossary translator.\n"
        f"Translate this Hebrew domain term to English.\n"
        f"Term: {term}\n"
        f"Context sentences (real corpus excerpts):\n{ctx_block}\n\n"
        f"Rules:\n"
        f"- Output ONLY JSON: {{\"english\": string, \"keep_source\": bool, \"notes\": string}}\n"
        f"- keep_source=true if this is an internal name, part number, org name, or must stay Hebrew.\n"
        f"- english should be the best English rendering (or empty if keep_source).\n"
        f"- notes: brief rationale or empty.\n"
        f"- Keep person names as-is (do not translate them) — but this glossary is for domain terms, not names.\n"
    )

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
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"LLM HTTP {e.code}") from e
    except Exception as e:
        raise RuntimeError(f"LLM call failed: {e}") from e

    try:
        choices = data.get("choices") if isinstance(data, dict) else None
        choice = choices[0] if isinstance(choices, list) and choices else {}
        if isinstance(choice, dict) and choice.get("finish_reason") == "length":
            raise RuntimeError("LLM response truncated (finish_reason=length)")
        msg = choice.get("message") if isinstance(choice, dict) else None
        content = msg.get("content") if isinstance(msg, dict) else None
        if not content:
            content = data["choices"][0]["message"]["content"]
        obj = json.loads(content)
        return {
            "english": str(obj.get("english", "")).strip(),
            "keep_source": bool(obj.get("keep_source", False)),
            "notes": str(obj.get("notes", "")).strip(),
        }
    except Exception as e:
        raise RuntimeError(f"LLM bad JSON content: {e} — raw: {content[:400]}") from e


def _mock_translate(term: str, lang: str, suggested: str) -> dict:
    if lang == "mixed" and suggested:
        return {"english": suggested, "keep_source": False, "notes": "mock: mixed→en stem"}
    return {"english": f"EN_{term}", "keep_source": False, "notes": "mock"}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Bulk-propose glossary English via MiniMax M2.7")
    ap.add_argument("vault_root", nargs="?", default=".", help="vault root (has raw/raw_md, convert_config.json)")
    ap.add_argument("--input", dest="input_csv", default=None, help="translation_seed.csv path")
    ap.add_argument("--out", dest="out_csv", default=None, help="output glossary_proposed.csv")
    ap.add_argument("--mock", action="store_true", help="offline mock (no LLM call, for CI)")
    ap.add_argument("--model", default=None, help="override model id")
    ap.add_argument("--limit", type=int, default=0, help="limit terms (0=all, for smoke tests)")
    args = ap.parse_args(argv)

    vault_root = Path(args.vault_root).resolve()
    cfg = load_config(vault_root)
    tcfg = cfg.get("translation", {})

    base_url = os.environ.get("TRANSLATE_BASE_URL") or os.environ.get("QMD_OPENAI_BASE_URL") or tcfg.get("base_url", "")
    api_key = os.environ.get("TRANSLATE_API_KEY") or os.environ.get("QMD_OPENAI_API_KEY") or ""
    # api_key may be optional for some gateways; base_url is required for non-mock
    model = args.model or tcfg.get("model") or os.environ.get("TRANSLATE_MODEL") or "minimax-m2.7"

    input_csv = Path(args.input_csv) if args.input_csv else vault_root / "data" / "domain_terms" / "translation_seed.csv"
    if not input_csv.exists():
        print(f"ERROR: translation_seed.csv not found: {input_csv}", file=sys.stderr)
        print("Run: python scripts/extract_domain_terms.py <vault>", file=sys.stderr)
        sys.exit(1)

    out_csv = Path(args.out_csv) if args.out_csv else vault_root / "data" / "domain_terms" / "glossary_proposed.csv"
    out_csv.parent.mkdir(parents=True, exist_ok=True)

    if not args.mock and not base_url:
        print("ERROR: translation base_url missing. Set TRANSLATE_BASE_URL or QMD_OPENAI_BASE_URL or convert_config.json translation.base_url", file=sys.stderr)
        sys.exit(1)
    # api_key may be empty for no-auth gateways; call_llm handles it (auth header with empty key is ok)

    # Read seed (strip # comment / empty lines like check_glossary)
    seed_rows: list[dict] = []
    text = input_csv.read_text(encoding="utf-8")
    lines = strip_csv_comments(text)
    if not lines:
        print("translation_seed.csv has no rows — nothing to translate (empty corpus or all terms filtered)", file=sys.stderr)
        out_csv.parent.mkdir(parents=True, exist_ok=True)
        with open(out_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["term_he", "english", "keep_source", "notes", "status", "example_doc", "context_snippets", "lang", "model"])
            w.writeheader()
        print(f"Wrote header-only glossary to {out_csv}")
        sys.exit(0)
    reader = csv.DictReader(lines)
    for row in reader:
        seed_rows.append(row)
    if not seed_rows:
        print("translation_seed.csv has no rows — nothing to translate (empty corpus or all terms filtered)", file=sys.stderr)
        # Still write header-only output so downstream doesn't break
        out_csv.parent.mkdir(parents=True, exist_ok=True)
        with open(out_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["term_he", "english", "keep_source", "notes", "status", "example_doc", "context_snippets", "lang", "model"])
            w.writeheader()
        print(f"Wrote header-only glossary to {out_csv}")
        sys.exit(0)
    if args.limit:
        seed_rows = seed_rows[:args.limit]

    terms = [r["term"] for r in seed_rows]
    print(f"Glossary translate: {len(seed_rows)} terms from {input_csv}")

    # Harvest contexts
    corpus_dir = resolve_corpus_dir(vault_root)
    context_map: dict[str, list[str]] = {}
    if corpus_dir is not None:
        print(f"Harvesting contexts from {corpus_dir} ...")
        context_map = harvest_contexts(corpus_dir, terms, per_term=3)
        with_ctx = sum(1 for v in context_map.values() if v)
        print(f"  {with_ctx}/{len(terms)} terms have context sentences")
    else:
        print("  no corpus dir found — translating without contexts")
        context_map = {t: [] for t in terms}

    # Call LLM per term
    out_rows: list[dict] = []
    for row in seed_rows:
        term = row["term"]
        lang = row.get("lang", "")
        suggested = row.get("suggested_en", "")
        example_doc = row.get("example_doc", "")
        contexts = context_map.get(term, [])
        if args.mock:
            res = _mock_translate(term, lang, suggested)
        else:
            res = _call_llm(base_url, api_key, model, term, contexts)
        out_rows.append({
            "term_he": term,
            "english": res["english"],
            "keep_source": "1" if res["keep_source"] else "0",
            "notes": res["notes"],
            "status": "proposed",
            "example_doc": example_doc,
            "context_snippets": " | ".join(contexts[:2]),
            "lang": lang,
            "model": model,
        })
        if not args.mock:
            print(f"  {term} -> {res['english']!r} keep_source={res['keep_source']}")

    fieldnames = ["term_he", "english", "keep_source", "notes", "status", "example_doc", "context_snippets", "lang", "model"]
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(out_rows)
    print(f"Wrote {len(out_rows)} rows to {out_csv}")

    # Also echo version hash hint
    h = hashlib.sha256(out_csv.read_bytes()).hexdigest()[:10]
    print(f"hash: {h}")


if __name__ == "__main__":
    main()
