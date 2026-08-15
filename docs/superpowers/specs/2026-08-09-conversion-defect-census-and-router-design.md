# Conversion Defect Census + Reconversion Router — Design

**Date:** 2026-08-09
**Status:** Part 1 (census) approved for planning. Parts 2–3 designed, planned after
Part 1 lands.
**Scope:** Recovering the damaged portion of `raw_md/`. Three parts, strictly sequenced:
a read-only **defect census** that measures what is actually wrong, the **actions** that
consume it, and a **reconversion router** for the subset the census says reconversion can
fix.

**Standing autonomy rule (client decision, 2026-08-09):** nothing runs automatically
except repair, and every automatic action is reviewed before it is trusted to run
unattended. No action promotes itself.

## What this revises

The stage 1–3 acquisition steps (Confluence export → PDF → docling → `raw_md/`, plus the
file-share copy) were declared out of scope and "assumed working" in the
[client ingest pipeline design](2026-08-02-client-ingest-pipeline-design.md). They are not
working. This spec does not re-open stages 1–3 wholesale; it measures their output and
repairs what is repairable.

It also closes an open item in the
[stage-4 filtering design](2026-08-02-stage4-filtering-design.md): the `rtl_corruption`
evidence filter is marked *pending client examples*. Those examples now exist, and the
census detectors are where they land.

## Constraints (inherited)

- **Pure stdlib.** The census runs in the gap. Every detector below is byte- or
  regex-level, so this is achievable with zero dependencies — no docling, no models, no
  network. This is a hard requirement, not a preference.
- **Read-only.** The census never writes into `raw_md/`. Stages never mutate inputs.
- **Designed outside, executed inside.** Scripts are authored here and carried in. Every
  script must be self-diagnosing: it reports what it found rather than assuming, and its
  output is what comes back out.
- **Language:** Hebrew source. Confirmed by the ingest pipeline spec. The RTL detectors
  are Hebrew-specific and would need different rules for Arabic.

## Non-goals

- **No repair in the census.** It measures and routes. Every write belongs to Part 2.
  The census has no `--apply` flag and no write path at all; this is a property of the
  code, not a habit of the operator.
- **No relevance judgement.** Whether a document *belongs* is stage 4's job. This is
  purely "is this file damaged, and by what."
- **No re-splitting.** Detector family 6 reports whether the split is broken; fixing the
  split is separate work, sized once the census says how bad it is.
- **No security screening.** Out of scope corpus-wide, per standing decision.

---

# Part 1 — Defect census

## Contract

Mirrors the `filters.py` registry deliberately, so detectors are portable into the
stage-4 evidence lane rather than rewritten there.

```python
Route = Literal["repair", "reconvert", "resplit", "reroute", "none"]

@dataclass(frozen=True)
class CensusDoc:
    doc_id: str
    title: str
    raw_bytes: bytes          # authoritative; some files are not decodable text at all
    text: str | None          # None when raw_bytes fail to decode
    source_path: str
    sibling_paths: frozenset[str]   # inventory snapshot, for reference resolution

@dataclass(frozen=True)
class DefectFinding:
    detector_id: str
    version: int
    route: Route
    fired: bool
    measures: dict[str, float | int | str]   # the numbers, always, even when not fired
    samples: tuple[Sample, ...] = ()          # excerpt + line number, capped per file
```

Rules the contract enforces, carried over verbatim from the filter contract:

- **One detector, one concern.** No fused predicates.
- **Always return measures**, even when `fired` is False — that is what makes thresholds
  tunable after the fact instead of guessed up front.
- **Detectors are pure.** No I/O, no network. The runner handles persistence.
- **Version integers.** Changing logic or a threshold bumps `version`; census rows store
  `detector_id@version` so stale rows recompute on the next run.

`CensusDoc` diverges from the filter contract's `Doc` in two ways that the corpus forces:

- **`raw_bytes` is authoritative and `text` is nullable.** Family 3 exists precisely
  because some `.md` files are not text, so a contract that only offers a decoded `str`
  cannot express its own inputs. Detectors that need text must handle `None`.
- **`sibling_paths` is passed in, not looked up.** `image_broken_ref` has to know whether
  a referenced file exists, which would otherwise mean filesystem access inside a
  detector and break purity. The runner takes one inventory snapshot up front and hands
  it down.

