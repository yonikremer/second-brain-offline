---
title: "<Source Name> — Overview"
type: concept
tags: [data-source, overview]
sources: []
---

# <Source Name> — Overview

**One-sentence description of the data source and its primary purpose.**

## What it is

Describe the system, service, or dataset. Include the technology (PostgreSQL, S3,
Snowflake, internal API, etc.) and the organizational context.

## Main use cases

- Use case 1 — who queries it and for what decision.
- Use case 2 — downstream consumers or reports.
- Use case 3 — ad-hoc analysis or exploratory work.

## Limits and constraints

- **Volume / scale:** approximate row counts, file sizes, or daily growth.
- **Latency / freshness:** how often data is loaded or refreshed.
- **Retention:** how long data is kept and any purge policy.
- **Access:** who can read/write, network constraints, credentials.
- **Known issues:** schema drift, unreliable fields, deprecation warnings.

## Ownership

- **Maintainer:** name / team / contact
- **Stakeholders:** teams that depend on this source
- **Change process:** how to request schema or access changes

## Tables

See `tables.md` for the table guide and `tables/*.md` for per-table details.

## Connection

See `connect.py` for connection details and sample code.

## Related
[[data-sources]]
