#!/usr/bin/env python3
"""Translate Hebrew markdown chunks to English with glossary + name guard.

- Structural chunking at heading/paragraph boundaries (never mid-sentence/table/code).
- Filtered glossary: only terms occurring in chunk are injected.
- Person-name guard: exact-match against data/person_names/ (592 first + 818 last),
  masked to ⟦PERSON_n⟧ before LLM, unmasked after.
- Structured output {translation, unknown_terms, notes} via response_format=json_object.
- Zero-guessing: unknown terms → ⟦he:<term>⟧ markers, blocked_on_term ledger.
- Content-addressed store data/translations/<sha>/translation.md + ledger.jsonl.
- Bounded retries (max 3).

Config: convert_config.json translation block:
  translation {base_url (\"\"), reviewer_base_url (\"\"), api_key_env (TRANSLATE_API_KEY),
               model (minimax-m2.7), reviewer_model (kimi-k2.7), chunk_chars (6000),
               review_sample (0.2), glossary_path (data/domain_terms/glossary.csv)}
  Defaults: base_url \"\", reviewer_base_url \"\", chunk_chars 6000, review_sample 0.2,
            glossary_path data/domain_terms/glossary.csv, model minimax-m2.7,
            reviewer_model kimi-k2.7, api_key_env TRANSLATE_API_KEY.
  Env precedence: TRANSLATE_BASE_URL primary, QMD_OPENAI_BASE_URL fallback;
  reviewer uses TRANSLATE_REVIEWER_BASE_URL override (see translation_reviewer.py).
Fail-fast if base_url missing. --mock for CI (mock is PERSON-sentinel aware: splits by
  ⟦PERSON_n⟧, only wraps remaining [א-ת]{2,} as ⟦he:…⟧ so sentinels are not marked).

CLI:
  python scripts/translate.py [vault_root] [--input DIR] [--glossary PATH] [--out DIR]
                              [--check] [--mock] [--force] [--resume] [--limit N]
  vault_root positional (default ".")
  --input DIR     corpus dir (default raw_md/raw auto-detect)
  --glossary PATH glossary.csv override (default translation.glossary_path or vault/data/domain_terms/glossary.csv)
  --out DIR       output store dir (default vault/data/translations, canonical ledger vault/data/translations/ledger.jsonl)
  --check         only check glossary gate, exit 1 if blocked
  --mock          offline mock (glossary substitution + sentinel-aware Hebrew marking)
  --force         retranslate even if cached (content-addressed <sha> already exists)
  --resume        same as default (resume by hash, kept for docs compat)
  --limit N       limit files (0=all)
"""
from __future__ import annotations

import argparse
import csv
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

_scripts_dir = os.path.dirname(os.path.abspath(__file__))
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

PERSON_OPEN = "⟦PERSON_"
PERSON_CLOSE = "⟧"
HE_MARKER_FMT = "⟦he:{term}⟧"

# Hebrew char range (narrow א-ת) — word runs used for mock/qa sentinel-aware marking
HEBREW_WORD_RE = re.compile(r"[א-ת]{2,}")


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
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


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
    """Read CSV text stripping # comment and empty lines (matches check_glossary)."""
    text = path.read_text(encoding="utf-8")
    return [l for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]


def load_glossary(glossary_path: Path) -> list[dict]:
    if not glossary_path.exists():
        return []
    lines = _read_csv_skip_comments(glossary_path)
    if not lines:
        return []
    reader = csv.DictReader(lines)
    if not reader.fieldnames:
        return []
    return list(reader)


def load_person_names(vault_root: Path) -> tuple[set[str], set[str]]:
    first_p = vault_root / "data" / "person_names" / "first_names.txt"
    last_p = vault_root / "data" / "person_names" / "last_names_ranked.txt"
    if not last_p.exists():
        last_p = vault_root / "data" / "person_names" / "last_names.txt"
    first: set[str] = set()
    last: set[str] = set()
    for p, s in [(first_p, first), (last_p, last)]:
        if p.exists():
            try:
                for line in p.read_text(encoding="utf-8").splitlines():
                    t = line.strip()
                    if t:
                        s.add(t)
            except OSError:
                pass
    return first, last


