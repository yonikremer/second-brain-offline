# Conversion Defect Census Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a read-only, pure-stdlib census that measures conversion damage across `raw_md/` and routes every finding to the repair path that can fix it.

**Architecture:** A `census` package laid into vaults at `scripts/census/`. Ten focused modules: shared types, a detector registry, shared scanning helpers, six detector families (16 detectors), and a two-pass runner. Detectors are pure functions over a `CensusDoc` — the runner does all I/O and hands down everything a detector needs (decoded text, sibling inventory, corpus statistics) so purity is structural rather than aspirational.

**Tech Stack:** Python ≥3.11, stdlib only (`re`, `dataclasses`, `hashlib`, `base64`, `statistics`, `json`, `argparse`, `unittest`). No third-party packages, ever — this runs in an air gap.

**Spec:** [2026-08-09-conversion-defect-census-and-router-design.md](../specs/2026-08-09-conversion-defect-census-and-router-design.md), Part 1 only. Parts 2–3 (actions, router) are out of scope for this plan.

---

## File structure

All paths under `src/second_brain_vault_framework/payload/scripts/census/`, which `vault upgrade` lays into a vault at `scripts/census/`.

| File | Responsibility |
|------|----------------|
| `__init__.py` | Package marker. Empty. |
| `types.py` | `Route`, `Sample`, `CorpusStats`, `CensusDoc`, `DefectFinding`. No logic. |
| `registry.py` | `@register` decorator, `REGISTRY`, `THRESHOLDS`, `run_all()`. |
| `scan.py` | Shared helpers: `content_lines()` (skips frontmatter + code fences), `magic_type()`. |
| `detect_rtl.py` | Family 1 — `rtl_char_reversed`, `rtl_order_reversed`, `rtl_bidi_controls`. |
| `detect_binary.py` | Family 3 — `binary_magic_bytes`, `binary_byte_ratio`. |
| `detect_base64.py` | Family 4 — `base64_data_uri`, `base64_bare_blob`. |
| `detect_images.py` | Family 5 — `image_placeholder`, `image_empty_target`, `image_broken_ref`. |
| `detect_tables.py` | Family 2 — `table_pipe_inconsistent`, `table_cell_fragmented`, `table_shape_implausible`. |
| `detect_split.py` | Family 6 — `split_length_outlier`, `split_opens_midsentence`, `split_no_title`. |
| `runner.py` | Two-pass corpus walk, `census.jsonl`, `census-report.md`, example harvest, CLI. |
| `DETECTORS.md` | Human documentation, one section per detector. |

Tests live in `tests/test_census.py` (single file, one `TestCase` class per family — matching `tests/test_vault.py`'s existing shape).

**Two refinements to the spec's module layout**, both discovered while planning:

1. The spec names a single `detectors.py`. Sixteen detectors in one file would be ~600 lines; splitting by family keeps each file focused and puts code that changes together in the same place. Task 13 updates the spec to match.
2. `split_length_outlier` needs corpus-wide percentiles, which a per-document pure function cannot compute. `CensusDoc` gains a `corpus: CorpusStats` field, filled by the runner's first pass — the same pattern the spec already uses for `sibling_paths`. Task 13 records this too.

---

## Task 1: Package skeleton, types, registry, scanning helpers

**Files:**
- Create: `src/second_brain_vault_framework/payload/scripts/census/__init__.py`
- Create: `src/second_brain_vault_framework/payload/scripts/census/types.py`
- Create: `src/second_brain_vault_framework/payload/scripts/census/registry.py`
- Create: `src/second_brain_vault_framework/payload/scripts/census/scan.py`
- Test: `tests/test_census.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_census.py`:

```python
# tests/test_census.py
import sys
import unittest
from pathlib import Path

from second_brain_vault_framework import core as vault

sys.path.insert(0, str(vault.payload_root() / "scripts"))

from census.registry import REGISTRY, ROUTES, THRESHOLDS, VERSIONS, register, run_all  # noqa: E402
from census.scan import content_lines, magic_type  # noqa: E402
from census.types import CensusDoc, CorpusStats, DefectFinding  # noqa: E402

DEFAULT_CORPUS = CorpusStats(doc_count=100, length_p05=120, length_p50=2000, length_p95=8000)


def _unregister(detector_id):
    """Remove a test-only detector from every registry table."""
    for table in (REGISTRY, ROUTES, VERSIONS, THRESHOLDS):
        table.pop(detector_id, None)


def make_doc(text="", *, raw=None, path="raw_md/a.md", siblings=(), corpus=None):
    """Build a CensusDoc for tests. Pass raw= for undecodable binary content."""
    if raw is None:
        raw_bytes, decoded = text.encode("utf-8"), text
    else:
        raw_bytes = raw
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError:
            decoded = None
    return CensusDoc(
        doc_id="testdoc",
        title="t",
        raw_bytes=raw_bytes,
        text=decoded,
        source_path=path,
        sibling_paths=frozenset(siblings),
        corpus=corpus or DEFAULT_CORPUS,
    )


class TestScanHelpers(unittest.TestCase):
    def test_content_lines_skips_frontmatter(self):
        text = "---\ntitle: T\n---\nbody\n"
        self.assertEqual(list(content_lines(text)), [(4, "body")])

    def test_content_lines_skips_fenced_code(self):
        text = "before\n```\ninside\n```\nafter\n"
        self.assertEqual(list(content_lines(text)), [(1, "before"), (5, "after")])

    def test_magic_type_identifies_zip_and_none(self):
        self.assertEqual(magic_type(b"PK\x03\x04rest"), "zip")
        self.assertIsNone(magic_type(b"# just markdown"))


class TestRegistry(unittest.TestCase):
    def test_register_populates_registry_and_run_all_returns_findings(self):
        @register(id="dummy_probe", version=1, route="none")
        def dummy_probe(doc):
            return DefectFinding(
                detector_id="dummy_probe", version=1, route="none",
                fired=bool(doc.text), measures={"len": len(doc.text or "")},
            )

        try:
            self.assertIn("dummy_probe", REGISTRY)
            findings = {f.detector_id: f for f in run_all(make_doc("abc"))}
            self.assertTrue(findings["dummy_probe"].fired)
            self.assertEqual(findings["dummy_probe"].measures["len"], 3)
        finally:
            _unregister("dummy_probe")

    def test_register_rejects_duplicate_id(self):
        @register(id="dupe_probe", version=1, route="none")
        def first(doc):
            return DefectFinding("dupe_probe", 1, "none", False, {})

        try:
            with self.assertRaises(ValueError):
                @register(id="dupe_probe", version=1, route="none")
                def second(doc):
                    return DefectFinding("dupe_probe", 1, "none", False, {})
        finally:
            _unregister("dupe_probe")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m unittest discover -s tests -p test_census.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'census'`.

- [ ] **Step 3: Write minimal implementation**

Create `__init__.py` (empty file — the package marker).

Create `types.py`:

```python
"""Shared types for the defect census. Pure data, no logic, no I/O."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Route = Literal["repair", "reconvert", "resplit", "reroute", "none"]


@dataclass(frozen=True)
class Sample:
    """One excerpt that triggered a detector, with its 1-based line number."""
    line: int
    excerpt: str


@dataclass(frozen=True)
class CorpusStats:
    """Corpus-wide measures computed by the runner's first pass."""
    doc_count: int
    length_p05: int
    length_p50: int
    length_p95: int


@dataclass(frozen=True)
class CensusDoc:
    """One document as detectors see it.

    raw_bytes is authoritative: some files in raw_md/ are not markdown at all,
    so text is None whenever the bytes do not decode as UTF-8.
    sibling_paths and corpus are supplied by the runner so detectors never
    touch the filesystem.
    """
    doc_id: str
    title: str
    raw_bytes: bytes
    text: str | None
    source_path: str
    sibling_paths: frozenset[str]
    corpus: CorpusStats


@dataclass(frozen=True)
class DefectFinding:
    detector_id: str
    version: int
    route: Route
    fired: bool
    measures: dict[str, float | int | str]
    samples: tuple[Sample, ...] = ()
```

Create `registry.py`:

```python
"""Detector registry. Importing a detect_* module registers its detectors."""
from __future__ import annotations

from typing import Callable

from census.types import CensusDoc, DefectFinding, Route

MAX_SAMPLES = 3

REGISTRY: dict[str, Callable[[CensusDoc], DefectFinding]] = {}
THRESHOLDS: dict[str, float] = {}
ROUTES: dict[str, Route] = {}
VERSIONS: dict[str, int] = {}


def register(*, id: str, version: int, route: Route, threshold: float | None = None):
    """Register a detector. Bump version when logic or threshold changes."""
    def wrap(fn: Callable[[CensusDoc], DefectFinding]):
        if id in REGISTRY:
            raise ValueError(f"duplicate detector id: {id}")
        REGISTRY[id] = fn
        ROUTES[id] = route
        VERSIONS[id] = version
        if threshold is not None:
            THRESHOLDS[id] = threshold
        return fn
    return wrap


def run_all(doc: CensusDoc) -> list[DefectFinding]:
    """Run every registered detector against one document, in stable id order."""
    return [REGISTRY[k](doc) for k in sorted(REGISTRY)]
```

Create `scan.py`:

```python
"""Shared scanning helpers used by more than one detector family."""
from __future__ import annotations

import re
from typing import Iterator

_FENCE_RE = re.compile(r"^\s*(```|~~~)")

