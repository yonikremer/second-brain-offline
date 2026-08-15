# Step 03 — Translation: guidance

Read once before filling in `QUESTIONS.md`. This file is reference material for people
making hard calls; it is not needed while answering, and should not be loaded as context
during pipeline work.

---

## Glossary layers

Most organizations share a large vocabulary across nearly all their knowledge, with
smaller term sets specific to each domain and a few cases where one subdomain renames a
shared concept. Record every term at the **most general layer at which it is true**:

| Layer | Holds | Resolution |
|-------|-------|-----------|
| Organization | Terms used across most domains | Base layer |
| Domain | Terms specific to one domain, plus deliberate overrides | Overrides organization |
| Subdomain | Only where a subdomain genuinely renames a shared concept | Overrides domain |

Lookup walks from the most specific layer outward, so the narrowest definition wins.
**An override is always explicit and always carries a reason.** A domain silently
rendering a shared term differently from the organization glossary is exactly the
failure this layering exists to prevent, and the pipeline flags it rather than
accepting it.

A flat glossary per work unit would copy the same organization-wide vocabulary into every
domain and then let the copies drift apart — the same source term rendered one way in one
domain and differently in another. Layering exists to make that impossible rather than
merely discouraged.

## Terms that come from outside the organization

Standards bodies, vendors, and academic literature own their terminology. Their English
form is canonical and must not be re-derived: a source-language document that originally
translated an English standard term has to round-trip back to the *same* English word,
not a plausible synonym. If it lands on a synonym, retrieval silently fragments — half
the corpus says one thing, half says another, and neither looks wrong.

These terms are recorded with their canonical form and a citation of where that form
comes from, and the translation QA gate enforces exact rendering.