def mask_person_names(text: str, first: set[str], last: set[str]) -> tuple[str, list[str]]:
    """Exact-match scan for person names (unigram + bigram), mask to sentinels.

    Token-boundary safe: replaces whole Hebrew tokens only, never substrings
    inside a longer word (e.g. 'דן' inside 'דניאל').
    """
    tokens = re.findall(r"[א-ת]{2,}", text)
    token_set = set(tokens)

    words = re.findall(r"[א-ת]+", text)
    bigram_candidates: set[str] = set()
    for i in range(len(words) - 1):
        bg = f"{words[i]} {words[i+1]}"
        bigram_candidates.add(bg)

    single_names: set[str] = {t for t in token_set if t in first or t in last}
    bigram_names: set[str] = set()
    for bg in bigram_candidates:
        parts = bg.split()
        if len(parts) == 2 and parts[0] in first and parts[1] in last:
            bigram_names.add(bg)

    # If a bigram was matched, its component tokens must not also be listed as singles
    if bigram_names:
        bigram_tokens: set[str] = set()
        for bg in bigram_names:
            bigram_tokens.update(bg.split())
        single_names -= bigram_tokens

    all_names = sorted(single_names | bigram_names, key=len, reverse=True)
    if not all_names:
        return text, []

    name_to_sentinel: dict[str, str] = {}
    mapping: list[str] = []
    for name in all_names:
        sentinel = f"{PERSON_OPEN}{len(mapping)}{PERSON_CLOSE}"
        name_to_sentinel[name] = sentinel
        mapping.append(name)

    masked = _mask_via_tokens(text, name_to_sentinel)
    return masked, mapping


def _mask_via_tokens(text: str, name_to_sentinel: dict[str, str]) -> str:
    """Replace names at token boundaries by scanning Hebrew word spans."""
    bigram_set = {k for k in name_to_sentinel if " " in k}
    single_set = {k for k in name_to_sentinel if " " not in k}

    he_spans = [(m.start(), m.end(), m.group(0)) for m in re.finditer(r"[א-ת]+", text)]
    if not he_spans:
        return text

    skip: set[int] = set()
    replacements: dict[int, str] = {}

    # Bigram pass first (longer matches win)
    for i in range(len(he_spans) - 1):
        if i in skip:
            continue
        bg = f"{he_spans[i][2]} {he_spans[i + 1][2]}"
        if bg in bigram_set:
            sep = text[he_spans[i][1]: he_spans[i + 1][0]]
            if sep in ("", " ", "\t", "\n", "־", "-", "־"):
                replacements[i] = name_to_sentinel[bg]
                skip.add(i + 1)

    # Single pass for remaining tokens
    for i, (_, _, tok) in enumerate(he_spans):
        if i in skip or i in replacements:
            continue
        if tok in single_set:
            replacements[i] = name_to_sentinel[tok]

    if not replacements:
        return text

    # Rebuild via offset scan
    result: list[str] = []
    cur = 0
    i = 0
    while i < len(he_spans):
        s, e, _tok = he_spans[i]
        if i in replacements:
            result.append(text[cur:s])
            result.append(replacements[i])
            if (i + 1) in skip:
                cur = he_spans[i + 1][1]
                i += 2
            else:
                cur = e
                i += 1
        elif i in skip:
            i += 1
        else:
            i += 1
    result.append(text[cur:])
    return "".join(result)


def unmask_person_names(text: str, mapping: list[str]) -> str:
    for i, name in enumerate(mapping):
        text = text.replace(f"{PERSON_OPEN}{i}{PERSON_CLOSE}", name)
    return text