# Leading-byte signatures. WAV is special-cased because RIFF carries its type at offset 8.
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"Rar!\x1a\x07", "rar"),
    (b"PK\x03\x04", "zip"),
    (b"%PDF-", "pdf"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"7z\xbc\xaf\x27\x1c", "7z"),
    (b"OggS", "ogg"),
    (b"GIF8", "gif"),
    (b"\x1f\x8b", "gzip"),
    (b"BM", "bmp"),
)

IMAGE_TYPES = frozenset({"png", "jpeg", "gif", "bmp"})


def content_lines(text: str) -> Iterator[tuple[int, str]]:
    """Yield (1-based line number, line), skipping YAML frontmatter and fenced code.

    Detectors must never fire on code samples or metadata, so every text-scanning
    detector iterates through here rather than over raw splitlines().
    """
    lines = text.splitlines()
    start = 0
    if lines and lines[0].strip() == "---":
        for j in range(1, len(lines)):
            if lines[j].strip() == "---":
                start = j + 1
                break
    in_fence = False
    for n in range(start, len(lines)):
        line = lines[n]
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            yield n + 1, line


def magic_type(data: bytes) -> str | None:
    """Identify a file by its leading bytes. Returns None for anything unrecognized."""
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    for sig, name in _MAGIC:
        if data.startswith(sig):
            return name
    return None
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m unittest discover -s tests -p test_census.py -v
```

Expected: PASS, 5 tests.

- [ ] **Step 5: Commit**

```bash
git add src/second_brain_vault_framework/payload/scripts/census tests/test_census.py && git commit -m "feat(census): package skeleton, types, registry, scan helpers"
```

---

## Task 2: `rtl_char_reversed` — the final-form invariant

**Files:**
- Create: `src/second_brain_vault_framework/payload/scripts/census/detect_rtl.py`
- Test: `tests/test_census.py`

Hebrew's five final-form letters (ך ם ן ף ץ) may only occupy the last position in a word. A run starting with one is character-reversed. The mirror check — a non-final twin (כ מ נ פ צ) at run end — catches the same corruption from the other side.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_census.py`, above the `if __name__` block:

```python
from census import detect_rtl  # noqa: E402,F401


def find(doc, detector_id):
    """Run one detector by id and return its finding."""
    return REGISTRY[detector_id](doc)


class TestRtlCharReversed(unittest.TestCase):
    # "שלום העולם" — hello world. Each word ends in final mem.
    CLEAN = "שלום העולם שלום העולם שלום העולם"
    # The same text with each Hebrew run character-reversed.
    REVERSED = "םולש םלועה םולש םלועה םולש םלועה"

    def test_fires_on_reversed_runs(self):
        f = find(make_doc(self.REVERSED), "rtl_char_reversed")
        self.assertTrue(f.fired)
        self.assertEqual(f.measures["runs_total"], 6)
        self.assertEqual(f.measures["runs_reversed"], 6)
        self.assertEqual(f.route, "repair")

    def test_silent_on_clean_hebrew(self):
        f = find(make_doc(self.CLEAN), "rtl_char_reversed")
        self.assertFalse(f.fired)
        self.assertEqual(f.measures["runs_reversed"], 0)
        self.assertEqual(f.measures["runs_total"], 6)

    def test_ignores_latin_and_digits(self):
        f = find(make_doc("hello world 2024-01-15 ABC"), "rtl_char_reversed")
        self.assertFalse(f.fired)
        self.assertEqual(f.measures["runs_total"], 0)

    def test_skips_code_fences(self):
        text = "```\n" + self.REVERSED + "\n```\n"
        f = find(make_doc(text), "rtl_char_reversed")
        self.assertEqual(f.measures["runs_total"], 0)

    def test_single_letter_runs_do_not_count(self):
        # Standalone prefix letters carry no final-form information.
        f = find(make_doc("ב ל ש ה מ"), "rtl_char_reversed")
        self.assertEqual(f.measures["runs_total"], 0)

    def test_min_runs_guard_prevents_tiny_document_firing(self):
        f = find(make_doc("םולש"), "rtl_char_reversed")
        self.assertEqual(f.measures["runs_reversed"], 1)
        self.assertFalse(f.fired)  # 1 run is below MIN_RUNS

    # --- Adversarial cases the spec requires: clean Hebrew that must stay silent ---

    def test_gershayim_acronyms_do_not_fire(self):
        # צה״ל, תנ״ך, ארה״ב — real acronyms, correctly ordered.
        f = find(make_doc("צה״ל תנ״ך ארה״ב צה״ל תנ״ך ארה״ב"), "rtl_char_reversed")
        self.assertEqual(f.measures["runs_reversed"], 0)
        self.assertFalse(f.fired)

    def test_mixed_hebrew_latin_digit_line_does_not_fire(self):
        text = "המערכת Confluence גרסה 7.19 הותקנה בתאריך 2024-01-15 בשרת production"
        f = find(make_doc(text), "rtl_char_reversed")
        self.assertEqual(f.measures["runs_reversed"], 0)
        self.assertFalse(f.fired)

    def test_words_without_final_forms_are_the_known_blind_spot(self):
        # ספר reversed is רפס — neither end carries the final-form signal.
        # This asserts the documented limitation rather than a capability.
        f = find(make_doc("רפס רפס רפס רפס רפס רפס"), "rtl_char_reversed")
        self.assertEqual(f.measures["runs_total"], 6)
        self.assertEqual(f.measures["runs_reversed"], 0)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m unittest discover -s tests -p test_census.py -k RtlCharReversed -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'census.detect_rtl'`.

- [ ] **Step 3: Write minimal implementation**

Create `detect_rtl.py`:

```python
"""Family 1 — RTL corruption. Hebrew-specific; Arabic would need different rules."""
from __future__ import annotations

import re

from census.registry import MAX_SAMPLES, THRESHOLDS, register
from census.scan import content_lines
from census.types import CensusDoc, DefectFinding, Sample

# Hebrew letters, and the marks that may sit inside a word.
_LETTERS = "א-ת"
_MARKS = "ְ-ׇ׳״"          # niqqud, geresh, gershayim
_RUN_RE = re.compile(f"[{_LETTERS}{_MARKS}]+")

FINAL_FORMS = frozenset("ךםןףץ")      # ך ם ן ף ץ
NON_FINAL_TWINS = frozenset("כמנפצ")  # כ מ נ פ צ

MIN_RUNS = 5   # a handful of runs cannot establish a corpus-level rate


def _letters(run: str) -> list[str]:
    return [c for c in run if "א" <= c <= "ת"]


@register(id="rtl_char_reversed", version=1, route="repair", threshold=0.02)
def rtl_char_reversed(doc: CensusDoc) -> DefectFinding:
    """Hebrew runs whose character order is reversed, by final-form position."""
    total = reversed_runs = final_initial = nonfinal_terminal = 0
    samples: list[Sample] = []

    if doc.text is not None:
        for lineno, line in content_lines(doc.text):
            for m in _RUN_RE.finditer(line):
                letters = _letters(m.group())
                if len(letters) < 2:
                    continue
                total += 1
                head = letters[0] in FINAL_FORMS
                tail = letters[-1] in NON_FINAL_TWINS
                if head:
                    final_initial += 1
                if tail:
                    nonfinal_terminal += 1
                if head or tail:
                    reversed_runs += 1
                    if len(samples) < MAX_SAMPLES:
                        samples.append(Sample(line=lineno, excerpt=line.strip()[:160]))

    share = reversed_runs / total if total else 0.0
    return DefectFinding(
        detector_id="rtl_char_reversed",
        version=1,
        route="repair",
        fired=total >= MIN_RUNS and share >= THRESHOLDS["rtl_char_reversed"],
        measures={
            "runs_total": total,
            "runs_reversed": reversed_runs,
            "runs_final_initial": final_initial,
            "runs_nonfinal_terminal": nonfinal_terminal,
            "reversed_share": round(share, 4),
        },
        samples=tuple(samples),
    )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m unittest discover -s tests -p test_census.py -k RtlCharReversed -v
```

Expected: PASS, 9 tests.

- [ ] **Step 5: Commit**

```bash
git add -A src/second_brain_vault_framework/payload/scripts/census tests/test_census.py && git commit -m "feat(census): rtl_char_reversed via the Hebrew final-form invariant"
```

---

## Task 3: `rtl_order_reversed` — word-order and bidi tells

**Files:**
- Modify: `src/second_brain_vault_framework/payload/scripts/census/detect_rtl.py`
- Test: `tests/test_census.py`

Three independent line-level signals: a line opening with a full stop, a `)` preceding its `(`, and a date token that only becomes valid when fully reversed. The date check discriminates against legitimate European `DD-MM-YYYY` dates because reversing those yields an invalid day.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_census.py`:

```python
class TestRtlOrderReversed(unittest.TestCase):
    def test_fires_on_leading_period_lines(self):
        text = "\n".join([".שלום העולם"] * 6)
        f = find(make_doc(text), "rtl_order_reversed")
        self.assertTrue(f.fired)
        self.assertEqual(f.measures["lines_leading_period"], 6)

    def test_fires_on_mirrored_parentheses(self):
        text = "\n".join(["טקסט )הערה( נוסף"] * 6)
        f = find(make_doc(text), "rtl_order_reversed")
        self.assertTrue(f.fired)
        self.assertEqual(f.measures["lines_mirrored_parens"], 6)

    def test_reversed_iso_date_counts(self):
        f = find(make_doc("תאריך 51-10-4202 כאן"), "rtl_order_reversed")
        self.assertEqual(f.measures["dates_reversed"], 1)

    def test_european_date_is_not_a_reversed_date(self):
        # "15-01-2024" reversed is "4202-10-51" — day 51 is invalid, so no fire.
        f = find(make_doc("תאריך 15-01-2024 כאן"), "rtl_order_reversed")
        self.assertEqual(f.measures["dates_reversed"], 0)

    def test_silent_on_clean_text(self):
        text = "\n".join(["שלום העולם (הערה) נוסף"] * 6)
        f = find(make_doc(text), "rtl_order_reversed")
        self.assertFalse(f.fired)
        self.assertEqual(f.measures["lines_flagged"], 0)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m unittest discover -s tests -p test_census.py -k RtlOrderReversed -v