`route` replaces the filter contract's `lane` because these detectors answer a different
question. A filter decides keep/reject. A detector decides *which repair path applies* —
and that distinction is the whole point of the census, because reconversion cannot fix a
boundary error and re-splitting cannot fix a mangled table.

## Detector catalog v1

### Family 1 — RTL corruption

Three independent sub-modes. They co-occur but are **not** the same bug, and treating
them as one will corrupt files that only had the other.

| ID | Signal | Route |
|----|--------|-------|
| `rtl_char_reversed` | A Hebrew final-form letter (ך ם ן ף ץ) in non-final position within a word run; mirror check for non-final forms (כ מ נ פ צ) at word end | `repair` |
| `rtl_order_reversed` | Line opens with `.`; a `)` preceding its `(`; digit runs that reverse into plausible dates or numbers | `repair` |
| `rtl_bidi_controls` | Presence and density of U+200E, U+200F, U+202A–202E | `none` (evidence) |

The final-form rule is the load-bearing one. Hebrew final forms may only occupy the last
position in a word, so a word run starting with one is character-reversed with very high
confidence — no dictionary, no model, no bidi library. Known false-positive sources:
acronyms marked with gershayim (״), single-letter tokens, and Latin transliterations.
Thresholds are fit against the Phase-0 gold sample, never guessed.

Two correctness rules for the eventual repair, recorded here because they constrain what
the detector must measure:

1. **Repair operates on maximal Hebrew-letter runs, never whole lines.** Reversing a line
   would destroy embedded Latin words, numbers, and punctuation that were never reversed.
2. **Damage is per-run, not per-file.** Extractors routinely emit some runs correctly and
   others reversed within the same document, so the detector records per-run counts and
   the affected share, not a file-level boolean.

### Family 2 — Mangled tables

| ID | Signal | Route |
|----|--------|-------|
| `table_pipe_inconsistent` | Row-to-row pipe-count variance within one table block | `reconvert` |
| `table_cell_fragmented` | Share of cells 1–3 characters long; adjacent cells that concatenate into a plausible word | `reconvert` |
| `table_shape_implausible` | Column count > 12 *(fit)*; empty or separator-only header row | `reconvert` |

Split into three because they fire on different failures and will want different
thresholds. `numeric_table_heavy` in the stage-4 catalog protects legitimate dense spec
tables from being read as damage; these detectors must not double-count it.

### Family 3 — Binary content in `.md`

| ID | Signal | Route |
|----|--------|-------|
| `binary_magic_bytes` | Leading magic: `Rar!\x1a\x07`, `PK\x03\x04`, `RIFF…WAVE`, `%PDF-`, `\x89PNG`, `\xff\xd8\xff`, `\x1f\x8b`, `OggS`, `7z\xbc\xaf\x27\x1c` | `reroute` |
| `binary_byte_ratio` | NUL bytes present; share of bytes outside printable + whitespace | `reroute` |

**These are a salvage opportunity, not junk.** `PK\x03\x04` is also the magic for every
OOXML format, so a `.md` that is secretly a ZIP may be a recoverable `.docx`, `.pptx`, or
`.xlsx`. The detector inspects for `[Content_Types].xml` inside the archive and records
the distinction: recoverable office document → the router's input queue; genuine archive
or media → quarantine with its detected type recorded. Reroute, never silently reject.

### Family 4 — Base64 payloads

| ID | Signal | Route |
|----|--------|-------|
| `base64_data_uri` | `data:<mime>;base64,` URIs, including inside markdown image syntax | `repair` |
| `base64_bare_blob` | Bare `[A-Za-z0-9+/]{N,}={0,2}` runs above a length floor *(fit)* | `repair` |

Both record blob count, total blob bytes, and blob share of file bytes. Share matters
more than count: an embedded blob wrecks chunking and inflates token counts far
downstream, so this is a retrieval-quality defect, not a cosmetic one. Repair is
extract-to-store plus a reference, which the content-addressed store already supports.

### Family 5 — Dropped images

| ID | Signal | Route |
|----|--------|-------|
| `image_placeholder` | Docling's literal `<!-- image -->` marker; `<!-- formula-not-decoded -->` | `reconvert` |
| `image_empty_target` | `![alt]()` or `![]()` with no target | `reconvert` |
| `image_broken_ref` | Image reference resolving to a path that does not exist | `reconvert` |

