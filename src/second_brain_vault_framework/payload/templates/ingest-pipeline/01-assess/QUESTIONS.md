# Step 01 — Assess

**Covers:** A1–A5
**Read first:** [`GUIDANCE.md`](GUIDANCE.md) — the rules for deciding what is a domain
**Needs first:** nothing — this is the entry point
**Produces:** [`../domains.md`](../domains.md) (domains and subdomains), the out-of-scope
list, the authority map, and the contested-split record

The minimum needed to start processing: what this corpus is about at domain and subdomain
level, what is explicitly not in it, and who decides when the pipeline asks a question.
Subdomains belong here because they are usually where the actionable granularity lives —
a handful of broad domains says little on its own.

What waits for [step 05](../05-domain-model/QUESTIONS.md) is everything that needs a
*readable* corpus: the hard/soft dependency graph, the completeness loop, layering,
overlaps, and sequencing.

Expect this list to be **provisional and to change.** The completeness loop in `A7` exists
to correct it against the real corpus later; do not stall here trying to get it perfect.

---

## Part A — Domains and subdomains

> *Why: the domain is the top-level partition of the vault. Domains are learned
> separately but queried together, so the boundaries decide what gets built when.*

### A1 — What domains does the vault need to cover?
Build this list from **several independent angles**, not one pass — the point is to catch
what a single top-down attempt forgets. Work through each angle, then merge:

- **From the business:** what does this campaign do, what are its outputs and missions?
- **From the corpus:** what do the source folders, space trees, and recurring title terms
  cluster into? Include clusters that match no domain you had thought of.
- **From the people:** what does each team or expert know, and what do they get asked?
- **From the questions:** what do people actually come to this team to find out?

At this stage the corpus angle is necessarily coarse — folder and space names are legible
before anything has been processed, but document-level clustering is not. Take what the
structure gives you and leave the rest to `A7`.

**Record the result in [`../domains.md`](../domains.md)** — the single copy every other
step reads. Do not restate the list here.

### A2 — For each in-scope domain, what subdomains does it break into?
Only one level down. Subdomains share the domain's glossary, scope card, and trust map —
if a candidate subdomain would need its own, revisit A5.

**Record these in [`../domains.md`](../domains.md) alongside their domain.** With few
broad domains and many subdomains, the subdomain is the level most steps actually work
at, so it belongs in the shared artifact rather than here.

### A3 — Which domains are explicitly out of scope, and why?
Name them. Out-of-scope domains that appear in the corpus are the hardest filtering
case — well-formed documents about the wrong subject — so the filter needs to know
what they look like.

### A4 — Who is the authority for each in-scope domain?
The person who adjudicates when the pipeline asks a question. If it is the same person
for everything, say so; if a domain has no available authority, flag it now.

| Domain | Authority | Availability |
|--------|-----------|--------------|

### A5 — Apply the test to every candidate split you argued about.
For each area where you debated one domain versus several, record the decision and the
reason. This is the record that stops the argument being reopened every month.

| Candidate split | Shared glossary? | Shared scope card? | Shared trust map? | Decision | Why |
|-----------------|-----------------|-------------------|------------------|----------|-----|

---

## Sign-off — Step 01

| Field | Value |
|-------|-------|
| Domain expert | |
| Pipeline operator | |
| Date completed | |

Unanswered questions and `TBD`s, with what would resolve each:

| ID | Blocked on | Owner |
|----|-----------|-------|
