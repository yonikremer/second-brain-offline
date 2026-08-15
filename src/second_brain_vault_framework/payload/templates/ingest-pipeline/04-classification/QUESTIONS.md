# Step 04 — Classification

**Covers:** Parts B, E
**Needs first:** [`../domains.md`](../domains.md)
**Produces:** [`../sources.md`](../sources.md), plus the trust tier map and document type
vocabulary held in this file

> **B1 has no prerequisites** — it is a factual inventory and can be filled at any
> time. Step 05's Part H reads `../sources.md`, so it must exist before sequencing.

Classification assigns two things to every document: **what kind of document it is**
(Part E) and **how much it is trusted** (Part B). Trust comes from where a document came
from, not from a model's opinion of it, which is why the source map lives here.

---

## Part B — Sources and trust

> *Why: trust comes from where a document came from, not from a model's opinion of it.
> This map is what resolves contradictions between documents later.*

### B1 — What sources feed this corpus?
One row per source: a Confluence space, a file-share directory tree, a mailbox export,
a document library.

**Record the result in [`../sources.md`](../sources.md)** — the single copy step 01 and
step 03 read. Do not restate the inventory here.

### B2 — Assign each source category a trust tier.
Starter ladder — adapt the labels, keep the ordering meaningful:

| Tier | Meaning | Typical sources |
|------|---------|----------------|
| T1 | Global authority — external standards, manufacturer specifications | |
| T2 | Verified internal — approved procedures, controlled documents | |
| T3 | Expert analysis — academic essays, research summaries, opinionated but rigorous | |
| T4 | Team knowledge — internal notes, wiki pages, summaries | |
| T5 | Informal — emails, chat exports, drafts, scratch notes | |

### B3 — When two documents contradict each other, what should happen?
Default rule is "higher tier wins, and the conflict is recorded in the note." Say where
that default is wrong. For example: does recent team knowledge override an older
standard when the standard is known to be outdated in practice?

### B4 — Are there sources that are authoritative for one subject but unreliable for others?
Name them and the split. This is common with vendor documentation.

---

## Part E — Document types

> *Why: the type decides how a document is read — what assumptions to make, how literally
> to take it. The list is frozen before classification starts and grows only by review.*

### E1 — What document types do you expect to find?
Think about how you would *read* each type differently, not about file formats.

| Type | How it should be read | Typical trust tier | Example from the corpus |
|------|----------------------|-------------------|------------------------|

### E2 — Which types carry durable knowledge, and which are ephemeral coordination?
Ephemeral types (logistics, scheduling, announcements) may be filtered or ingested at
low priority. Say which is which.

### E3 — Are there types where only part of the document matters?
For example: meeting minutes where only decisions matter, or reports where only the
findings section is durable.

---

## Sign-off — Step 04

| Field | Value |
|-------|-------|
| Domain expert | |
| Pipeline operator | |
| Date completed | |

Unanswered questions and `TBD`s, with what would resolve each:

| ID | Blocked on | Owner |
|----|-----------|-------|
