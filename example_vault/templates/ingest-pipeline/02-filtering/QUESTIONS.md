# Step 02 — Filtering

**Covers:** Parts C, D
**Needs first:** [`../domains.md`](../domains.md) and the out-of-scope list from
[`01-assess/A3`](../01-assess/QUESTIONS.md). `../sources.md` (step 04) enables
path-based rules but is not required to start — Part D needs neither.
**Produces:** scope cards (read by the filter's judge), deterministic filter seed rules,
the protect list

Everything named in Part D becomes a deterministic filter rule, which is cheaper and more
reliable than asking a model. Part C becomes the scope card for the one judgement a rule
cannot make: a well-formed document about the wrong subject.

Put example files referenced by D2 in this folder under `examples/`.

---

## Part C — Scope boundaries

> *Why: this becomes the scope card the filter's judge reads for every document. Concrete
> examples work far better than abstract criteria.*

### C1 — In one paragraph per domain: what does a document have to be about to belong?

### C2 — Give 5–10 examples of documents that clearly BELONG.
Titles or paths, with a word on why.

### C3 — Give 5–10 examples of documents that clearly DO NOT belong.
Titles or paths, with a word on why. Include the tempting near-misses — documents that
look relevant but aren't.

### C4 — What subjects sit right on the boundary?
The cases where you would want to be asked rather than have the pipeline decide.

---

## Part D — Corpus quirks and known junk

> *Why: every pattern named here becomes a deterministic filter rule, which is cheaper
> and more reliable than asking a model.*

### D1 — What kinds of pages carry no knowledge at all?
Examples from this corpus: case-ID pages with a UUID title and no body, navigation and
index pages, blank templates, attachment stubs, archived duplicates.

| Pattern | How to recognize it | Rough share of corpus |
|---------|--------------------|-----------------------|

### D2 — Are there systematic conversion defects?
Text reversal or layout corruption, mangled tables, dropped images, unconverted macros,
encoding damage. **Attach or link 3–5 real examples of each** — these become the test
fixtures for the filter and the repair code.

### D3 — Is there heavy duplication, and where does it come from?
Page copies, exported versions of the same document, templates reused verbatim,
attachments duplicated across spaces. Which copy should win — newest, longest, a
particular location?

### D4 — Are there documents that must never be filtered out, whatever the rules say?
An explicit protect list. Filters check it before anything else.

---

## Sign-off — Step 02

| Field | Value |
|-------|-------|
| Domain expert | |
| Pipeline operator | |
| Date completed | |

Unanswered questions and `TBD`s, with what would resolve each:

| ID | Blocked on | Owner |
|----|-----------|-------|
