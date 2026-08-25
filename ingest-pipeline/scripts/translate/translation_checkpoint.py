"""Chunk-level checkpoint store for stage 5 translation.

A 100-200 page PDF becomes ~67 chunks at chunk_chars=6000, translated strictly
sequentially with one LLM call each. The document-level content-addressed store
(data/translations/<sha>/) only skips work at document granularity, so a failure
in chunk 43 threw away chunks 1-42. At 95% per-chunk reliability a 67-chunk
document survives ~3% of the time.

This module is the partial-credit layer: every chunk that translates cleanly is
written to data/translations/chunks/<key[:2]>/<key>.json, so a rerun resumes
instead of restarting.

The key covers every input that determines a chunk's translation, including
prev_tail -- chunk N is fed the tail of chunk N-1's translation, so retranslating
N-1 must invalidate N.

Known limitations (deliberate, not oversights):

- base_url is NOT in the key. Pointing at a different backend that serves the same
  model name would reuse chunks from the first one. Including it was rejected because
  a restart on a different port would invalidate the entire store mid-corpus, and in
  the air gap there is one vLLM server. If a second backend is ever introduced, add it.
- Nothing prunes the store. Editing the glossary or any tracked module invalidates
  every checkpoint and lays down a fresh generation (~1,500 chunks for the 3,800-page
  corpus) without removing the old one. Chunks are invisible to the consumers, which
  all rglob("translation.md"), so this is disk cost only -- but it is unbounded.
- os.fsync before os.replace makes the write atomic against process death and, on
  NTFS, against most power-loss cases. A torn write still degrades to a cache miss.
- A .tmp-*.json file left by SIGKILL or power loss is never cleaned up. Harmless to
  reads (only <key>.json is ever loaded), but it accumulates.
- Retries have no backoff. call_llm already backs off on 429/5xx; this layer only
  retries model non-compliance, where an immediate retry is as good as a delayed one.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

# Bump when the payload shape or the set of key inputs changes, so old
# checkpoints are never replayed against new semantics.
CHECKPOINT_VERSION = 1


def chunk_checkpoint_key(chunk_text: str, section_path: str, prev_tail: str,
                         glossary_fingerprint: str, model: str,
                         mock: bool, no_mask: bool,
                         names_fingerprint: str, code_fingerprint: str) -> str:
    """Content address for one chunk translation. Stable across runs.

    Every argument is required on purpose: a forgotten input silently reuses
    chunks that were produced under different rules, and the failure is invisible
    because the cached output looks perfectly well-formed.

    names_fingerprint covers the curated person-name lists. They are edited between
    runs (that is what name_candidates.txt is for), they reach both the prompt and
    the preservation checks, and omitting them makes the name guard fail *open* on
    resumed chunks.

    code_fingerprint covers the pipeline modules that turn a chunk into a
    translation, so editing a prompt or the segmenter mid-corpus does not leave a
    document silently stitched together from two generations of the code.
    """
    h = hashlib.sha256()
    for part in (
        str(CHECKPOINT_VERSION),
        chunk_text,
        section_path,
        prev_tail,
        glossary_fingerprint,
        model,
        "mock" if mock else "live",
        "nomask" if no_mask else "mask",
        names_fingerprint,
        code_fingerprint,
    ):
        # Length-prefix each field so concatenation is unambiguous.
        h.update(str(len(part)).encode())
        h.update(b"\x00")
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def chunk_checkpoint_path(out_root: Path, key: str) -> Path:
    """Sharded path for one chunk checkpoint, mirroring the document store."""
    return Path(out_root) / "chunks" / key[:2] / f"{key}.json"


def load_chunk_checkpoint(out_root: Path, key: str) -> dict | None:
    """Return a stored chunk result, or None on miss.

    A corrupt or unreadable checkpoint is a miss, never an error: the whole
    point of this store is to make long runs more survivable, so a bad file
    must cost one retranslated chunk, not the document.
    """
    p = chunk_checkpoint_path(out_root, key)
    try:
        with open(p, encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError):
        return None
    if not _payload_is_well_formed(payload):
        return None
    return payload


def _payload_is_well_formed(payload: object) -> bool:
    """Reject anything the caller would choke on.

    The caller reads payload["translation"] and e["term_he"] unguarded, and main()
    only guards `except RuntimeError` — so a single stale file from an older payload
    shape would raise KeyError straight out of the batch, ending a 3,800-page run.
    Treat any non-conforming file as a miss: it costs one retranslated chunk.
    """
    if not isinstance(payload, dict):
        return False
    if not isinstance(payload.get("translation"), str):
        return False
    term_map = payload.get("term_map")
    if term_map is not None:
        if not isinstance(term_map, list):
            return False
        for e in term_map:
            if not isinstance(e, dict) or "term_he" not in e:
                return False
            # New schema uses translations:[] ; old checkpoints used english:str
            if "translations" not in e and "english" not in e:
                return False
    for field in ("unknown", "notes", "person_names"):
        v = payload.get(field)
        if v is not None and not isinstance(v, list):
            return False
    return True


def save_chunk_checkpoint(out_root: Path, key: str, payload: dict) -> None:
    """Write a chunk result atomically (temp file + os.replace)."""
    p = chunk_checkpoint_path(out_root, key)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(p.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, p)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# Modules whose source determines what a chunk translates to. Editing any of them
# mid-corpus must invalidate checkpoints, otherwise one document ends up stitched
# together from two generations of the pipeline with nothing recording it.
_CODE_FINGERPRINT_MODULES = (
    "translate.py",
    "translation_prompt.py",
    "translation_masking.py",
    "translation_invariants.py",
    "translation_chunking.py",
    "md_mask.py",
)

_code_fingerprint_cache: str | None = None


def pipeline_code_fingerprint(scripts_dir: Path | None = None) -> str:
    """Digest of the pipeline source that turns a chunk into a translation.

    Computed once per process. A module that cannot be read contributes its name
    only, so a partial checkout degrades to a coarser fingerprint rather than an
    exception — but it still changes the digest, which is the fail-safe direction.
    """
    global _code_fingerprint_cache
    if scripts_dir is None and _code_fingerprint_cache is not None:
        return _code_fingerprint_cache
    base = Path(scripts_dir) if scripts_dir is not None else Path(__file__).resolve().parent
    h = hashlib.sha256()
    for name in _CODE_FINGERPRINT_MODULES:
        h.update(name.encode())
        try:
            h.update((base / name).read_bytes())
        except OSError:
            h.update(b"<unreadable>")
    digest = h.hexdigest()[:16]
    if scripts_dir is None:
        _code_fingerprint_cache = digest
    return digest


def names_fingerprint(first_names: set[str], last_names: set[str]) -> str:
    """Digest of the curated person-name lists (order-independent: they are sets)."""
    h = hashlib.sha256()
    for label, names in (("first", first_names), ("last", last_names)):
        h.update(label.encode())
        for n in sorted(names):
            h.update(n.encode("utf-8"))
            h.update(b"\x00")
    return h.hexdigest()[:16]