```

Expected: FAIL with `KeyError: 'rtl_order_reversed'`.

- [ ] **Step 3: Write minimal implementation**

Append to `detect_rtl.py`:

```python
import datetime

_DATE_RE = re.compile(r"\b\d{2}-\d{2}-\d{4}\b")

MIN_LINES = 5


def _is_reversed_date(token: str) -> bool:
    """True when the token only parses as a date after full character reversal."""
    try:
        datetime.date.fromisoformat(token[::-1])
        return True
    except ValueError:
        return False


def _has_mirrored_parens(line: str) -> bool:
    """A closing paren appearing before any opening one is a bidi mangling tell."""
    close, open_ = line.find(")"), line.find("(")
    return close != -1 and (open_ == -1 or close < open_)


@register(id="rtl_order_reversed", version=1, route="repair", threshold=0.02)
def rtl_order_reversed(doc: CensusDoc) -> DefectFinding:
    """Word- or line-order reversal, by punctuation and date-token position."""
    total = flagged = leading_period = mirrored = dates = 0
    samples: list[Sample] = []

    if doc.text is not None:
        for lineno, line in content_lines(doc.text):
            stripped = line.strip()
            if not stripped:
                continue
            total += 1
            a = stripped.startswith(".")
            b = _has_mirrored_parens(stripped)
            hits = [t for t in _DATE_RE.findall(stripped) if _is_reversed_date(t)]
            if a:
                leading_period += 1
            if b:
                mirrored += 1
            dates += len(hits)
            if a or b or hits:
                flagged += 1
                if len(samples) < MAX_SAMPLES:
                    samples.append(Sample(line=lineno, excerpt=stripped[:160]))

    share = flagged / total if total else 0.0
    return DefectFinding(
        detector_id="rtl_order_reversed",
        version=1,
        route="repair",
        fired=total >= MIN_LINES and share >= THRESHOLDS["rtl_order_reversed"],
        measures={
            "lines_total": total,
            "lines_flagged": flagged,
            "lines_leading_period": leading_period,
            "lines_mirrored_parens": mirrored,
            "dates_reversed": dates,
            "flagged_share": round(share, 4),
        },
        samples=tuple(samples),
    )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m unittest discover -s tests -p test_census.py -k RtlOrderReversed -v
```

Expected: PASS, 5 tests.

- [ ] **Step 5: Commit**

```bash
git add -A src/second_brain_vault_framework/payload/scripts/census tests/test_census.py && git commit -m "feat(census): rtl_order_reversed via punctuation and date-token tells"
```

---

## Task 4: `rtl_bidi_controls` — evidence only

**Files:**
- Modify: `src/second_brain_vault_framework/payload/scripts/census/detect_rtl.py`
- Test: `tests/test_census.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_census.py`:

```python
class TestRtlBidiControls(unittest.TestCase):
    def test_counts_bidi_control_characters(self):
        f = find(make_doc("שלום‏עולם‫טקסט"), "rtl_bidi_controls")
        self.assertTrue(f.fired)
        self.assertEqual(f.measures["control_count"], 2)
        self.assertEqual(f.route, "none")

    def test_silent_without_controls(self):
        f = find(make_doc("שלום העולם"), "rtl_bidi_controls")
        self.assertFalse(f.fired)
        self.assertEqual(f.measures["control_count"], 0)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m unittest discover -s tests -p test_census.py -k RtlBidiControls -v
```

Expected: FAIL with `KeyError: 'rtl_bidi_controls'`.

- [ ] **Step 3: Write minimal implementation**

Append to `detect_rtl.py`:

```python
_BIDI_RE = re.compile("[‎‏‪-‮⁦-⁩]")


@register(id="rtl_bidi_controls", version=1, route="none")
def rtl_bidi_controls(doc: CensusDoc) -> DefectFinding:
    """Bidi control characters. Diagnostic of extractor behaviour; never acted on."""
    text = doc.text or ""
    count = len(_BIDI_RE.findall(text))
    per_kchar = (count / len(text) * 1000) if text else 0.0
    return DefectFinding(
        detector_id="rtl_bidi_controls",
        version=1,
        route="none",
        fired=count > 0,
        measures={"control_count": count, "controls_per_kchar": round(per_kchar, 3)},
    )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m unittest discover -s tests -p test_census.py -k RtlBidiControls -v
```

Expected: PASS, 2 tests.

- [ ] **Step 5: Commit**

```bash
git add -A src/second_brain_vault_framework/payload/scripts/census tests/test_census.py && git commit -m "feat(census): rtl_bidi_controls as evidence-only detector"
```

---

## Task 5: Family 3 — binary content masquerading as markdown

**Files:**
- Create: `src/second_brain_vault_framework/payload/scripts/census/detect_binary.py`
- Test: `tests/test_census.py`

A ZIP signature is also every OOXML format's signature, so this detector distinguishes a recoverable `.docx`/`.pptx`/`.xlsx` from a genuine archive by looking for `[Content_Types].xml` near the start. Byte-ratio checks operate on decoded text, never raw bytes — Hebrew UTF-8 is full of high bytes and would otherwise flag the entire corpus.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_census.py`:

```python
from census import detect_binary  # noqa: E402,F401


class TestBinaryDetectors(unittest.TestCase):
    def test_magic_bytes_identify_rar(self):
        f = find(make_doc(raw=b"Rar!\x1a\x07\x00payload"), "binary_magic_bytes")
        self.assertTrue(f.fired)
        self.assertEqual(f.measures["magic"], "rar")
        self.assertEqual(f.measures["ooxml"], 0)
        self.assertEqual(f.route, "reroute")

    def test_ooxml_zip_is_flagged_recoverable(self):
        raw = b"PK\x03\x04" + b"\x00" * 26 + b"[Content_Types].xml" + b"\x00" * 40
        f = find(make_doc(raw=raw), "binary_magic_bytes")
        self.assertTrue(f.fired)
        self.assertEqual(f.measures["magic"], "zip")
        self.assertEqual(f.measures["ooxml"], 1)

    def test_plain_markdown_is_not_binary(self):
        f = find(make_doc("# Title\n\nbody\n"), "binary_magic_bytes")
        self.assertFalse(f.fired)
        self.assertEqual(f.measures["magic"], "")

    def test_byte_ratio_fires_on_nul_bytes(self):
        f = find(make_doc(raw=b"text\x00\x00more"), "binary_byte_ratio")
        self.assertTrue(f.fired)
        self.assertEqual(f.measures["nul_bytes"], 2)

    def test_byte_ratio_fires_when_undecodable(self):
        f = find(make_doc(raw=b"\xff\xfe\xfd\xfc"), "binary_byte_ratio")
        self.assertTrue(f.fired)
        self.assertEqual(f.measures["decodable"], 0)

    def test_byte_ratio_silent_on_hebrew(self):
        f = find(make_doc("שלום העולם, טקסט תקין."), "binary_byte_ratio")
        self.assertFalse(f.fired)
        self.assertEqual(f.measures["decodable"], 1)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m unittest discover -s tests -p test_census.py -k BinaryDetectors -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'census.detect_binary'`.

- [ ] **Step 3: Write minimal implementation**

Create `detect_binary.py`:

```python
"""Family 3 — binary content saved with an .md extension."""
from __future__ import annotations

from census.registry import THRESHOLDS, register
from census.scan import magic_type
from census.types import CensusDoc, DefectFinding

_OOXML_MARKER = b"[Content_Types].xml"
_OOXML_SCAN_BYTES = 4096   # the marker is conventionally the archive's first entry


@register(id="binary_magic_bytes", version=1, route="reroute")
def binary_magic_bytes(doc: CensusDoc) -> DefectFinding:
    """Identify non-markdown by leading bytes, separating OOXML from real archives."""
    kind = magic_type(doc.raw_bytes)
    ooxml = int(
        kind == "zip" and _OOXML_MARKER in doc.raw_bytes[:_OOXML_SCAN_BYTES]
    )
    return DefectFinding(
        detector_id="binary_magic_bytes",
        version=1,
        route="reroute",
        fired=kind is not None,
        measures={
            "magic": kind or "",
            "ooxml": ooxml,
            "recoverable_document": ooxml,
            "byte_size": len(doc.raw_bytes),
        },
    )


@register(id="binary_byte_ratio", version=1, route="reroute", threshold=0.02)
def binary_byte_ratio(doc: CensusDoc) -> DefectFinding:
    """Undecodable bytes, NULs, and C0 control density.

    Ratios are computed on decoded text, never raw bytes: Hebrew UTF-8 is full of
    high bytes and a raw-byte printability test would flag the whole corpus.
    """
    nul = doc.raw_bytes.count(b"\x00")
    decodable = doc.text is not None
    if decodable:
        text = doc.text or ""
        controls = sum(1 for c in text if ord(c) < 0x20 and c not in "\t\n\r")
        share = controls / len(text) if text else 0.0
    else:
        controls, share = 0, 0.0
    return DefectFinding(
        detector_id="binary_byte_ratio",
        version=1,
        route="reroute",
        fired=(nul > 0) or (not decodable) or share >= THRESHOLDS["binary_byte_ratio"],
        measures={
            "nul_bytes": nul,
            "decodable": int(decodable),
            "control_chars": controls,
            "control_share": round(share, 4),
        },
    )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m unittest discover -s tests -p test_census.py -k BinaryDetectors -v
```

