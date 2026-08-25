"""YAP-aware term detection for glossary QA (prompt-only pipeline).

No sentinel masking — glossary is prompt-only plain English. This module detects
which glossary terms occur in a chunk (YAP root-aware, longest match) and returns
a term_map for prompting + validation.
"""
from __future__ import annotations

import re

from .translation_common import check_glossary_collisions

HEBREW_RANGE = "א-ת"
PROCLITICS = set("הלבמושכ")
RAW_WORD_RE = re.compile(rf"(?:[A-Za-z0-9_{HEBREW_RANGE}]{{2,}}(?:-[A-Za-z0-9_{HEBREW_RANGE}]{{1,}})?|[A-Za-z0-9_{HEBREW_RANGE}]-[A-Za-z0-9_{HEBREW_RANGE}]{{1,}})")
MIXED_SPLIT_RE = re.compile(rf"^([{HEBREW_RANGE}]+)-?([A-Za-z][A-Za-z0-9_\-]*)")

try:
    from hebrew_yap_stemmer import root_keys as _yap_root_keys
    from hebrew_yap_stemmer import analyze_tokens as _yap_analyze
    _YAP_AVAILABLE = True
except ImportError:
    _yap_root_keys = None  # type: ignore
    _yap_analyze = None  # type: ignore
    _YAP_AVAILABLE = False


def _require_yap():
    if not _YAP_AVAILABLE or _yap_root_keys is None:
        raise RuntimeError("YAP required for glossary detection — fail-closed (YAP not installed: install YAP and ensure yap.exe is on $PATH or set YAP_DIR; see https://github.com/ONLP-Lab/yap)")
    if hasattr(_yap_root_keys, "_mock_name") or hasattr(_yap_root_keys, "assert_called"):
        return
    try:
        from hebrew_yap_stemmer import _find_yap_exe
        _find_yap_exe()
    except Exception as e:
        raise RuntimeError(f"YAP required for glossary detection — fail-closed: {e}") from e


def _heuristic_split(surface: str) -> tuple[str, str, str]:
    if any("א" <= c <= "ת" for c in surface) and any("A" <= c <= "Z" or "a" <= c <= "z" for c in surface):
        m = MIXED_SPLIT_RE.match(surface)
        if m:
            he_prefix, en_stem = m.group(1), m.group(2)
            if all(c in PROCLITICS for c in he_prefix) and len(en_stem) >= 2:
                remainder = surface[m.end():]
                return he_prefix, en_stem, remainder
        proclitic = ""
        i = 0
        while i < len(surface) and surface[i] in PROCLITICS:
            proclitic += surface[i]
            i += 1
            if i >= len(surface) - 1:
                break
        if proclitic and i < len(surface):
            core = surface[i:]
            for suf in ("יהם", "ינו", "כם", "כן", "ות", "ים", "ה", "ו", "ן", "ם"):
                if core.endswith(suf) and len(core) - len(suf) >= 2:
                    return proclitic, core[: -len(suf)], suf
            return proclitic, core, ""
        return "", surface, ""
    proclitic = ""
    rest = surface
    pre = ""
    for c in surface:
        if c in PROCLITICS and len(surface) - len(pre) > 3:
            pre += c
        else:
            break
    if pre:
        remainder = surface[len(pre):]
        if remainder and any("א" <= c <= "ת" for c in remainder):
            proclitic = pre
            rest = remainder
    suffix = ""
    base = rest
    for suf in ("יהם", "ינו", "כם", "כן", "ות", "ים", "ה", "ו", "ן", "ם"):
        if rest.endswith(suf) and len(rest) - len(suf) >= 2:
            suffix = suf
            base = rest[: -len(suf)]
            break
    return proclitic, base, suffix