Reconversion only helps here if image export is actually enabled in the router's docling
invocation — recorded as a router requirement, not an assumption.

### Family 6 — Split damage

| ID | Signal | Route |
|----|--------|-------|
| `split_length_outlier` | Document length in the extreme tails of the corpus distribution | `resplit` |
| `split_opens_midsentence` | First non-whitespace character is lowercase, a conjunction, or mid-clause punctuation | `resplit` |
| `split_no_title` | No title-like first line and no leading heading | `resplit` |

This family exists because the Confluence monolith was split by a header-detection
heuristic that was never validated. It is the only family that can tell you whether the
split needs redoing, and it must be read before any reconversion work is scheduled —
reconverting a mis-split corpus produces cleanly converted garbage.

## Outputs

| Artifact | Contents |
|----------|----------|
| `census.jsonl` | One row per file per detector: `doc_id, detector_id@version, route, fired, measures, samples`. Append-only, joins to the ledger by `doc_id`. |
| `census-report.md` | Per-detector fire counts and corpus share, route totals, the length distribution behind family 6, and a co-occurrence matrix showing which defects travel together. |
| `examples/<detector_id>/` | Harvested real excerpts, capped per detector. |

The harvested examples are a deliverable, not a convenience: Step 02 question **D2**
requires 3–5 real examples of each conversion defect as the test fixtures for the filter
and the repair code. The census fills that in from the corpus instead of by hand.

The co-occurrence matrix is what turns the report into a plan. A file that is both
mis-split and RTL-damaged must be re-split before it is repaired, or the repair is thrown
away with the boundaries.

## Module layout

```
scripts/census/
  __init__.py
  detectors.py    # every detector + the registry, mirroring filters.py
  runner.py       # walks the corpus, persists rows, writes the report
  DETECTORS.md    # one section per detector: what it catches, threshold + derivation, examples
  fixtures/       # hand-curated positive/negative cases, seeded from examples/
```

A test asserts every registered detector ID appears in `DETECTORS.md` and vice versa, so
the documentation cannot drift — the same guard the filter catalog uses.

## Testing

- Per-detector fixture tests: known-damaged and known-clean inputs, asserting `fired` and
  the measures, not just the boolean.
- The RTL detectors get adversarial fixtures specifically: legitimate Hebrew acronyms,
  mixed Hebrew/Latin/digit lines, and correctly-ordered Hebrew that must not fire.
- A whole-corpus smoke run asserting the census completes and emits a report; runtime is
  expected in minutes over ~2 GB of markdown, single pass, no per-file process spawn.

## Success criteria

1. Every file in `raw_md/` has a census row for every registered detector.
2. Each of the five client-reported defect classes has a detector that fires on the
   client's real examples and stays silent on hand-picked clean controls.
3. The report answers, with numbers: how much of the corpus is mis-split, how much is
   repairable in place, how much needs reconversion, and how much is not damaged at all.
4. D2 of Step 02 can be filled in from the report and `examples/` alone.

---

# Part 2 — Actions

Every route in the catalog names an action. This part specifies what each one actually
does, what it must not touch, how it is verified, and how much autonomy it has.

## Autonomy levels

| Level | Meaning | Who is here now |
|-------|---------|-----------------|
| `propose` | Computes the change, writes it to a worklist, executes nothing. | `reconvert`, `resplit`, `reroute` |
| `review` | Executes, and queues every applied change for human review. Reversible by construction. | `repair` |
| `auto` | Executes, sampled audit only. | **Nothing.** Reachable only by promotion. |

Two rules govern the table:

1. **`propose` is the default, and the runner requires `--apply` to leave it.** A run
   with no flags can only ever produce a worklist.
2. **Promotion is a human act, and is never itself automatic.** A detector becomes
   eligible for `auto` after N consecutive reviewed applications with zero rejections,
   but eligibility is not promotion — someone records the decision, with the evidence,
   in `ACTIONS.md`. Any rejection resets the counter to zero.

## Why automatic repair is safe here

Repair runs at `review` rather than `propose`, and that is only defensible because of a
convention already standing in the pipeline: **stages never mutate inputs.** A repair does
not edit a file. It writes a new artifact into the content-addressed store and moves a
ledger pointer, recording `repaired_from=<hash>` and the `detector_id@version` responsible.