Expected: PASS, 6 tests.

- [ ] **Step 5: Commit**

```bash
git add -A src/second_brain_vault_framework/payload/scripts/census tests/test_census.py && git commit -m "feat(census): binary-as-markdown detectors with OOXML salvage flag"
```

---

## Task 6: Family 4 — base64 payloads

**Files:**
- Create: `src/second_brain_vault_framework/payload/scripts/census/detect_base64.py`
- Test: `tests/test_census.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_census.py`:

```python
import base64  # noqa: E402

from census import detect_base64  # noqa: E402,F401

PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 200).decode()


class TestBase64Detectors(unittest.TestCase):
    def test_data_uri_is_detected_and_decoded(self):
        doc = make_doc(f"text\n\n![img](data:image/png;base64,{PNG_B64})\n")
        f = find(doc, "base64_data_uri")
        self.assertTrue(f.fired)
        self.assertEqual(f.measures["blob_count"], 1)
        self.assertEqual(f.measures["decoded_images"], 1)
        self.assertEqual(f.route, "repair")

    def test_data_uri_share_is_recorded(self):
        doc = make_doc(f"![img](data:image/png;base64,{PNG_B64})")
        f = find(doc, "base64_data_uri")
        self.assertGreater(f.measures["blob_share"], 0.5)

    def test_silent_without_blobs(self):
        f = find(make_doc("# Title\n\njust prose\n"), "base64_data_uri")
        self.assertFalse(f.fired)
        self.assertEqual(f.measures["blob_count"], 0)

    def test_bare_blob_needs_length_floor(self):
        short = "A" * 100
        self.assertFalse(find(make_doc(short), "base64_bare_blob").fired)
        long = "A" * 600
        self.assertTrue(find(make_doc(long), "base64_bare_blob").fired)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m unittest discover -s tests -p test_census.py -k Base64Detectors -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'census.detect_base64'`.

- [ ] **Step 3: Write minimal implementation**

Create `detect_base64.py`:

```python
"""Family 4 — base64 payloads inlined into markdown."""
from __future__ import annotations

import base64
import binascii
import re

from census.registry import MAX_SAMPLES, register
from census.scan import IMAGE_TYPES, magic_type
from census.types import CensusDoc, DefectFinding, Sample

_DATA_URI_RE = re.compile(r"data:([\w.+-]+/[\w.+-]+);base64,([A-Za-z0-9+/=\s]{40,})")

# 512 is a floor that clears hashes, UUIDs, and short ids while catching real payloads.
BARE_BLOB_FLOOR = 512
_BARE_RE = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{%d,}={0,2}" % BARE_BLOB_FLOOR)


def _decode(payload: str) -> bytes | None:
    try:
        return base64.b64decode("".join(payload.split()), validate=True)
    except (binascii.Error, ValueError):
        return None


@register(id="base64_data_uri", version=1, route="repair")
def base64_data_uri(doc: CensusDoc) -> DefectFinding:
    """Inline data: URIs. Repair extracts them to the store and leaves a reference."""
    text = doc.text or ""
    count = decoded_images = blob_chars = 0
    samples: list[Sample] = []

    for m in _DATA_URI_RE.finditer(text):
        count += 1
        blob_chars += len(m.group(2))
        raw = _decode(m.group(2))
        if raw is not None and magic_type(raw) in IMAGE_TYPES:
            decoded_images += 1
        if len(samples) < MAX_SAMPLES:
            line = text[: m.start()].count("\n") + 1
            samples.append(Sample(line=line, excerpt=f"{m.group(1)} ({len(m.group(2))} chars)"))

    share = blob_chars / len(text) if text else 0.0
    return DefectFinding(
        detector_id="base64_data_uri",
        version=1,
        route="repair",
        fired=count > 0,
        measures={
            "blob_count": count,
            "decoded_images": decoded_images,
            "blob_chars": blob_chars,
            "blob_share": round(share, 4),
        },
        samples=tuple(samples),
    )


@register(id="base64_bare_blob", version=1, route="repair")
def base64_bare_blob(doc: CensusDoc) -> DefectFinding:
    """Unwrapped base64 runs above the length floor."""
    text = doc.text or ""
    count = blob_chars = decodable = 0
    samples: list[Sample] = []

    for m in _BARE_RE.finditer(text):
        count += 1
        blob_chars += len(m.group())
        if _decode(m.group()) is not None:
            decodable += 1
        if len(samples) < MAX_SAMPLES:
            line = text[: m.start()].count("\n") + 1
            samples.append(Sample(line=line, excerpt=f"{len(m.group())} chars"))

    share = blob_chars / len(text) if text else 0.0
    return DefectFinding(
        detector_id="base64_bare_blob",
        version=1,
        route="repair",
        fired=count > 0,
        measures={
            "blob_count": count,
            "decodable_blobs": decodable,
            "blob_chars": blob_chars,
            "blob_share": round(share, 4),
        },
        samples=tuple(samples),
    )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m unittest discover -s tests -p test_census.py -k Base64Detectors -v
```

Expected: PASS, 4 tests.

- [ ] **Step 5: Commit**

```bash
git add -A src/second_brain_vault_framework/payload/scripts/census tests/test_census.py && git commit -m "feat(census): base64 data-uri and bare-blob detectors"
```

---

## Task 7: Family 5 — dropped images

**Files:**
- Create: `src/second_brain_vault_framework/payload/scripts/census/detect_images.py`
- Test: `tests/test_census.py`

`image_broken_ref` resolves references against `doc.sibling_paths`, the inventory snapshot the runner passes in — never against the filesystem, which would break detector purity.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_census.py`:

```python
from census import detect_images  # noqa: E402,F401


class TestImageDetectors(unittest.TestCase):
    def test_docling_placeholder_is_detected(self):
        f = find(make_doc("text\n<!-- image -->\nmore\n<!-- image -->\n"), "image_placeholder")
        self.assertTrue(f.fired)
        self.assertEqual(f.measures["placeholder_count"], 2)
        self.assertEqual(f.route, "reconvert")

    def test_formula_placeholder_counts_too(self):
        f = find(make_doc("<!-- formula-not-decoded -->\n"), "image_placeholder")
        self.assertTrue(f.fired)

    def test_empty_target_is_detected(self):
        f = find(make_doc("![alt]()\n![]()\n"), "image_empty_target")
        self.assertTrue(f.fired)
        self.assertEqual(f.measures["empty_target_count"], 2)

    def test_broken_ref_resolves_against_siblings(self):
        doc = make_doc(
            "![a](img/present.png)\n![b](img/missing.png)\n",
            path="raw_md/page.md",
            siblings=["raw_md/img/present.png"],
        )
        f = find(doc, "image_broken_ref")
        self.assertTrue(f.fired)
        self.assertEqual(f.measures["broken_count"], 1)
        self.assertEqual(f.measures["ref_count"], 2)

    def test_remote_and_data_urls_are_not_broken_refs(self):
        doc = make_doc("![a](https://x/y.png)\n![b](data:image/png;base64,AAAA)\n")
        f = find(doc, "image_broken_ref")
        self.assertFalse(f.fired)
        self.assertEqual(f.measures["ref_count"], 0)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m unittest discover -s tests -p test_census.py -k ImageDetectors -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'census.detect_images'`.

- [ ] **Step 3: Write minimal implementation**

Create `detect_images.py`:

```python
"""Family 5 — images the converter dropped."""
from __future__ import annotations

import posixpath
import re
from urllib.parse import unquote

from census.registry import MAX_SAMPLES, register
from census.scan import content_lines
from census.types import CensusDoc, DefectFinding, Sample

_PLACEHOLDER_RE = re.compile(r"<!--\s*(image|formula-not-decoded)\s*-->")
_EMPTY_TARGET_RE = re.compile(r"!\[[^\]]*\]\(\s*\)")
_IMG_REF_RE = re.compile(r"!\[[^\]]*\]\(\s*([^)\s]+)")
_REMOTE_RE = re.compile(r"^(https?:|data:|#|mailto:)", re.IGNORECASE)


@register(id="image_placeholder", version=1, route="reconvert")
def image_placeholder(doc: CensusDoc) -> DefectFinding:
    """Docling's literal placeholders for content it could not extract."""
    count = 0
    samples: list[Sample] = []
    if doc.text is not None:
        for lineno, line in content_lines(doc.text):
            for m in _PLACEHOLDER_RE.finditer(line):
                count += 1
                if len(samples) < MAX_SAMPLES:
                    samples.append(Sample(line=lineno, excerpt=m.group()))
    return DefectFinding(
        detector_id="image_placeholder", version=1, route="reconvert",
        fired=count > 0, measures={"placeholder_count": count}, samples=tuple(samples),
    )


@register(id="image_empty_target", version=1, route="reconvert")
def image_empty_target(doc: CensusDoc) -> DefectFinding:
    """Image syntax with no target at all."""
    count = 0
    samples: list[Sample] = []
    if doc.text is not None:
        for lineno, line in content_lines(doc.text):
            for m in _EMPTY_TARGET_RE.finditer(line):
                count += 1
                if len(samples) < MAX_SAMPLES:
                    samples.append(Sample(line=lineno, excerpt=m.group()))
    return DefectFinding(
        detector_id="image_empty_target", version=1, route="reconvert",
        fired=count > 0, measures={"empty_target_count": count}, samples=tuple(samples),
    )