def chunk_markdown(md_text: str, max_chars: int = 6000) -> list[dict]:
    """Split at heading boundaries, then paragraph boundaries if chunk exceeds budget.
    Never mid-code-block / mid-frontmatter / mid-table (table handled as paragraph).
    Returns [{section_path, chunk_text}].

    Note: kept heading→paragraph aligned with how qmd chunks markdown for
    embedding (qmd's chunking is internal: AST for code + heading-aware for
    markdown, not exposed as a library). If qmd exposes a chunk API later,
    wire it here — same boundaries keep translation chunks = retrieval chunks.
    """
    # Separate frontmatter
    body = md_text
    frontmatter = ""
    if md_text.startswith("---\n"):
        end = md_text.find("\n---\n", 4)
        if end != -1:
            frontmatter = md_text[: end + 5]
            body = md_text[end + 5:]

    # Track code fence state so we never split inside one
    lines = body.split("\n")
    sections: list[dict] = []
    cur_lines: list[str] = []
    cur_heading = ""
    in_fence = False

    def flush():
        nonlocal cur_lines, cur_heading
        if cur_lines:
            # Trim
            text = "\n".join(cur_lines).strip()
            if text:
                sections.append({"section_path": cur_heading, "chunk_text": text})
            cur_lines = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            cur_lines.append(line)
            continue
        if not in_fence and re.match(r"^#{1,6}\s+", line):
            # Heading boundary — flush previous section
            flush()
            cur_heading = line.strip()
            cur_lines.append(line)
        else:
            cur_lines.append(line)
            # Check if current buffer exceeds budget and we're at a paragraph boundary
            if not in_fence and sum(len(l) for l in cur_lines) > max_chars:
                # Look back for blank line (paragraph boundary)
                # Find last empty line in cur_lines
                last_blank = -1
                for i in range(len(cur_lines) - 1, -1, -1):
                    if cur_lines[i].strip() == "":
                        last_blank = i
                        break
                if last_blank > 0:
                    head = cur_lines[: last_blank + 1]
                    tail = cur_lines[last_blank + 1 :]
                    text = "\n".join(head).strip()
                    if text:
                        sections.append({"section_path": cur_heading, "chunk_text": text})
                    cur_lines = tail
                # else: no paragraph boundary found — keep accumulating (rare, very long paragraph)

    flush()
    # Re-attach frontmatter to first chunk
    if frontmatter and sections:
        sections[0]["chunk_text"] = frontmatter + "\n" + sections[0]["chunk_text"]
    elif frontmatter and not sections:
        sections.append({"section_path": "", "chunk_text": frontmatter})

    if not sections and body.strip():
        sections.append({"section_path": "", "chunk_text": body.strip()})
    return sections


def glossary_for_chunk(chunk_text: str, glossary: list[dict]) -> list[dict]:
    """Filter glossary to entries whose term_he occurs in chunk at word boundaries.

    Only terms with status 'approved' or 'keep_source' are injected — 'proposed'
    rows must not leak into prompts. Uses token-boundary matching to avoid
    injecting מודל when chunk contains only מודלים.
    """
    relevant = []
    he_tokens = set(re.findall(r"[א-ת]+", chunk_text))
    has_hebrew = re.compile(r"[א-ת]")
    for row in glossary:
        term = (row.get("term_he") or "").strip()
        if not term:
            continue
        status = (row.get("status") or "approved").strip()
        if status not in ("approved", "keep_source"):
            continue
        # Fast path: exact token match for single-token Hebrew terms
        if " " not in term and term in he_tokens:
            relevant.append(row)
            continue
        # Boundary-aware regex: require word boundaries so substring inside longer word doesn't match
        if has_hebrew.search(term):
            pat = r"(?<![א-ת])" + re.escape(term) + r"(?![א-ת])"
        else:
            pat = r"(?<![A-Za-z0-9_])" + re.escape(term) + r"(?![A-Za-z0-9_])"
        if re.search(pat, chunk_text):
            relevant.append(row)
    return relevant


def build_prompt(chunk_text: str, section_path: str, glossary_rows: list[dict],
                 prev_tail: str = "") -> str:
    glossary_block = ""
    if glossary_rows:
        lines = []
        for r in glossary_rows:
            term = r.get("term_he", "")
            eng = r.get("english", "")
            ks = r.get("keep_source", "0")
            if ks == "1":
                lines.append(f"- {term} → KEEP AS-IS (do not translate)")
            elif eng:
                lines.append(f"- {term} → {eng}")
            else:
                lines.append(f"- {term} → (translate per context)")
        glossary_block = "Glossary (use these exact renderings):\n" + "\n".join(lines) + "\n\n"

    prev_block = ""
    if prev_tail:
        prev_block = f"Previous chunk tail (context only, do not re-emit):\n{prev_tail[:800]}\n\n"

    person_rule = (
        "Keep person names in Hebrew — tokens like ⟦PERSON_0⟧ are person names, do not translate them.\n"
    )
    return (
        f"Translate this Hebrew markdown chunk to faithful technical English.\n"
        f"Rules:\n"
        f"- Preserve headings, lists, tables, code fences exactly (same counts).\n"
        f"- Use glossary renderings exactly where they appear.\n"
        f"- {person_rule}"
        f"- Never invent translations for unknown terms — list them in unknown_terms.\n"
        f"- Output JSON: {{\"translation\": string, \"unknown_terms\": [string], \"notes\": [string]}}\n\n"
        f"{glossary_block}"
        f"{prev_block}"
        f"Section: {section_path}\n\n"
        f"Chunk to translate:\n{chunk_text}\n"
    )