The original bytes are still in the store, unchanged, forever. So "automatic" here means
*a new candidate was produced and the pointer moved* — not *your data was overwritten*.
Reverting is a pointer change, and reverting a whole batch is reverting one commit. If
that convention is ever broken, repair must drop back to `propose`.

## The actions

### `repair` / `rtl_char_reversed` — reverse the run back

**Operates on:** each maximal run of Hebrew letters (plus intra-word niqqud, geresh, and
gershayim) that the detector flagged. Character order within the run is reversed.

**Must not touch:** runs the detector did not flag; embedded Latin words and digit
sequences, which were never reversed and would be destroyed by reversing them; and all
markdown structure — link targets, table pipes, HTML comments, frontmatter, and the
interior of fenced code blocks, which are skipped wholesale.

**Verification, and the reason this one can be trusted:** the repair is self-checking.
Re-run the detector on the output; the final-form invariant that identified the damage
must no longer fire. If it still fires, the repair did not work — the file is re-routed
to `reconvert` rather than left in a half-fixed state. Very few repairs get an objective
success test; this one does, because the invariant is a property of the language rather
than a guess about the content.

**Records:** runs repaired, runs skipped, and the post-repair detector result.

### `repair` / `rtl_order_reversed` — reverse the word sequence

**Operates on:** the word sequence within a flagged line or segment.

**Must not touch:** anything outside the flagged segment; intra-word character order,
which is `rtl_char_reversed`'s concern and independently determined.

**This one has no self-check, and that is a real asymmetry.** Word-order reversal produces
text that looks structurally valid in either direction — there is no invariant like the
final-form rule to verify against, so only a Hebrew reader can confirm the result reads
correctly. It should stay at `review` well past the point where `rtl_char_reversed`
graduates, and I would not promote it to `auto` at all without the expert explicitly
signing off on that specific detector. Segment boundaries in mixed-direction lines are
genuinely ambiguous, and this is where I expect the rejections to come from.

### `repair` / `base64_data_uri`, `base64_bare_blob` — extract and reference

**Operates on:** each detected blob. Decode it, write the bytes into the content-addressed
store keyed by their hash, replace the inline blob with a reference to it.

**Must not touch:** any blob that fails to decode, or that decodes to bytes matching no
known image magic. Those are recorded as findings and left in place — an undecodable blob
is a different defect, and guessing at it would destroy evidence.

**Verification:** decoded bytes match a known image signature, and the rewritten document
still parses as markdown.

**Records:** blob count, bytes reclaimed, store hashes written. The payoff is downstream —
inline blobs wreck chunking and inflate token counts through every later stage.

### `propose` / `reconvert` — produce a candidate, prove it is better

**Produces:** a worklist of documents whose defects reconversion could plausibly fix
(mangled tables, dropped images), each with the source file, the proposed engine and
settings, and the current defect counts.

**When it runs (Part 3), acceptance is machine-checkable and blind overwrite is
forbidden.** The router converts to a *candidate*, runs the census over the candidate, and
compares against the incumbent. A candidate is accepted only if it fires strictly fewer
detectors on the same document and introduces no detector the incumbent did not fire.
Salvage that cannot be measured is not salvage — without this test, "reconvert and hope"
can quietly trade a table defect for an RTL defect and call it progress.

### `propose` / `resplit` — report boundary damage, change nothing

**Produces:** the suspected-boundary-error list with its evidence — length outliers,
documents opening mid-sentence, documents with no title.

**Why it cannot act:** `doc_id` is `f(source path, content hash)`, so re-splitting mints
new identities and orphans every downstream artifact keyed to the old ones. Re-splitting
is therefore not a repair but a re-entry, and it needs a new splitter plus a migration
story for anything already derived. Both are separate work, correctly sized only once this
report says how much of the corpus is affected.

**Read this one first.** If the split is badly broken, reconversion scheduled before
re-splitting produces cleanly converted garbage.

### `propose` / `reroute` — manifest the non-markdown, extract nothing

**Produces:** a manifest of every `.md` that is not markdown — detected type from magic
bytes, whether a ZIP is a recoverable OOXML document (contains `[Content_Types].xml`) or a
genuine archive, and a proposed destination: the router's input queue for recoverable
office documents, quarantine for everything else.