@register(id="image_broken_ref", version=1, route="reconvert")
def image_broken_ref(doc: CensusDoc) -> DefectFinding:
    """Local image references that resolve to nothing in the corpus inventory."""
    base = posixpath.dirname(doc.source_path)
    refs = broken = 0
    samples: list[Sample] = []

    if doc.text is not None:
        for lineno, line in content_lines(doc.text):
            for m in _IMG_REF_RE.finditer(line):
                target = m.group(1).strip("<>")
                if _REMOTE_RE.match(target):
                    continue
                refs += 1
                resolved = posixpath.normpath(posixpath.join(base, unquote(target)))
                if resolved not in doc.sibling_paths:
                    broken += 1
                    if len(samples) < MAX_SAMPLES:
                        samples.append(Sample(line=lineno, excerpt=resolved))

    return DefectFinding(
        detector_id="image_broken_ref", version=1, route="reconvert",
        fired=broken > 0,
        measures={"ref_count": refs, "broken_count": broken},
        samples=tuple(samples),
    )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m unittest discover -s tests -p test_census.py -k ImageDetectors -v
```

Expected: PASS, 5 tests.

- [ ] **Step 5: Commit**

```bash
git add -A src/second_brain_vault_framework/payload/scripts/census tests/test_census.py && git commit -m "feat(census): dropped-image detectors resolving refs against the inventory"
```

---

## Task 8: Family 2 — mangled tables

**Files:**
- Create: `src/second_brain_vault_framework/payload/scripts/census/detect_tables.py`
- Test: `tests/test_census.py`

All three detectors share one block parser, which is why they live in one file. Cells that are purely numeric are excluded from the fragmentation measure so legitimate dense spec tables do not read as damage.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_census.py`:

```python
from census import detect_tables  # noqa: E402,F401

GOOD_TABLE = (
    "| Name | Value | Notes |\n"
    "|------|-------|-------|\n"
    "| alpha | 12 | fine |\n"
    "| beta | 34 | also fine |\n"
)

RAGGED_TABLE = (
    "| a | b | c |\n"
    "|---|---|---|\n"
    "| 1 | 2 |\n"
    "| 1 | 2 | 3 | 4 | 5 |\n"
    "| 1 |\n"
)

FRAGMENTED_TABLE = (
    "| ש | ל | ו | ם | ה | ע |\n"
    "|---|---|---|---|---|---|\n"
    "| ו | ל | ם | א | ב | ג |\n"
    "| ד | ה | ו | ז | ח | ט |\n"
)


class TestTableDetectors(unittest.TestCase):
    def test_ragged_rows_are_detected(self):
        f = find(make_doc(RAGGED_TABLE), "table_pipe_inconsistent")
        self.assertTrue(f.fired)
        self.assertEqual(f.route, "reconvert")

    def test_well_formed_table_is_silent(self):
        f = find(make_doc(GOOD_TABLE), "table_pipe_inconsistent")
        self.assertFalse(f.fired)

    def test_fragmented_cells_are_detected(self):
        f = find(make_doc(FRAGMENTED_TABLE), "table_cell_fragmented")
        self.assertTrue(f.fired)
        self.assertGreater(f.measures["fragmented_share"], 0.9)

    def test_numeric_cells_do_not_count_as_fragments(self):
        f = find(make_doc(GOOD_TABLE), "table_cell_fragmented")
        self.assertFalse(f.fired)

    def test_implausible_column_count_is_detected(self):
        wide = "| " + " | ".join(str(i) for i in range(20)) + " |\n"
        sep = "|" + "---|" * 20 + "\n"
        f = find(make_doc(wide + sep + wide), "table_shape_implausible")
        self.assertTrue(f.fired)
        self.assertEqual(f.measures["max_columns"], 20)

    def test_normal_shape_is_silent(self):
        f = find(make_doc(GOOD_TABLE), "table_shape_implausible")
        self.assertFalse(f.fired)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m unittest discover -s tests -p test_census.py -k TableDetectors -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'census.detect_tables'`.

- [ ] **Step 3: Write minimal implementation**

Create `detect_tables.py`:

```python
"""Family 2 — tables mangled by the converter."""
from __future__ import annotations

import re
from collections import Counter

from census.registry import MAX_SAMPLES, THRESHOLDS, register
from census.scan import content_lines
from census.types import CensusDoc, DefectFinding, Sample

_SEPARATOR_RE = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")

MIN_BLOCK_ROWS = 3
MAX_PLAUSIBLE_COLUMNS = 12
FRAGMENT_MAX_LEN = 3


def _table_blocks(text: str) -> list[list[tuple[int, str]]]:
    """Group consecutive pipe-bearing lines into table blocks."""
    blocks: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    for lineno, line in content_lines(text):
        if line.count("|") >= 2:
            current.append((lineno, line))
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return [b for b in blocks if len(b) >= MIN_BLOCK_ROWS]


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


@register(id="table_pipe_inconsistent", version=1, route="reconvert", threshold=0.10)
def table_pipe_inconsistent(doc: CensusDoc) -> DefectFinding:
    """Rows whose column count deviates from their block's modal count."""
    rows = deviating = 0
    samples: list[Sample] = []

    for block in _table_blocks(doc.text or ""):
        data = [(n, l) for n, l in block if not _SEPARATOR_RE.match(l)]
        if len(data) < MIN_BLOCK_ROWS - 1:
            continue
        counts = Counter(l.count("|") for _, l in data)
        modal = counts.most_common(1)[0][0]
        for lineno, line in data:
            rows += 1
            if line.count("|") != modal:
                deviating += 1
                if len(samples) < MAX_SAMPLES:
                    samples.append(Sample(line=lineno, excerpt=line.strip()[:160]))

    share = deviating / rows if rows else 0.0
    return DefectFinding(
        detector_id="table_pipe_inconsistent", version=1, route="reconvert",
        fired=rows >= MIN_BLOCK_ROWS and share >= THRESHOLDS["table_pipe_inconsistent"],
        measures={"table_rows": rows, "deviating_rows": deviating,
                  "deviating_share": round(share, 4)},
        samples=tuple(samples),
    )


@register(id="table_cell_fragmented", version=1, route="reconvert", threshold=0.35)
def table_cell_fragmented(doc: CensusDoc) -> DefectFinding:
    """Very short non-numeric cells — the shape a word split across columns makes."""
    total = fragments = 0
    samples: list[Sample] = []

    for block in _table_blocks(doc.text or ""):
        for lineno, line in block:
            if _SEPARATOR_RE.match(line):
                continue
            for cell in _cells(line):
                if not cell:
                    continue
                total += 1
                if len(cell) <= FRAGMENT_MAX_LEN and not cell.isnumeric():
                    fragments += 1
                    if len(samples) < MAX_SAMPLES:
                        samples.append(Sample(line=lineno, excerpt=line.strip()[:160]))

    share = fragments / total if total else 0.0
    return DefectFinding(
        detector_id="table_cell_fragmented", version=1, route="reconvert",
        fired=total > 0 and share >= THRESHOLDS["table_cell_fragmented"],
        measures={"cells_total": total, "cells_fragmented": fragments,
                  "fragmented_share": round(share, 4)},
        samples=tuple(samples),
    )


@register(id="table_shape_implausible", version=1, route="reconvert")
def table_shape_implausible(doc: CensusDoc) -> DefectFinding:
    """Column counts no human authored, and header rows with no content."""
    max_cols = empty_headers = blocks = 0

    for block in _table_blocks(doc.text or ""):
        blocks += 1
        for _, line in block:
            if not _SEPARATOR_RE.match(line):
                max_cols = max(max_cols, len(_cells(line)))
        header = next((l for _, l in block if not _SEPARATOR_RE.match(l)), "")
        if header and all(not c for c in _cells(header)):
            empty_headers += 1

    return DefectFinding(
        detector_id="table_shape_implausible", version=1, route="reconvert",
        fired=max_cols > MAX_PLAUSIBLE_COLUMNS or empty_headers > 0,
        measures={"table_blocks": blocks, "max_columns": max_cols,
                  "empty_header_rows": empty_headers},
    )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m unittest discover -s tests -p test_census.py -k TableDetectors -v
```

Expected: PASS, 6 tests.

- [ ] **Step 5: Commit**

```bash
git add -A src/second_brain_vault_framework/payload/scripts/census tests/test_census.py && git commit -m "feat(census): mangled-table detectors over a shared block parser"
```

---

## Task 9: Family 6 — split damage

**Files:**
- Create: `src/second_brain_vault_framework/payload/scripts/census/detect_split.py`
- Test: `tests/test_census.py`

This is the family that answers whether the header-detection splitter needs redoing. `split_length_outlier` deliberately does *not* use a plain percentile band — flagging everything below p05 would mark a fixed 5% of any corpus, healthy or not. It uses an absolute floor plus a multiple of p95.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_census.py`:

```python
from census import detect_split  # noqa: E402,F401


