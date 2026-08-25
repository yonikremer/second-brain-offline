"""LLM I/O — extracted from translate.py (pure move).

I/O boundary with retries, urllib, plain-text mock.
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request

from .translation_invariants import HE_MARKER_FMT, HEBREW_WORD_RE


def mock_translate(chunk_text: str, glossary_rows: list[dict], invariants: dict | None = None) -> dict:
    """Deterministic mock: protect invariants + table markers, wrap Hebrew, pick first translation.

    term_map is derived via detect_glossary_terms for consistency tracking,
    but no sentinel embedding — output is plain English choices.
    """
    from .translation_masking import detect_glossary_terms

    try:
        term_map = detect_glossary_terms(chunk_text, glossary_rows)
    except RuntimeError:
        raise
    except FileNotFoundError as e:
        raise RuntimeError(f"YAP required for glossary detection — fail-closed: {e}") from e

    # Determine chosen translation per term (first option for mock consistency)
    chosen_map: dict[str, str] = {}
    for e in term_map:
        if e.get("keep_source"):
            chosen_map[e["term_he"]] = e["term_he"]
        else:
            translations = e.get("translations") or []
            if isinstance(translations, str):
                translations = [translations] if translations.strip() else []
            if translations:
                chosen_map[e["term_he"]] = str(translations[0]).strip()

    # Build protected: invariants + table markers
    protected: list[str] = []
    if invariants:
        for cat in ("yaml_frontmatter", "code_sections", "person_names", "english_spans", "urls_and_paths"):
            for v in invariants.get(cat, []):
                if v and v not in protected:
                    protected.append(v)
    for delim in ("⟦SEG⟧", "⟦CELL⟧"):
        if delim in chunk_text and delim not in protected:
            protected.append(delim)

    # Pre-replace detected glossary Hebrew with chosen English (simulate model picking a valid option)
    mocked = chunk_text
    # Replace longest Hebrew terms first to avoid partial overlap
    for e in sorted(term_map, key=lambda x: len(x.get("term_he", "")), reverse=True):
        he = e.get("term_he", "")
        if not he:
            continue
        chosen = chosen_map.get(he, "")
        if not chosen:
            continue
        # Simple substring replacement (YAP detection ensures correct matching boundary)
        # For Hebrew inflected forms (הDBים), the mocked replacement just uses the chosen
        # English at the detected position; fine for mock determinism.
        # We replace exact term_he substring occurrences.
        mocked = re.sub(r"(?<![א-ת])" + re.escape(he) + r"(?![א-ת])", chosen, mocked)

    # Wrap remaining Hebrew outside protected spans
    if protected:
        protected_sorted = sorted(protected, key=len, reverse=True)
        pat = re.compile("|".join(re.escape(p) for p in protected_sorted))
        parts = pat.split(mocked)
        sentinels = pat.findall(mocked)
        wrapped_parts: list[str] = []
        for i, seg in enumerate(parts):
            wrapped_parts.append(HEBREW_WORD_RE.sub(lambda m: HE_MARKER_FMT.format(term=m.group(0)), seg))
            if i < len(sentinels):
                wrapped_parts.append(sentinels[i])
        mocked = "".join(wrapped_parts)
    else:
        mocked = HEBREW_WORD_RE.sub(lambda m: HE_MARKER_FMT.format(term=m.group(0)), mocked)

    return {"translation": mocked, "unknown_terms": [], "notes": ["mock"]}


# Legacy aliases for translate.py compat
def _mock_with_sentinels(masked_text: str, term_map: list[dict], invariants: dict | None = None) -> str:
    """Legacy: delegate to mock_translate on the original Hebrew side."""
    return mock_translate(masked_text, [dict(term_he=e.get("term_he"), translations=e.get("translations"), keep_source=e.get("keep_source"), status="approved") for e in term_map], invariants).get("translation", masked_text)


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
            choices = data.get("choices") if isinstance(data, dict) else None
            choice = choices[0] if isinstance(choices, list) and choices else {}
            finish_reason = choice.get("finish_reason") if isinstance(choice, dict) else None
            if finish_reason == "length":
                raise RuntimeError("LLM response truncated (finish_reason=length) — chunk too large or token limit hit")
            msg = choice.get("message") if isinstance(choice, dict) else None
            content = msg.get("content") if isinstance(msg, dict) else None
            if not content:
                content = data["choices"][0]["message"]["content"]
            obj = json.loads(content)
            return {
                "translation": str(obj.get("translation", "")).strip(),
                "unknown_terms": list(obj.get("unknown_terms", [])),
                "notes": list(obj.get("notes", [])),
            }
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode(errors="replace")[:300]
            except Exception:
                body = str(e)[:300]
            last_err = f"HTTP {e.code}"
            print(f"LLM HTTP {e.code}: {body[:200]}", file=sys.stderr)
            if e.code in (429, 500, 502, 503) and attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise RuntimeError(last_err) from e
        except RuntimeError:
            raise
        except Exception as e:
            last_err = str(e)
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise RuntimeError(f"LLM call failed after {retries} attempts: {last_err}") from e
    raise RuntimeError(last_err or "LLM exhausted retries")
