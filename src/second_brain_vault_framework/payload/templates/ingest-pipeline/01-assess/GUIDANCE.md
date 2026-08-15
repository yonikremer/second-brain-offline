# Domain model — guidance

Used by step 01 (the initial domain list) and step 05 (subdomains, dependencies,
layering). One copy, linked from both.

Read once before filling in `QUESTIONS.md`. This file is reference material for people
making hard calls; it is not needed while answering, and should not be loaded as context
during pipeline work.

---

## How to decide what is a domain and what is a subdomain

Do not try to settle this by subject matter — that argument has no end. Decide it by
**which artifacts the pipeline would have to duplicate.** Each domain gets exactly one
of each of these:

- a scope card (what belongs, what does not)
- a glossary and translation policy
- a trust map (which sources win when documents disagree)
- a concept namespace in the wiki
- an acceptance test (the questions it must answer)

**The test:** if two candidate areas would share all five, they are **one domain with two
subdomains**. If they would genuinely need different glossaries, different reading
conventions, and different trust maps, they are **two domains**.

Three corollaries that resolve most arguments:

1. **Size is not a criterion.** A domain that is too big to process at once stays one
   domain and is processed in several ordered work units. Splitting a domain to make it
   smaller duplicates its shared core, which is the outcome you least want.
2. **"You need A to understand B" means A and B are in the same domain**, ordered
   foundational-first — or A is a separate domain that B *hard-depends* on (A6). Mutual
   prerequisite knowledge is the strongest evidence of a single domain.
3. **Teams are not domains, and depth is not a domain.** Several teams may work across
   the same domain, and the same domain may be known at different depths by different
   audiences. Depth is recorded per note as a knowledge level, never as a separate
   domain — otherwise the same concept exists twice.
4. **If comparing two things is a primary use case, they belong in one namespace.**
   Comparison across domains is a join; comparison inside a domain is a lookup. Never
   split apart the very things the team exists to compare.

## Subject domains versus system domains

Most corpora contain two different kinds of knowledge, and separating them is usually
the highest-value cut available:

- **Subject domains** describe external reality — the things being studied. Test:
  *would this still be true if we replaced our own systems tomorrow?* If yes, it is
  subject knowledge.
- **System domains** describe the organization's own machinery — collection, processing,
  storage, tooling, outputs. Test: *would this fact change if we rebuilt our stack?* If
  yes, it is system knowledge.

**Do not mirror one into the other.** It is common to find that the system's structure
echoes the subject's structure (a processing path per subject family, a tool per subject
area). Resist creating a shadow domain per subject inside the system half: the shadows
duplicate the subject vocabulary and then drift out of step with it. Keep **one** system
domain per lifecycle stage, and have its notes **link** to the subject concepts they
handle. The insight that "understanding our processing deepens understanding of the
subject" is real, and it is delivered by those links and by joint querying — not by
parallel hierarchies.

## Cross-cutting domains: split the core from the body

Some domains touch everything (collection sources, device or platform knowledge,
shared infrastructure). Treating such a domain as a prerequisite for every other domain
front-loads an enormous amount of work; treating it as purely enriching lets avoidable
misreadings through. Split it instead:

- a **small prerequisite core** — the minimum without which other domains' documents
  would be misread (typically 5–15% of the domain), ingested early
- the **enriching body** — the rest, ingested on its own schedule, connected by links

## Foundation domains and shared cores

Two different things get called "shared knowledge", and they need opposite treatment.

**A foundation domain** is a substantial, coherent body of lower-level knowledge that
other domains are built on top of — a physical or transport layer, a base science, a
regulatory framework. It has its own vocabulary and its own authority, and it usually
holds the corpus's most authoritative and most stable documents (standards,
specifications, textbooks). It is a full domain in its own right, ingested before the
domains that depend on it, and it is a **hard** prerequisite — though the core/body
split above still applies, so only its core blocks other work.

**Specializations of a foundation layer belong to the layer, not to the consumer that
uses them.** A sophisticated mechanism built on the base layer and used by only one
consumer is still layer knowledge. Test: does the document read like a layer document
that assumes fluency in the layer, or like a consumer document that happens to mention
it? Put it where its vocabulary lives and link to it from the consumer — otherwise, when
a second consumer adopts something similar, the two cannot be compared without a
cross-domain join.

**A shared core** is different: the small set of orphan concepts that several domains
use and none owns naturally. Keep it disciplined with a promotion-only rule — a concept
moves there when **two or more domains need it and neither is its natural home**, never
by default. Started otherwise it becomes a dumping ground. If a candidate shared core
starts growing into a coherent body with its own vocabulary, it was a foundation domain
all along; promote it.