class TestSplitDetectors(unittest.TestCase):
    def test_short_document_is_an_outlier(self):
        f = find(make_doc("tiny"), "split_length_outlier")
        self.assertTrue(f.fired)
        self.assertEqual(f.measures["too_short"], 1)
        self.assertEqual(f.route, "resplit")

    def test_extremely_long_document_is_an_outlier(self):
        # p95 is 8000 in DEFAULT_CORPUS; 3x that is the swallowed-pages signal.
        f = find(make_doc("x" * 30000), "split_length_outlier")
        self.assertTrue(f.fired)
        self.assertEqual(f.measures["extreme_long"], 1)

    def test_normal_length_is_silent(self):
        f = find(make_doc("y" * 2000), "split_length_outlier")
        self.assertFalse(f.fired)

    def test_opens_midsentence_on_leading_comma(self):
        f = find(make_doc(", וכך המשיך הטקסט\n"), "split_opens_midsentence")
        self.assertTrue(f.fired)

    def test_opens_cleanly_is_silent(self):
        f = find(make_doc("# כותרת\n\nטקסט תקין\n"), "split_opens_midsentence")
        self.assertFalse(f.fired)

    def test_missing_title_is_detected(self):
        f = find(make_doc("just a paragraph with no heading at all\n"), "split_no_title")
        self.assertTrue(f.fired)

    def test_heading_satisfies_title_check(self):
        f = find(make_doc("# A Heading\n\nbody\n"), "split_no_title")
        self.assertFalse(f.fired)

    def test_frontmatter_title_satisfies_title_check(self):
        f = find(make_doc("---\ntitle: A Title\n---\nbody\n"), "split_no_title")
        self.assertFalse(f.fired)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m unittest discover -s tests -p test_census.py -k SplitDetectors -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'census.detect_split'`.

- [ ] **Step 3: Write minimal implementation**

Create `detect_split.py`:

```python
"""Family 6 — damage from the Confluence monolith's header-detection split."""
from __future__ import annotations

import re

from census.registry import register
from census.scan import content_lines
from census.types import CensusDoc, DefectFinding, Sample

ABSOLUTE_FLOOR = 200        # chars; a real Confluence page is longer than this
EXTREME_MULTIPLE = 3.0      # x p95 — the shape of several pages glued together
TITLE_SEARCH_LINES = 3
_FRONTMATTER_TITLE_RE = re.compile(r"^title:\s*\S", re.MULTILINE)
_MIDSENTENCE_PREFIXES = tuple(",;:)]}־")


@register(id="split_length_outlier", version=1, route="resplit")
def split_length_outlier(doc: CensusDoc) -> DefectFinding:
    """Documents too short to be a page, or long enough to be several.

    A plain p05/p95 band would flag a fixed 10% of any corpus regardless of health,
    so this uses an absolute floor and a multiple of p95 instead.
    """
    length = len(doc.text or "")
    ceiling = doc.corpus.length_p95 * EXTREME_MULTIPLE
    too_short = int(length < ABSOLUTE_FLOOR)
    extreme_long = int(length > ceiling)
    return DefectFinding(
        detector_id="split_length_outlier", version=1, route="resplit",
        fired=bool(too_short or extreme_long),
        measures={
            "char_count": length,
            "too_short": too_short,
            "extreme_long": extreme_long,
            "corpus_p50": doc.corpus.length_p50,
            "corpus_p95": doc.corpus.length_p95,
        },
    )


@register(id="split_opens_midsentence", version=1, route="resplit")
def split_opens_midsentence(doc: CensusDoc) -> DefectFinding:
    """First content line beginning as a continuation rather than a start."""
    first = ""
    first_line_no = 0
    for lineno, line in content_lines(doc.text or ""):
        if line.strip():
            first, first_line_no = line.strip(), lineno
            break

    starts_punct = first.startswith(_MIDSENTENCE_PREFIXES)
    starts_lower = bool(first) and first[0].isalpha() and first[0].islower()
    fired = starts_punct or starts_lower
    return DefectFinding(
        detector_id="split_opens_midsentence", version=1, route="resplit",
        fired=fired,
        measures={"starts_punctuation": int(starts_punct),
                  "starts_lowercase": int(starts_lower)},
        samples=(Sample(line=first_line_no, excerpt=first[:160]),) if fired else (),
    )


@register(id="split_no_title", version=1, route="resplit")
def split_no_title(doc: CensusDoc) -> DefectFinding:
    """No heading near the top and no frontmatter title."""
    text = doc.text or ""

    has_fm_title = False
    if text.startswith("---"):
        lines = text.splitlines()
        for j in range(1, len(lines)):
            if lines[j].strip() == "---":
                block = "\n".join(lines[1:j])
                has_fm_title = bool(_FRONTMATTER_TITLE_RE.search(block))
                break

    has_heading = False
    seen = 0
    for _, line in content_lines(text):
        if not line.strip():
            continue
        seen += 1
        if line.lstrip().startswith("#"):
            has_heading = True
            break
        if seen >= TITLE_SEARCH_LINES:
            break

    return DefectFinding(
        detector_id="split_no_title", version=1, route="resplit",
        fired=not (has_heading or has_fm_title),
        measures={"has_heading": int(has_heading), "has_frontmatter_title": int(has_fm_title)},
    )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m unittest discover -s tests -p test_census.py -k SplitDetectors -v
```

Expected: PASS, 8 tests.

- [ ] **Step 5: Commit**

```bash
git add -A src/second_brain_vault_framework/payload/scripts/census tests/test_census.py && git commit -m "feat(census): split-damage detectors with floor-and-multiple outlier rule"
```

---

## Task 10: Runner — two-pass corpus walk and `census.jsonl`

**Files:**
- Create: `src/second_brain_vault_framework/payload/scripts/census/runner.py`
- Test: `tests/test_census.py`

Pass 1 uses `os.stat` only — no file reads — to build `CorpusStats` and the inventory. Pass 2 reads each file once, builds a `CensusDoc`, runs every detector, and streams JSONL.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_census.py`:

```python
import json  # noqa: E402
import tempfile  # noqa: E402

from census import runner  # noqa: E402


class TestRunner(unittest.TestCase):
    def _corpus(self, tmp):
        corpus = Path(tmp) / "raw_md"
        (corpus / "img").mkdir(parents=True)
        (corpus / "clean.md").write_text("# כותרת\n\n" + "שלום העולם " * 40, encoding="utf-8")
        (corpus / "reversed.md").write_text("# כותרת\n\n" + "םולש םלועה " * 40, encoding="utf-8")
        (corpus / "archive.md").write_bytes(b"Rar!\x1a\x07\x00" + b"\x00" * 300)
        return corpus

    def test_run_writes_one_row_per_file_per_detector(self):
        with tempfile.TemporaryDirectory() as tmp:
            corpus = self._corpus(tmp)
            out = Path(tmp) / "out"
            runner.run(corpus, out)

            rows = [json.loads(l) for l in (out / "census.jsonl").read_text(encoding="utf-8").splitlines()]
            detectors = {r["detector_id"] for r in rows}
            docs = {r["source_path"] for r in rows}
            self.assertEqual(len(docs), 3)
            self.assertEqual(len(rows), len(docs) * len(detectors))
            self.assertIn("rtl_char_reversed", detectors)

    def test_reversed_file_fires_and_clean_file_does_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            corpus = self._corpus(tmp)
            out = Path(tmp) / "out"
            runner.run(corpus, out)

            rows = [json.loads(l) for l in (out / "census.jsonl").read_text(encoding="utf-8").splitlines()]
            fired = {(r["source_path"], r["detector_id"]) for r in rows if r["fired"]}
            self.assertIn(("raw_md/reversed.md", "rtl_char_reversed"), fired)
            self.assertNotIn(("raw_md/clean.md", "rtl_char_reversed"), fired)
            self.assertIn(("raw_md/archive.md", "binary_magic_bytes"), fired)

    def test_corpus_is_never_modified(self):
        with tempfile.TemporaryDirectory() as tmp:
            corpus = self._corpus(tmp)
            before = {p: p.read_bytes() for p in corpus.rglob("*") if p.is_file()}
            runner.run(corpus, Path(tmp) / "out")
            after = {p: p.read_bytes() for p in corpus.rglob("*") if p.is_file()}
            self.assertEqual(before, after)

    def test_refuses_output_inside_corpus(self):
        with tempfile.TemporaryDirectory() as tmp:
            corpus = self._corpus(tmp)
            with self.assertRaises(ValueError):
                runner.run(corpus, corpus / "out")
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m unittest discover -s tests -p test_census.py -k TestRunner -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'census.runner'`.

- [ ] **Step 3: Write minimal implementation**

Create `runner.py`:

