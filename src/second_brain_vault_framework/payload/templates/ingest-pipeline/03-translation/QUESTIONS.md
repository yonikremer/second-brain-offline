# Step 03 — Translation

**Covers:** Part I
**Read first:** [`GUIDANCE.md`](GUIDANCE.md) — how glossary layers resolve
**Needs first:** [`../domains.md`](../domains.md), since every term is placed at a layer
**Produces:** translation policy, layered glossary seed

---

## Part I — Language and terminology

> *Why: this becomes the translation policy and the first glossary. Getting it wrong
> means re-translating the corpus later.*

### I0 — Which terms are organization-wide, and which belong to one domain?
A term's spread across domains is the first evidence of its layer: a term appearing
throughout the corpus is organizational, one concentrated in a single domain belongs to
that domain. The extraction pass proposes a layer for each term; this question captures
what you already know before that runs.

| Term | Proposed layer | Notes |
|------|---------------|-------|

### I1 — Which terms must stay in the source language?
Terms where translating would lose meaning or break searchability: product names,
internal project names, standard identifiers, part numbers.

| Term | Why it stays | Preferred rendering |
|------|-------------|--------------------|

### I2 — Which terms have an established English equivalent that must be used?
The start of the glossary. Add as many as you can now — every term answered here is a
question the pipeline will not have to ask later.

| Source term | English | Layer | Notes |
|-------------|---------|-------|-------|

### I2b — Which terms come from outside the organization?
Standards bodies, vendors, academic literature. These already have canonical English
forms that must be used rather than re-derived — a source-language document that
originally translated an English standard term must round-trip back to the *same*
English word, not a synonym. Cite where the canonical form comes from.

| Source term | Canonical English | Where it comes from |
|-------------|------------------|--------------------|

### I2c — Where does a specific domain or subdomain use a shared term differently?
Every deliberate override, with its reason. Anything not listed here is treated as an
accident and flagged.

| Term | Layer that overrides | Rendering there | Why it differs |
|------|---------------------|-----------------|----------------|

### I3 — How should acronyms be handled?
Expanded on first use, kept as-is, translated? Are there acronyms that collide with
common English ones?

### I4 — Are there terms used inconsistently across sources?
Where two teams call the same thing different names, or the same name means different
things. Name the canonical form.

### I5 — Any conventions for units, dates, standards references, or numbers?

---

## Sign-off — Step 03

| Field | Value |
|-------|-------|
| Domain expert | |
| Pipeline operator | |
| Date completed | |

Unanswered questions and `TBD`s, with what would resolve each:

| ID | Blocked on | Owner |
|----|-----------|-------|