**Why it stays at `propose` permanently:** the action here is unpacking archives of unknown
provenance and moving files between trees. Neither should happen without someone looking
at the manifest first, and unlike repair, neither is a pointer change that reverts cleanly.

### `none` / `rtl_bidi_controls` — evidence only

Recorded in the census, never acted on. Bidi control characters are diagnostic of how the
extractor behaved; they inform the other RTL detectors and the eventual router settings,
and stripping them would erase that signal.

## The review queue

Every `review`-level application and every `propose` worklist entry writes one queue row:
`doc_id`, detector, the before and after excerpts side by side, the measures, and a
verdict field.

- **The reviewer is the domain expert**, not the operator. Every RTL verdict requires
  reading Hebrew, so this cannot be delegated to the pipeline.
- **Rejections are the most valuable output.** Each becomes a fixture in
  `fixtures/`, so a rejected repair can never silently recur.
- **The queue is sampled, not exhaustive, above a volume threshold** — sized by the
  Phase-0 autonomy dial, consistent with the rest of the pipeline. Below the threshold,
  everything is reviewed.

`ACTIONS.md` documents one section per action: what it does, its current autonomy level,
the promotion evidence to date, and the version history. A test asserts every action in
the code appears there and vice versa, the same drift guard the filter and detector
catalogs use.

## Module layout

```
scripts/actions/
  __init__.py
  actions.py      # every action + the registry; autonomy level is declared here
  runner.py       # --apply gate, store writes, ledger pointer moves, queue rows
  queue.py        # review queue read/write and verdict recording
  ACTIONS.md      # per action: what it does, autonomy level, promotion evidence, versions
  fixtures/       # accepted and rejected cases, rejections promoted from the queue
```

Actions are separate from `scripts/census/` on purpose. The census must remain a module
with no write path, and the cleanest way to guarantee that is for the writing code to live
somewhere else entirely.

---

# Part 3 — Reconversion router

Designed here so Part 1's outputs have a known consumer. Planned and built after Part 1
lands and its numbers are read.

## Engine selection

Two decisions, deliberately separate, because docling and markitdown overlap heavily on
PDF/DOCX/PPTX/XLSX/HTML and "docling if supported, else markitdown" conflates them:

1. **Preferred engine by extension**, from a rules table.
2. **Fallback chain on failure**, per engine, so a docling failure can retry under
   markitdown and be recorded as such.

Every conversion records which engine and settings produced it. An output whose engine is
unknown cannot be compared against its predecessor, and comparison is the whole point.

## Surviving a docling crash

Docling on malformed PDFs does not always raise a catchable exception — it can die in the
native PDF layer or be OOM-killed, which no `try`/`except` intercepts. The design
consequence:

1. **Write the ledger row before attempting the file**, `status: in_flight`, flushed and
   fsynced. On restart, any row still `in_flight` is by definition the file that killed
   the process. Mark it poison, skip it, continue. This is what makes "we know where to
   come back to" work, and it must be designed in rather than bolted on.
2. **Per-file subprocess isolation** for docling, so a hard crash becomes a non-zero exit
   code and a hang becomes a timeout. Costs process startup per file; worth it.
3. **A persistent poison list**, so repeated runs do not re-crash on the same file.
4. **Atomic writes** — temp name, rename into place, then update the ledger. Standing
   convention.

## The monolith

The Confluence export is one PDF of 3800+ pages. Handing it to docling whole risks OOM
and makes every crash a total loss. If it is reconverted, it is chunked first with a
cheap pure-Python page-range pass (no models, no OCR), and each chunk becomes an
independently resumable unit. Whether reconversion is warranted at all depends on family
6: if the split is the primary damage, the fix is a better splitter, not a better
converter.

## Open inputs

| Item | Blocked on |
|------|------------|
| Is markitdown installed in the gap? | Docling is present; markitdown is unverified. If absent, the file-share path needs wheels carried in — the `airgap-pack` skill covers this, and it has lead time. |
| Docling version and backend in the gap | Determines RTL behaviour and whether image export is available. The census cannot answer this; a probe can. |
| File-share extension histogram | Produced by the census's inventory pass. |