```python
"""Census runner. The only module in the package that touches the filesystem.

Read-only by construction: it opens corpus files with mode "rb" and writes
exclusively under the output directory, which may not live inside the corpus.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import statistics
import sys
from pathlib import Path

from census.registry import REGISTRY, ROUTES, run_all
from census.types import CensusDoc, CorpusStats

# Importing the detector modules is what populates the registry.
from census import detect_base64, detect_binary, detect_images  # noqa: F401
from census import detect_rtl, detect_split, detect_tables  # noqa: F401

MARKDOWN_SUFFIX = ".md"


def _percentile(values: list[int], pct: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(pct / 100 * (len(ordered) - 1)))))
    return ordered[idx]


def _inventory(corpus: Path) -> tuple[list[Path], frozenset[str], CorpusStats]:
    """Pass 1: stat-only walk. Builds the sibling inventory and corpus statistics."""
    all_paths: list[str] = []
    docs: list[Path] = []
    sizes: list[int] = []
    for path in sorted(corpus.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(corpus.parent).as_posix()
        all_paths.append(rel)
        if path.suffix.lower() == MARKDOWN_SUFFIX:
            docs.append(path)
            sizes.append(path.stat().st_size)
    stats = CorpusStats(
        doc_count=len(docs),
        length_p05=_percentile(sizes, 5),
        length_p50=_percentile(sizes, 50),
        length_p95=_percentile(sizes, 95),
    )
    return docs, frozenset(all_paths), stats


def _build_doc(path: Path, corpus: Path, siblings: frozenset[str], stats: CorpusStats) -> CensusDoc:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    rel = path.relative_to(corpus.parent).as_posix()
    doc_id = hashlib.sha256(rel.encode("utf-8") + b"\0" + raw).hexdigest()[:16]
    return CensusDoc(
        doc_id=doc_id,
        title=path.stem,
        raw_bytes=raw,
        text=text,
        source_path=rel,
        sibling_paths=siblings,
        corpus=stats,
    )


def run(corpus: Path, out: Path) -> Path:
    """Run every detector over every markdown file. Returns the jsonl path."""
    corpus, out = Path(corpus).resolve(), Path(out).resolve()
    if corpus == out or corpus in out.parents:
        raise ValueError("output directory must not live inside the corpus")

    out.mkdir(parents=True, exist_ok=True)
    docs, siblings, stats = _inventory(corpus)
    jsonl = out / "census.jsonl"

    with jsonl.open("w", encoding="utf-8") as fh:
        for path in docs:
            doc = _build_doc(path, corpus, siblings, stats)
            for finding in run_all(doc):
                fh.write(json.dumps({
                    "doc_id": doc.doc_id,
                    "source_path": doc.source_path,
                    "detector_id": finding.detector_id,
                    "version": finding.version,
                    "route": finding.route,
                    "fired": finding.fired,
                    "measures": finding.measures,
                    "samples": [dataclasses.asdict(s) for s in finding.samples],
                }, ensure_ascii=False) + "\n")
    return jsonl


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="census",
        description="Read-only conversion-defect census over a markdown corpus.",
    )
    parser.add_argument("corpus", type=Path, help="directory of converted markdown (e.g. raw_md/)")
    parser.add_argument("--out", type=Path, required=True, help="output directory (must be outside the corpus)")
    args = parser.parse_args(argv)

    jsonl = run(args.corpus, args.out)
    print(f"census: {len(REGISTRY)} detectors -> {jsonl}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m unittest discover -s tests -p test_census.py -k TestRunner -v
```

Expected: PASS, 4 tests.

- [ ] **Step 5: Commit**

```bash
git add -A src/second_brain_vault_framework/payload/scripts/census tests/test_census.py && git commit -m "feat(census): two-pass runner writing census.jsonl"
```

---

## Task 11: Report and example harvest

**Files:**
- Modify: `src/second_brain_vault_framework/payload/scripts/census/runner.py`
- Test: `tests/test_census.py`

The report's co-occurrence matrix is what turns numbers into a work plan — a document that is both mis-split and RTL-damaged must be re-split first, or the repair is thrown away with the boundaries. The harvested examples are the D2 deliverable.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_census.py`:

```python
class TestReport(unittest.TestCase):
    def _corpus(self, tmp):
        corpus = Path(tmp) / "raw_md"
        corpus.mkdir(parents=True)
        (corpus / "reversed.md").write_text("# כותרת\n\n" + "םולש םלועה " * 40, encoding="utf-8")
        (corpus / "clean.md").write_text("# כותרת\n\n" + "שלום העולם " * 40, encoding="utf-8")
        return corpus

    def test_report_lists_every_detector_and_route_totals(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            runner.run(self._corpus(tmp), out)
            report = (out / "census-report.md").read_text(encoding="utf-8")
            self.assertIn("rtl_char_reversed", report)
            self.assertIn("Route totals", report)
            self.assertIn("Co-occurrence", report)

    def test_examples_are_harvested_for_fired_detectors(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            runner.run(self._corpus(tmp), out)
            harvested = out / "examples" / "rtl_char_reversed"
            self.assertTrue(harvested.is_dir())
            self.assertTrue(any(harvested.iterdir()))

    def test_no_examples_directory_for_silent_detectors(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            runner.run(self._corpus(tmp), out)
            self.assertFalse((out / "examples" / "table_shape_implausible").exists())
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m unittest discover -s tests -p test_census.py -k TestReport -v
```

Expected: FAIL — `FileNotFoundError` for `census-report.md`.

- [ ] **Step 3: Write minimal implementation**

In `runner.py`, add these functions above `run()`:

```python
from collections import Counter

EXAMPLES_PER_DETECTOR = 5


def _harvest(out: Path, detector_id: str, doc: CensusDoc, finding, seen: Counter) -> None:
    """Write a real excerpt as a test fixture. D2 requires 3-5 per defect class."""
    if seen[detector_id] >= EXAMPLES_PER_DETECTOR:
        return
    folder = out / "examples" / detector_id
    folder.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"L{s.line}: {s.excerpt}" for s in finding.samples) or "(no excerpt)"
    (folder / f"{doc.doc_id}.md").write_text(
        f"# {detector_id}\n\n"
        f"- source: `{doc.source_path}`\n"
        f"- measures: `{json.dumps(finding.measures, ensure_ascii=False)}`\n\n"
        f"```\n{body}\n```\n",
        encoding="utf-8",
    )
    seen[detector_id] += 1


def _write_report(out: Path, stats: CorpusStats, fired: Counter,
                  route_totals: Counter, pairs: Counter) -> None:
    lines = [
        "# Conversion defect census",
        "",
        f"- documents scanned: **{stats.doc_count}**",
        f"- size percentiles (bytes): p05 {stats.length_p05} / p50 {stats.length_p50} / p95 {stats.length_p95}",
        "",
        "## Detector results",
        "",
        "| Detector | Route | Fired | Corpus share |",
        "|----------|-------|-------|--------------|",
    ]
    for det in sorted(REGISTRY):
        n = fired[det]
        share = (n / stats.doc_count * 100) if stats.doc_count else 0.0
        lines.append(f"| `{det}` | {ROUTES[det]} | {n} | {share:.1f}% |")

    lines += ["", "## Route totals", "",
              "| Route | Documents affected |", "|-------|--------------------|"]
    for route, n in sorted(route_totals.items()):
        lines.append(f"| {route} | {n} |")

    lines += ["", "## Co-occurrence", "",
              "Which defects travel together. A document that is both mis-split and",
              "RTL-damaged must be re-split before repair, or the repair is discarded",
              "with the boundaries.", "",
              "| Detector A | Detector B | Documents |",
              "|-----------|-----------|-----------|"]
    for (a, b), n in pairs.most_common(25):
        lines.append(f"| `{a}` | `{b}` | {n} |")

    (out / "census-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
```

Then replace the body of `run()` (from `out.mkdir` to `return jsonl`) with:

```python
    out.mkdir(parents=True, exist_ok=True)
    docs, siblings, stats = _inventory(corpus)
    jsonl = out / "census.jsonl"

    fired: Counter = Counter()
    route_totals: Counter = Counter()
    pairs: Counter = Counter()
    harvested: Counter = Counter()

    with jsonl.open("w", encoding="utf-8") as fh:
        for path in docs:
            doc = _build_doc(path, corpus, siblings, stats)
            hits: list[str] = []
            routes_hit: set[str] = set()
            for finding in run_all(doc):
                fh.write(json.dumps({
                    "doc_id": doc.doc_id,
                    "source_path": doc.source_path,
                    "detector_id": finding.detector_id,
                    "version": finding.version,
                    "route": finding.route,
                    "fired": finding.fired,
                    "measures": finding.measures,
                    "samples": [dataclasses.asdict(s) for s in finding.samples],
                }, ensure_ascii=False) + "\n")
                if finding.fired:
                    fired[finding.detector_id] += 1
                    hits.append(finding.detector_id)
                    if finding.route != "none":
                        routes_hit.add(finding.route)
                    _harvest(out, finding.detector_id, doc, finding, harvested)
            for route in routes_hit:
                route_totals[route] += 1
            for i, a in enumerate(sorted(hits)):
                for b in sorted(hits)[i + 1:]:
                    pairs[(a, b)] += 1

    _write_report(out, stats, fired, route_totals, pairs)
    return jsonl
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m unittest discover -s tests -p test_census.py -v
```

Expected: PASS, all tests including the 3 new report tests.

- [ ] **Step 5: Commit**

```bash
git add -A src/second_brain_vault_framework/payload/scripts/census tests/test_census.py && git commit -m "feat(census): report with route totals, co-occurrence, and example harvest"
```

---

## Task 12: `DETECTORS.md` and the drift guard

**Files:**
- Create: `src/second_brain_vault_framework/payload/scripts/census/DETECTORS.md`
- Test: `tests/test_census.py`

The filter catalog uses this same guard: a test asserts every registered id appears in the documentation and vice versa, so the two cannot drift apart.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_census.py`:

```python
class TestDetectorDocs(unittest.TestCase):
    DOC = vault.payload_root() / "scripts" / "census" / "DETECTORS.md"

    def test_every_detector_is_documented(self):
        text = self.DOC.read_text(encoding="utf-8")
        for det in sorted(REGISTRY):
            self.assertIn(f"### `{det}`", text, f"{det} missing from DETECTORS.md")

    def test_no_documented_detector_is_unregistered(self):
        import re as _re
        documented = set(_re.findall(r"^### `([a-z0-9_]+)`", self.DOC.read_text(encoding="utf-8"), _re.M))
        self.assertEqual(documented - set(REGISTRY), set())

    def test_sixteen_detectors_are_registered(self):
        self.assertEqual(len(REGISTRY), 16)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m unittest discover -s tests -p test_census.py -k DetectorDocs -v