def call_llm(base_url: str, api_key: str, model: str, prompt: str, retries: int = 3) -> dict:
    url = base_url.rstrip("/") + "/chat/completions"
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }).encode()

    last_err = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=payload, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        })
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = json.loads(resp.read().decode())
            content = data["choices"][0]["message"]["content"]
            obj = json.loads(content)
            return {
                "translation": str(obj.get("translation", "")).strip(),
                "unknown_terms": list(obj.get("unknown_terms", [])),
                "notes": list(obj.get("notes", [])),
            }
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:600]
            last_err = f"HTTP {e.code}: {body}"
            if e.code in (429, 500, 502, 503) and attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise RuntimeError(last_err) from e
        except Exception as e:
            last_err = str(e)
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise RuntimeError(f"LLM call failed after {retries} attempts: {last_err}") from e
    raise RuntimeError(last_err or "LLM exhausted retries")


def mock_translate(chunk_text: str, glossary_rows: list[dict]) -> dict:
    # Deterministic mock: apply glossary substitutions, wrap Hebrew remainder
    out = chunk_text
    for r in glossary_rows:
        term = r.get("term_he", "")
        eng = r.get("english", "")
        if term and eng and term in out:
            out = out.replace(term, eng)
    # Mark remaining Hebrew spans as ⟦he:..⟧ (zero-guessing simulation).
    # Exclude PERSON sentinels: split by sentinel pattern, only mark Hebrew
    # in non-sentinel segments so ⟦PERSON_n⟧ never gets wrapped.
    PERSON_RE = re.compile(re.escape(PERSON_OPEN) + r"\d+" + re.escape(PERSON_CLOSE))
    parts = PERSON_RE.split(out)
    sentinels = PERSON_RE.findall(out)
    marked_parts: list[str] = []
    for i, seg in enumerate(parts):
        marked_parts.append(HEBREW_WORD_RE.sub(lambda m: HE_MARKER_FMT.format(term=m.group(0)), seg))
        if i < len(sentinels):
            marked_parts.append(sentinels[i])
    out = "".join(marked_parts)
    return {"translation": out, "unknown_terms": [], "notes": ["mock"]}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Translate markdown chunks with glossary + name guard")
    ap.add_argument("vault_root", nargs="?", default=".", help="vault root")
    ap.add_argument("--input", dest="input_dir", default=None, help="corpus dir (default raw_md/raw)")
    ap.add_argument("--glossary", default=None, help="glossary.csv path")
    ap.add_argument("--out", dest="out_dir", default=None, help="output store dir")
    ap.add_argument("--check", action="store_true", help="only check glossary gate, exit 1 if blocked")
    ap.add_argument("--mock", action="store_true", help="offline mock (no LLM)")
    ap.add_argument("--force", action="store_true", help="retranslate even if cached")
    ap.add_argument("--resume", action="store_true", help="same as default (kept for docs compat)")
    ap.add_argument("--limit", type=int, default=0, help="limit files (0=all)")
    args = ap.parse_args(argv)

    vault_root = Path(args.vault_root).resolve()
    cfg = load_config(vault_root)
    tcfg = cfg.get("translation", {})

    # Glossary gate — path from CLI > convert_config.json translation.glossary_path > default
    if args.glossary:
        glossary_path = Path(args.glossary)
    elif tcfg.get("glossary_path"):
        gp = Path(tcfg["glossary_path"])
        glossary_path = gp if gp.is_absolute() else vault_root / gp
    else:
        glossary_path = vault_root / "data" / "domain_terms" / "glossary.csv"
    # Fallback: glossary_proposed.csv if glossary.csv not yet created (pre-approval phase)
    if not glossary_path.exists() and glossary_path.name == "glossary.csv":
        alt = glossary_path.parent / "glossary_proposed.csv"
        if alt.exists():
            glossary_path = alt

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

    first_names, last_names = load_person_names(vault_root)
    print(f"Person names: {len(first_names)} first, {len(last_names)} last")

    corpus_dir = resolve_corpus_dir(vault_root, Path(args.input_dir) if args.input_dir else None)
    md_files = sorted(corpus_dir.rglob("*.md"))
    if args.limit:
        md_files = md_files[:args.limit]
    print(f"Translating {len(md_files)} files from {corpus_dir}")

    out_root = Path(args.out_dir) if args.out_dir else vault_root / "data" / "translations"
    out_root.mkdir(parents=True, exist_ok=True)

    # Ledger (canonical: vault_root/data/translations/ledger.jsonl)
    ledger_path = get_ledger_path(vault_root, out_root)
    glossary_version = ""
    if glossary_path.exists():
        try:
            glossary_version = hashlib.sha256(glossary_path.read_bytes()).hexdigest()[:10]
        except OSError:
            pass

    name_candidates: set[str] = set()
    translated = 0
    blocked = 0

    for md_file in md_files:
        rel = md_file.relative_to(vault_root).as_posix() if md_file.is_relative_to(vault_root) else md_file.name
        try:
            raw_text = md_file.read_text(encoding="utf-8")
        except OSError as e:
            print(f" skip {rel}: {e}", file=sys.stderr)
            continue

        src_hash = hashlib.sha256(raw_text.encode()).hexdigest()
        store_dir = out_root / src_hash[:2] / src_hash
        out_file = store_dir / "translation.md"
        if out_file.exists() and not args.force:
            continue

        try:
            chunk_chars = int(tcfg.get("chunk_chars", 6000))
        except (TypeError, ValueError):
            chunk_chars = 6000
        chunks = chunk_markdown(raw_text, max_chars=chunk_chars)
        chunk_translations: list[str] = []
        doc_unknown: list[str] = []
        prev_tail = ""

        for ch in chunks:
            chunk_text = ch["chunk_text"]
            section_path = ch["section_path"]
            # Mask person names
            masked, mapping = mask_person_names(chunk_text, first_names, last_names)
            if mapping:
                name_candidates.update(mapping)
            # Filtered glossary
            g_rows = glossary_for_chunk(masked, glossary)
            prompt = build_prompt(masked, section_path, g_rows, prev_tail)

            if args.mock:
                res = mock_translate(masked, g_rows)
            else:
                res = call_llm(base_url, api_key, model, prompt)

            trans = res["translation"]
            # Inject markers for unknown terms that aren't already marked
            for ut in res.get("unknown_terms", []):
                ut = str(ut).strip()
                if ut and ut not in trans and ut in chunk_text:
                    # Add marker adjacent to first occurrence of translation of that region is tricky;
                    # for now append marker list — stage-5 says markers are inline ⟦he:term⟧
                    marker = HE_MARKER_FMT.format(term=ut)
                    if marker not in trans:
                        trans = trans.rstrip() + f" {marker}"
                if ut:
                    doc_unknown.append(ut)

            # Unmask person names
            trans = unmask_person_names(trans, mapping)
            chunk_translations.append(trans)
            # Context tail for next chunk
            prev_tail = trans[-400:] if trans else ""

        full_translation = "\n\n".join(chunk_translations)
        # Determine if blocked_on_term
        has_markers = "⟦he:" in full_translation
        status = "blocked_on_term" if (has_markers or doc_unknown) else "completed"

        # Write content-addressed store
        store_dir.mkdir(parents=True, exist_ok=True)
        frontmatter = {
            "source_doc": rel,
            "source_hash": src_hash,
            "model": model,
            "glossary_version": glossary_version,
            "status": status,
            "marker_count": full_translation.count("⟦he:"),
            "unknown_terms": sorted(set(doc_unknown)),
        }
        fm_text = "---\n" + json.dumps(frontmatter, ensure_ascii=False, indent=2) + "\n---\n\n"
        out_file.write_text(fm_text + full_translation, encoding="utf-8")

        # Ledger event
        event = {
            "event": "translation_completed" if status == "completed" else "blocked_on_term",
            "ts": datetime.now(timezone.utc).isoformat(),
            "source_doc": rel,
            "source_hash": src_hash,
            "model": model,
            "glossary_version": glossary_version,
            "status": status,
            "marker_count": frontmatter["marker_count"],
            "unknown_terms": frontmatter["unknown_terms"],
        }
        with open(ledger_path, "a", encoding="utf-8") as lf:
            lf.write(json.dumps(event, ensure_ascii=False) + "\n")

        if status == "blocked_on_term":
            blocked += 1
        else:
            translated += 1
        print(f"  {rel}: {status} ({len(chunk_translations)} chunks, {frontmatter['marker_count']} markers)")

    # Log name candidates
    if name_candidates:
        cand_path = out_root / "name_candidates.txt"
        with open(cand_path, "w", encoding="utf-8") as f:
            for n in sorted(name_candidates):
                f.write(n + "\n")
        print(f"Name candidates: {len(name_candidates)} unique -> {cand_path}")

    print(f"Done: {translated} completed, {blocked} blocked_on_term -> {out_root}")


if __name__ == "__main__":
    main()
