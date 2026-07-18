---
title: APIs
type: index
tags: [apis, registry, templates]
updated: 2026-07-18
---

# APIs

Registry of documented APIs in the vault.
Each API lives in its own bundle under `wiki/apis/<api-name>/`.
Every file in the bundle uses the `<api-name>-api-` prefix so each note has a
unique stem across the whole vault.

## Bundle convention

A valid API bundle contains:

1. `<api-name>-api-overview.md` — general description, use cases, limits, and maintainer.
2. `<api-name>-api-auth.py` — authentication snippet with a docstring/comment header.
3. `<api-name>-api-endpoints.md` — conceptual guide to the API's endpoints.
4. `endpoints/<api-name>-api-endpoint-<endpoint-name>.md` — one note per endpoint.

Markdown notes are standard concept notes and must cite a raw clipping in `sources:`.
Template to copy: `templates/api/`.

## Available APIs

*No APIs registered yet. Add the first bundle to `wiki/apis/`.*

## Related
[[source-registry]] · [[_map-of-content]]
