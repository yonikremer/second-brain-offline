# Step 06 — Success criteria

**Covers:** Parts J, K
**Needs first:** [`../domains.md`](../domains.md), the document types from
[`04-classification/E1`](../04-classification/QUESTIONS.md) (J1 samples across genres),
and the pilot choice from [`05-domain-model/H3`](../05-domain-model/QUESTIONS.md)
**Produces:** gold sample, reference translation set, acceptance tests, definition of done

This step is filled last, but it is not paperwork: J2's labelled sample is what tunes
every filter threshold, and J1's reference translations are the style ground truth the
translation QA gate is calibrated against. Nothing downstream can be measured without it.

Put the reference documents and the labelled sample in this folder.

---

## Part J — Pilot selection

> *Why: calibration needs a small, representative set. These choices determine how well
> every automated threshold is tuned.*

### J1 — Pick 3–5 documents for reference translation.
They should span the genres in E1 — one formal specification, one analytical document,
one team-knowledge page, one informal thread. The expert translates these carefully
(drafting with a model is fine, but review every line); they become the style ground
truth.

| Document | Genre it represents |
|----------|--------------------|

### J2 — Can you label a sample of ~100 documents as in-scope or out-of-scope?
This is what tunes the filter. Documents should be drawn across sources and include
borderline cases, not just obvious ones.

### J3 — Name 5–10 questions the vault must answer correctly for the pilot domain.
With the answers you expect. These become the work unit's acceptance test.

| Question | Expected answer | Source document |
|----------|----------------|-----------------|

---

## Part K — Definition of done

### K1 — What does "this work unit is finished" mean?
Coverage, question accuracy, remaining review queue depth?

### K2 — What would make you say the vault is working?

### K3 — What would make you say it is not?
The failure you would most want to catch early.

---

## Sign-off — Step 06

| Field | Value |
|-------|-------|
| Domain expert | |
| Pipeline operator | |
| Date completed | |

Unanswered questions and `TBD`s, with what would resolve each:

| ID | Blocked on | Owner |
|----|-----------|-------|