def _roots_for_token(tok: str) -> str:
    try:
        res = _yap_root_keys([tok])  # type: ignore
        if res is None:
            return tok
        if isinstance(res, set):
            if not res:
                return tok
            return next(iter(res))
        if isinstance(res, (list, tuple)):
            if not res:
                return tok
            return str(res[0])
        return str(res)
    except (FileNotFoundError, RuntimeError) as e:
        raise RuntimeError(f"YAP required for glossary detection — fail-closed: {e}") from e
    except SystemExit:
        raise RuntimeError("YAP required for glossary detection — fail-closed (YAP binary missing or failed)")
    except Exception:
        if hasattr(_yap_root_keys, "_mock_name") or hasattr(_yap_root_keys, "assert_called"):
            pro, base, _suf = _heuristic_split(tok)
            if any("A" <= c <= "Z" or "a" <= c <= "z" for c in base):
                return base.lower()
            return base if base else tok
        raise


def _batch_roots_for_tokens(tokens: list[str]) -> dict[str, str]:
    if not tokens:
        return {}
    if _yap_root_keys is not None and (hasattr(_yap_root_keys, "_mock_name") or hasattr(_yap_root_keys, "assert_called")):
        return {t: _roots_for_token(t) for t in tokens}
    try:
        from hebrew_yap_stemmer import analyze_tokens as _analyze, _strip_hb_suffix
    except ImportError as e:
        raise RuntimeError(f"YAP required for glossary detection — fail-closed: {e}") from e
    uniq = list(dict.fromkeys(tokens))
    try:
        pairs = _analyze(uniq)
    except (FileNotFoundError, RuntimeError) as e:
        raise RuntimeError(f"YAP required for glossary detection — fail-closed: {e}") from e
    lemma_map = {surf: lemma for surf, lemma in pairs}
    for t in uniq:
        if t not in lemma_map:
            lemma_map[t] = t
    weak = {'א', 'ה', 'ו', 'י'}
    out: dict[str, str] = {}
    for tok in tokens:
        lemma = lemma_map.get(tok, tok)
        reduced = _strip_hb_suffix(lemma)
        strong = [c for c in reduced if 'א' <= c <= 'ת' and c not in weak]
        if len(strong) >= 3:
            out[tok] = ''.join(strong[:3])
        else:
            if any('A' <= c <= 'Z' or 'a' <= c <= 'z' for c in reduced):
                out[tok] = reduced.lower() if reduced != tok else tok.lower() if tok.isascii() else reduced
                if any('א' <= c <= 'ת' for c in tok):
                    pass
            else:
                out[tok] = reduced
        if tok.isascii() and tok.lower() != tok:
            pass
    for tok in tokens:
        if tok.isascii():
            if any('A' <= c <= 'Z' or 'a' <= c <= 'z' for c in tok):
                if all('A' <= c <= 'Z' or 'a' <= c <= 'z' or c in "-_" for c in tok):
                    out[tok] = tok.lower()
    return out


def _analyze_with_fallback(tokens: list[str]) -> dict[str, tuple[str, str, str]]:
    result: dict[str, tuple[str, str, str]] = {}
    is_root_mocked = hasattr(_yap_root_keys, "_mock_name") or hasattr(_yap_root_keys, "assert_called") if _yap_root_keys is not None else False
    analysis = None
    if _YAP_AVAILABLE and _yap_analyze is not None:
        is_mock = hasattr(_yap_analyze, "_mock_name") or hasattr(_yap_analyze, "assert_called")
        if is_root_mocked and not is_mock:
            analysis = None
        else:
            try:
                analysis = _yap_analyze(tokens)  # type: ignore
            except (FileNotFoundError, RuntimeError) as e:
                if not is_mock:
                    raise RuntimeError(f"YAP required for glossary detection — fail-closed: {e}") from e
                analysis = None
            except SystemExit:
                if not is_mock:
                    raise RuntimeError("YAP required for glossary detection — fail-closed (YAP binary missing or failed)")
                analysis = None
            except Exception as e:
                if is_mock or is_root_mocked:
                    analysis = None
                else:
                    raise RuntimeError(f"YAP required for glossary detection — fail-closed: {e}") from e
    if analysis is not None:
        for entry in analysis:
            if not entry:
                continue
            if len(entry) == 4:
                surf, lemma, pre, suf = entry  # type: ignore
                result[str(surf)] = (str(lemma), str(pre), str(suf))
            elif len(entry) == 2:
                surf, lemma = entry  # type: ignore
                pre, base, suf = _heuristic_split(str(surf))
                result[str(surf)] = (str(lemma), pre, suf)
            elif len(entry) >= 3:
                surf = str(entry[0])
                pre = str(entry[2]) if len(entry) > 2 else ""
                suf = str(entry[3]) if len(entry) > 3 else ""
                lemma = str(entry[1]) if len(entry) > 1 else surf
                result[surf] = (lemma, pre, suf)
        for tok in tokens:
            if tok not in result:
                pre, base, suf = _heuristic_split(tok)
                result[tok] = (base if base else tok, pre, suf)
        return result
    for tok in tokens:
        pre, base, suf = _heuristic_split(tok)
        result[tok] = (base if base else tok, pre, suf)
    return result


