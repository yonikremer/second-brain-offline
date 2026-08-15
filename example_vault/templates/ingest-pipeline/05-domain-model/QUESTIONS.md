# Step 05 — Domain model

**Covers:** A6, A7, F, G, H
**Read first:** [`../01-assess/GUIDANCE.md`](../01-assess/GUIDANCE.md) — the same rules,
now applied in detail
**Needs first:** [`../domains.md`](../domains.md) from step 01, plus a filtered and
translated corpus — `A7` routes real documents, and `F` needs to see what the sources
actually contain
**Produces:** refined `../domains.md`, the dependency graph, layer ordering, and the
work sequence

Step 01 answered *what domains and subdomains exist*. This step answers *how they depend
on each other and in what order they get built* — which is far easier now that the corpus
has been filtered, translated, and typed, and which is what `A7` needs in order to check
the map against real documents.

---

## Part A — Dependencies and completeness

### A6 — Classify the dependencies between domains as hard or soft.
This distinction controls sequencing, and getting it wrong is expensive in both
directions — too many hard edges and nothing can start, too few and shallow notes get
written before the foundations that should have shaped them.

- **Hard (prerequisite):** the dependent domain cannot be understood without it. Creates
  a real ordering constraint — the prerequisite is ingested first.
- **Soft (enriching):** the dependent domain can be known well without it, but it adds
  depth. **Creates no ordering constraint.** The connection is made by links between
  concepts, and joint querying surfaces it whenever both are present.

| Domain A | Domain B | A is prerequisite / enriching for B | Evidence |
|----------|----------|-------------------------------------|----------|

> Only hard edges go into the sequencing plan in Part H. Broad, basic domains are
> frequently *enriching* rather than prerequisite; treating them as prerequisites
> front-loads a large amount of work that the priority domains do not actually need.

### A7 — Completeness check (repeat until stable)
Do not rely on memory to be sure the map is complete. Close the loop against the corpus:

1. Route a sample of the corpus against the current domain list.
2. Inspect everything that lands in **unassigned**.
3. Each unassigned document is one of: junk (a filter rule), out of scope (A3), or
   **evidence of a domain you forgot** (add it and repeat).

Update [`../domains.md`](../domains.md) with anything found, and record the iteration:

| Iteration | Date | Unassigned share | Domains added | Notes |
|-----------|------|-----------------|---------------|-------|

> Adding a domain later is cheap — a scope card, a glossary, and a re-run of a
> deterministic routing pass. The map has to be good enough to start the pilot, not
> perfect before it.

---

## Part F — Knowledge layers

> *Why: ingestion runs from foundational to advanced, so that later documents attach to
> concepts that already exist instead of creating duplicates.*

### F1 — For each domain, what is the foundational layer?
The documents that define the base vocabulary and concepts — what a newcomer must read
first. Usually standards, specifications, or formal internal documentation.

| Domain | Foundational sources or documents |
|--------|----------------------------------|

### F2 — What builds on top of that foundation, and in what order?
Sketch the layers: foundation → applied/analytical → team practice → informal notes.
Where a layer depends on another domain's foundation, say so.

### F3 — Are there documents that only make sense after specific others?
Name the pairs. These become explicit ordering constraints.

### F4 — What must be true before a domain is worth querying at all?
The minimum set of concepts that has to exist for answers to be useful.

---

## Part G — Overlaps between work units

> *Why: two sources covering the same subject is the main risk of duplicate or
> contradictory notes. Deciding ownership up front turns the overlap into the
> connection that makes cross-domain querying work.*

### G1 — Which pairs of domains or sources cover overlapping subject matter?

| A | B | What overlaps | Which should be ingested first | Why |
|---|---|--------------|-------------------------------|-----|

### G2 — For each overlap, what should happen when the second work unit arrives?
The default is: the existing note is extended, sources are added, and any disagreement
is flagged for you. Say where that is wrong.

### G3 — Are there concepts that genuinely mean different things in different domains?
Same word, different meaning. These must stay as separate notes, and the pipeline needs
to know so it doesn't merge them.

---

## Part H — Work units and sequencing

> *Why: a work unit is one batch — filtered, translated, classified, ingested, and
> checked together. Their order is the project plan.*

### H1 — Define the work units.
A work unit is usually one domain, sometimes one source, sometimes a slice of both — so
this reads both [`../domains.md`](../domains.md) and [`../sources.md`](../sources.md).

| Work unit | Definition (which documents) | Rough volume | Depends on |
|-----------|------------------------------|--------------|-----------|

### H2 — What is the order, and what forces it?
Hard dependencies from A6, layer ordering from F, overlap ownership from G, expert
availability, client priority.

### H3 — Which work unit is the pilot, and why?
Pick one that is valuable enough to prove the system and small enough to finish.

### H4 — What is the deadline or demo pressure, if any?

---

## Sign-off — Step 05

| Field | Value |
|-------|-------|
| Domain expert | |
| Pipeline operator | |
| Date completed | |

Unanswered questions and `TBD`s, with what would resolve each:

| ID | Blocked on | Owner |
|----|-----------|-------|
