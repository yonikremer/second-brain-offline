# Ingest pipeline — planning

**Fill these in before any document is filtered, translated, or ingested.**
The answers drive the entire pipeline: what gets filtered out, what order things are
learned in, which source wins when two documents disagree.

The planning is split into steps so that no single file has to be read — by a person or
by a model — in order to work on one subject. Each step is a folder holding the questions
for that subject, the guidance needed to answer them, and its own sign-off.

**The steps follow the pipeline, not the dependency graph.** Assess first, then the
stages in the order documents move through them, then the domain model, then success
criteria. This ordering exists because much of the domain model is *discovered* from the
corpus rather than known up front — and the corpus cannot be read until it has been
filtered and translated.

---

## How to use this

**Who fills it in:** the domain expert and the pipeline operator, together, one step per
focused session. The expert owns the knowledge questions; the operator owns the
mechanical ones and writes down what the expert says.

**How to answer:** write directly under each question. Tables are filled in as rows;
free-text questions take a paragraph. "I don't know yet" is a valid answer — mark it
`TBD` and note what would resolve it. A wrong guess costs more than an admitted gap.

**Question IDs (A1, B2, …) are stable and are not renumbered when steps move.** Later
documents, decisions, filter rules, and open tasks cite them. Reference a question by
step and ID — `02-filtering/C3` — so the reference says where to look without changing
what it points at.

**`GUIDANCE.md` is read once, by a person.** It holds the reasoning needed to make hard
calls. It is not needed while filling in answers and should not be loaded as context
during pipeline work.

**What the pipeline reads is the artifact, not the questions.** Each step produces a
compact output — a scope card, a trust map, a type vocabulary, a glossary seed. Those are
what downstream stages consume. If a pipeline stage is loading these question files, that
is a defect in the stage, not a reason to shorten the questions.

---

## Shared artifacts

Two outputs are needed by nearly every step, so they live at this level rather than
inside the step that produces them:

| File | Produced by | Refined by | Read by |
|------|------------|-----------|---------|
| [`domains.md`](domains.md) | `01-assess/A1`, `A2` | `05-domain-model/A7` | 02, 03, 04, 05, 06 |
| [`sources.md`](sources.md) | `04-classification/B1` | — | `05-domain-model/H1`, 02 |

> **One copy only.** Never restate either list inside a step file. A second copy drifts
> the moment the completeness loop in `A7` adds a domain, and then two steps are scoped
> to different maps. Link to the shared file instead.

---

## Steps

| # | Step | Covers | Needs first | Produces | Status |
|---|------|--------|-------------|----------|--------|
| 01 | [Assess](01-assess/QUESTIONS.md) | A1–A5 | — | `domains.md` (domains + subdomains), out-of-scope list, authority map | |
| 02 | [Filtering](02-filtering/QUESTIONS.md) | C, D | `domains.md`, A3 | Scope cards, filter seed rules, protect list | |
| 03 | [Translation](03-translation/QUESTIONS.md) | I | `domains.md` | Translation policy, layered glossary seed | |
| 04 | [Classification](04-classification/QUESTIONS.md) | B, E | `domains.md` | `sources.md`, trust tier map, document type vocabulary | |
| 05 | [Domain model](05-domain-model/QUESTIONS.md) | A6, A7, F, G, H | `domains.md`, `sources.md`, a processed corpus | Dependency graph, layer ordering, work sequence | |
| 06 | [Success criteria](06-success-criteria/QUESTIONS.md) | J, K | `domains.md`, E1, H3 | Gold sample, reference set, acceptance tests, definition of done | |

Steps sign off independently. Filtering can be signed off and running while translation
is still open — that is the point of the split.

### Two passes, not one line

Some questions are answerable immediately; others need a corpus you can actually read.
Do not stall on the second group in week one, and do not invent a provisional domain map
just to fill them in.

| | Answerable from the start | Second pass — needs a processed corpus |
|---|---|---|
| Assess | A1, A2, A3, A4, A5 | A1/A2's corpus angle → refined by `A7` |
| Filtering | D1–D4 (junk, defects, duplication, protect list) | C1–C4 (scope is defined per domain) |
| Translation | I1, I2b, I3, I5, and I2's organization-layer rows | I0, I2c, I2's domain-layer rows |
| Classification | B1–B3, E1–E3 | B4 |
| Domain model | — | all of it |
| Success criteria | — | all of it |

---

## What this produces

| Output | Built from | Used by |
|--------|-----------|---------|
| Domain and subdomain map | A1, A2 → `domains.md` | Every stage |
| Source inventory | B1 → `sources.md` | Work-unit definition, filtering |
| Scope cards | A3, C | Filtering (scope judge), classification |
| Filter seed rules | D | Filtering (deterministic gates) |
| Trust tier map | B2–B4 | Conflict resolution, ingest order |
| Document type vocabulary | E | Classification |
| Knowledge layer ordering | F | Ingest order within a work unit |
| Contested-split record | A5 | Stops settled arguments being reopened |
| Dependency graph and sequence | A6, G, H | Work sequencing, overlap ownership |
| Translation policy and glossary seed | I | Translation |
| Pilot selection | J | Calibration (gold sample, reference translations) |
| Definition of done | K | Work unit QA gate |

---

## Open items across all steps

Anything marked `TBD` in any step, with what would resolve it.

| Step | ID | Blocked on | Owner |
|------|----|-----------|-------|

## Overall sign-off

Only complete when every step is signed off in its own file.

| Field | Value |
|-------|-------|
| Domain expert | |
| Pipeline operator | |
| Date completed | |
| Version | 1 |