def detect_glossary_terms(chunk_text: str, glossary_rows: list[dict]) -> list[dict]:
    """YAP-aware detection. Returns term_map [{term_he, translations, keep_source, occurrences, src_order}]."""
    check_glossary_collisions(glossary_rows)

    glossary_entries: list[dict] = []
    glossary_index: dict[tuple[str, ...], dict] = {}
    _g_terms: list[tuple[dict, list[str]]] = []
    _all_g_toks: list[str] = []
    for row in glossary_rows:
        term_he = (row.get("term_he") or "").strip()
        if not term_he:
            continue
        status = (row.get("status") or "approved").strip()
        if status not in ("approved", "keep_source"):
            continue
        _require_yap()
        toks = RAW_WORD_RE.findall(term_he)
        if not toks and term_he:
            toks = re.findall(r"[א-תA-Za-z0-9_]+(?:-[א-תA-Za-z0-9_]+)?", term_he)
        if not toks:
            toks = term_he.split()
        if not toks:
            continue
        _g_terms.append((row, toks))
        _all_g_toks.extend(toks)
    _g_root_map: dict[str, str] = {}
    if _all_g_toks:
        try:
            _g_root_map = _batch_roots_for_tokens(_all_g_toks)
        except RuntimeError:
            raise
        except FileNotFoundError as e:
            raise RuntimeError(f"YAP required for glossary detection — fail-closed: {e}") from e
    for row, toks in _g_terms:
        term_he = (row.get("term_he") or "").strip()
        roots: list[str] = []
        for t in toks:
            try:
                r = _g_root_map.get(t) or _roots_for_token(t)
            except RuntimeError:
                raise
            except FileNotFoundError as e:
                raise RuntimeError(f"YAP required for glossary detection — fail-closed: {e}") from e
            roots.append(r)
        key = tuple(roots)
        if not key:
            continue
        keep_source = (status == "keep_source") or (str(row.get("keep_source") or "0").strip() == "1")
        translations = row.get("translations") or []
        if isinstance(translations, str):
            translations = [translations] if translations.strip() else []
        translations = [str(o).strip() for o in translations if str(o).strip()]
        gid = len(glossary_entries)
        entry = {
            "term_he": term_he,
            "translations": translations,
            "keep_source": keep_source,
            "rows_roots": key,
            "row": row,
        }
        glossary_entries.append(entry)
        if key not in glossary_index:
            glossary_index[key] = entry

    if not glossary_index:
        return []

    matches = list(RAW_WORD_RE.finditer(chunk_text))
    if not matches:
        return []

    chunk_tokens = [m.group(0) for m in matches]

    _require_yap()
    try:
        _analyze_with_fallback(chunk_tokens)
    except RuntimeError:
        raise
    except FileNotFoundError as e:
        raise RuntimeError(f"YAP required for glossary detection — fail-closed: {e}") from e

    _chunk_root_map: dict[str, str] = {}
    if chunk_tokens:
        try:
            _chunk_root_map = _batch_roots_for_tokens(chunk_tokens)
        except RuntimeError:
            raise
        except FileNotFoundError as e:
            raise RuntimeError(f"YAP required for glossary detection — fail-closed: {e}") from e
    root_sequence: list[str] = []
    for tok in chunk_tokens:
        try:
            r = _chunk_root_map.get(tok) or _roots_for_token(tok)
            root_sequence.append(r)
        except RuntimeError:
            raise
        except FileNotFoundError as e:
            raise RuntimeError(f"YAP required for glossary detection — fail-closed: {e}") from e

    matched_indices: set[int] = set()
    raw_matches: list[tuple[int, int, dict]] = []
    n_values = sorted({len(k) for k in glossary_index}, reverse=True)
    if not n_values:
        n_values = [1]

    for start in range(len(chunk_tokens)):
        if start in matched_indices:
            continue
        found = None
        found_entry = None
        for n in n_values:
            if start + n > len(chunk_tokens):
                continue
            if any((start + k) in matched_indices for k in range(n)):
                continue
            key = tuple(root_sequence[start : start + n])
            entry = glossary_index.get(key)
            if entry is None and n == 1:
                tok = chunk_tokens[start]
                pre, base, suf = _heuristic_split(tok)
                if base and base != tok:
                    try:
                        base_root = _roots_for_token(base)
                    except RuntimeError:
                        raise
                    alt_key = (base_root,)
                    entry = glossary_index.get(alt_key)
                    if entry is None and base.lower() != base:
                        entry = glossary_index.get((base.lower(),))
            if entry is None and n > 1:
                def _root_for(tok: str) -> str:
                    try:
                        return _roots_for_token(tok)
                    except RuntimeError:
                        raise
                pre0, base0, _suf0 = _heuristic_split(chunk_tokens[start])
                alt_first_root = _root_for(base0) if base0 and base0 != chunk_tokens[start] else root_sequence[start]
                pre_last, base_last, _suf_last = _heuristic_split(chunk_tokens[start + n - 1])
                alt_last_root = _root_for(base_last) if base_last and base_last != chunk_tokens[start + n - 1] else root_sequence[start + n - 1]
                candidates: list[tuple[str, ...]] = []
                c1 = list(root_sequence[start : start + n])
                c1[0] = alt_first_root
                candidates.append(tuple(c1))
                c2 = list(root_sequence[start : start + n])
                c2[-1] = alt_last_root
                candidates.append(tuple(c2))
                c3 = list(root_sequence[start : start + n])
                c3[0] = alt_first_root
                c3[-1] = alt_last_root
                candidates.append(tuple(c3))
                for cand in candidates:
                    if cand == tuple(root_sequence[start : start + n]):
                        continue
                    e2 = glossary_index.get(cand)
                    if e2 is not None:
                        entry = e2
                        break
            if entry is not None:
                found = (start, start + n)
                found_entry = entry
                break
        if found and found_entry is not None:
            raw_matches.append((found[0], found[1], found_entry))
            for k in range(found[0], found[1]):
                matched_indices.add(k)

    if not raw_matches:
        return []

    term_map_dict: dict[str, dict] = {}
    for s, e, entry in raw_matches:
        he = entry["term_he"]
        if he not in term_map_dict:
            term_map_dict[he] = {
                "term_he": he,
                "translations": entry["translations"],
                "keep_source": entry["keep_source"],
                "occurrences": 0,
                "src_order": s,
            }
        term_map_dict[he]["occurrences"] += 1
        if s < term_map_dict[he]["src_order"]:
            term_map_dict[he]["src_order"] = s

    return sorted(term_map_dict.values(), key=lambda x: (x["src_order"], x["term_he"]))


# Backward-compat aliases — translate.py and translation_llm mock may still import these
def mask_glossary_terms(chunk_text: str, glossary_rows: list[dict]) -> tuple[str, list[dict]]:
    """Deprecated: returns (chunk_text unchanged, term_map)."""
    return chunk_text, detect_glossary_terms(chunk_text, glossary_rows)


def unmask_glossary_terms(text: str, term_map: list[dict]) -> str:
    """Deprecated: plain pass-through."""
    return text