```

Expected: FAIL — `FileNotFoundError` for `DETECTORS.md`.

- [ ] **Step 3: Write minimal implementation**

Create `DETECTORS.md`:

````markdown
# Census detectors

Read-only measurement over `raw_md/`. Every detector is pure, returns its measures
whether or not it fired, and carries a version integer — bump it when logic or a
threshold changes, and census rows recompute on the next run.

Thresholds marked *(fit)* must be set from the Phase-0 gold sample before the numbers
are trusted. The defaults below are starting points, not derived values.

Run:

```bash
PYTHONPATH=scripts python3 -m census.runner raw_md/ --out census-out/
```

## Family 1 — RTL corruption (Hebrew)

### `rtl_char_reversed`
Route `repair` · threshold `0.02` *(fit)* · min 5 runs

Hebrew's final-form letters (ך ם ן ף ץ) may only end a word, so a run starting with one
is character-reversed. The mirror check catches a non-final twin (כ מ נ פ צ) at run end.

**Blind spot, by design:** words containing no final form — ספר reversed is רפס, and
neither end carries the signal. Detection is therefore reliable per *document* (where
final forms are common, especially the plural ending ים) and unreliable per *word*.
This is why the detector reports `reversed_share` rather than a boolean.

**Known false positives:** gershayim acronyms, Latin transliterations. Single-letter runs
are excluded outright.

### `rtl_order_reversed`
Route `repair` · threshold `0.02` *(fit)* · min 5 lines

Line-level order damage: a line opening with `.`, a `)` before its `(`, or a date token
that only parses after full reversal. `51-10-4202` fires; the legitimate European
`15-01-2024` does not, because its reversal yields day 51.

**No self-check exists for this one.** Reversed word order looks structurally valid in
either direction, so only a Hebrew reader can confirm a repair. Keep it at `review`.

### `rtl_bidi_controls`
Route `none`

Counts U+200E, U+200F, U+202A–202E, U+2066–2069. Evidence only — never acted on, because
stripping the controls erases the signal the other RTL detectors depend on.

## Family 2 — Mangled tables

### `table_pipe_inconsistent`
Route `reconvert` · threshold `0.10` *(fit)* · min 3 rows

Share of rows whose pipe count deviates from their block's modal count.

### `table_cell_fragmented`
Route `reconvert` · threshold `0.35` *(fit)*

Share of cells 1–3 characters and non-numeric — the shape a word split across columns
makes. Numeric cells are excluded so dense spec tables do not read as damage.

### `table_shape_implausible`
Route `reconvert`

Fires above 12 columns, or on a header row whose cells are all empty.

## Family 3 — Binary content in `.md`

### `binary_magic_bytes`
Route `reroute`

Leading-byte identification. A ZIP signature is also every OOXML signature, so the
detector looks for `[Content_Types].xml` in the first 4 KB: present means a recoverable
`.docx`/`.pptx`/`.xlsx` bound for the router's input queue, absent means a real archive
bound for quarantine. Route, never silently reject.

### `binary_byte_ratio`
Route `reroute` · threshold `0.02` *(fit)*

NUL bytes, undecodable content, and C0 control density. Ratios are computed on decoded
text, never raw bytes — Hebrew UTF-8 is full of high bytes and a raw printability test
would flag the entire corpus.

## Family 4 — Base64 payloads

### `base64_data_uri`
Route `repair`

`data:<mime>;base64,` URIs, with decode verification against image magic. Reports
`blob_share` of file characters, which matters more than count: inline blobs wreck
chunking and inflate token counts through every later stage.

### `base64_bare_blob`
Route `repair` · floor 512 chars *(fit)*

Unwrapped base64 runs. The floor clears hashes, UUIDs, and short identifiers.

## Family 5 — Dropped images

### `image_placeholder`
Route `reconvert`

Docling's literal `<!-- image -->` and `<!-- formula-not-decoded -->` markers.
Reconversion only helps if image export is enabled in the router's invocation.

### `image_empty_target`
Route `reconvert`

`![alt]()` with no target.

### `image_broken_ref`
Route `reconvert`

Local references resolving to nothing in the corpus inventory. Remote and `data:` URLs
are skipped. Resolution uses the inventory the runner passes in, never the filesystem.

## Family 6 — Split damage

### `split_length_outlier`
Route `resplit` · floor 200 chars · ceiling 3 × p95

Documents too short to be a Confluence page, or long enough to be several. A plain
p05/p95 band is deliberately avoided: it would flag a fixed 10% of any corpus whether
or not the split is healthy.

### `split_opens_midsentence`
Route `resplit`

First content line starting with continuation punctuation or a Latin lowercase letter.

**Weakest detector in the set.** Hebrew has no letter case, so the lowercase signal only
catches Latin passages, and continuation punctuation is a narrow tell. Treat its share as
a floor on split damage, never an estimate.

### `split_no_title`
Route `resplit`

No heading within the first 3 content lines and no frontmatter `title:`.
````

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m unittest discover -s tests -p test_census.py -k DetectorDocs -v
```

Expected: PASS, 3 tests.

- [ ] **Step 5: Commit**

```bash
git add -A src/second_brain_vault_framework/payload/scripts/census tests/test_census.py && git commit -m "docs(census): DETECTORS.md with drift guard against the registry"
```

---

## Task 13: Manifest registration, example vault, spec sync

**Files:**
- Modify: `src/second_brain_vault_framework/manifest.json`
- Modify: `docs/superpowers/specs/2026-08-09-conversion-defect-census-and-router-design.md`
- Generated: `example_vault/scripts/census/**`

Adding payload files without registering them in `owned_paths` means `scaffold` and `upgrade` silently ignore them — the file would never reach a vault. CI's `example-vault` stage then fails if the payload was not laid down.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_census.py`:

```python
class TestManifestRegistration(unittest.TestCase):
    def test_every_census_file_is_owned(self):
        manifest = json.loads(
            (Path(vault.__file__).parent / "manifest.json").read_text(encoding="utf-8")
        )
        owned = set(manifest["owned_paths"])
        census_dir = vault.payload_root() / "scripts" / "census"
        for path in sorted(census_dir.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                rel = f"scripts/census/{path.relative_to(census_dir).as_posix()}"
                self.assertIn(rel, owned, f"{rel} missing from manifest owned_paths")
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m unittest discover -s tests -p test_census.py -k ManifestRegistration -v
```

Expected: FAIL — `scripts/census/types.py missing from manifest owned_paths`.

- [ ] **Step 3: Write minimal implementation**

Add these 12 entries to `owned_paths` in `src/second_brain_vault_framework/manifest.json`, keeping the list's existing ordering style:

```json
    "scripts/census/__init__.py",
    "scripts/census/types.py",
    "scripts/census/registry.py",
    "scripts/census/scan.py",
    "scripts/census/detect_rtl.py",
    "scripts/census/detect_binary.py",
    "scripts/census/detect_base64.py",
    "scripts/census/detect_images.py",
    "scripts/census/detect_tables.py",
    "scripts/census/detect_split.py",
    "scripts/census/runner.py",
    "scripts/census/DETECTORS.md",
```

Then update the spec's module layout section. In `docs/superpowers/specs/2026-08-09-conversion-defect-census-and-router-design.md`, replace the `scripts/census/` code block under Part 1's "Module layout" with:

```
scripts/census/
  __init__.py
  types.py        # CensusDoc, DefectFinding, CorpusStats, Sample, Route
  registry.py     # the register decorator, REGISTRY, THRESHOLDS
  scan.py         # shared helpers: content_lines(), magic_type()
  detect_rtl.py       # family 1
  detect_tables.py    # family 2
  detect_binary.py    # family 3
  detect_base64.py    # family 4
  detect_images.py    # family 5
  detect_split.py     # family 6
  runner.py       # two-pass walk, jsonl, report, example harvest, CLI
  DETECTORS.md    # one section per detector; a test guards it against the registry
```

And in the same spec, add this bullet immediately after the `sibling_paths` bullet in the `CensusDoc` divergence list:

```markdown
- **`corpus: CorpusStats` is passed in too.** `split_length_outlier` needs corpus-wide
  percentiles, which a per-document pure function cannot compute. The runner's first
  pass is a stat-only walk that produces them before any detector runs.
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m unittest discover -s tests -p test_census.py -k ManifestRegistration -v
```

Expected: PASS, 1 test.

- [ ] **Step 5: Lay the payload into the example vault**

```bash
vault upgrade example_vault && vault check example_vault
```

Expected: `vault check` exits 0, and `example_vault/scripts/census/` now contains all 12 files.

- [ ] **Step 6: Run the full suite exactly as CI does**

```bash
python -m unittest discover -s tests -v
```

Expected: PASS — the pre-existing `test_vault.py` tests plus every census test.

- [ ] **Step 7: Commit**

```bash
git add -A && git commit -m "feat(census): register in manifest, lay into example_vault, sync spec"
```

---

## Verification before calling this done

Per the root `CLAUDE.md`, both commands must pass:

```bash
python -m unittest discover -s tests
```

```bash
vault check example_vault
```

And CI additionally requires that `example_vault` be current:

```bash
git diff --exit-code example_vault
```

A smoke run against a real corpus, which is what actually goes into the gap:

```bash
PYTHONPATH=example_vault/scripts python3 -m census.runner <corpus-dir> --out /tmp/census-out
```

Expected: `census: 16 detectors -> /tmp/census-out/census.jsonl`, plus `census-report.md`
and an `examples/` tree.
