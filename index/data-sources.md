---
title: Data Sources
type: index
tags: [data-sources, registry, templates]
updated: 2026-07-18
---

# Data Sources

Registry of documented data sources in the vault.
Each source lives in its own bundle under `wiki/data-sources/<source-name>/`.
Every file in the bundle uses the `<source-name>-data-source-` prefix so each note
has a unique stem across the whole vault.

## Bundle convention

A valid data-source bundle contains:

1. `<source-name>-data-source-overview.md` — general description, use cases, limits, and maintainer.
2. `<source-name>-data-source-connect.py` — connection snippet with a docstring/comment header.
3. `<source-name>-data-source-tables.md` — conceptual guide to the source’s tables.
4. `tables/<source-name>-data-source-table-<table-name>.md` — one note per table.

Markdown notes are standard concept notes and must cite a raw clipping in `sources:`.
Template to copy: `templates/data-source/`.

## Available sources

*No sources registered yet. Add the first bundle to `wiki/data-sources/`.*

## Related
[[source-registry]] · [[_map-of-content]]
