---
title: "Example Source — Example Table"
type: concept
tags: [data-source, table]
sources: []
---

# Example Source — Example Table

**One-sentence description of what this table represents.**

## Purpose

What business or analytical question does this table answer?
How does it relate to the overall data source?

## Schema

| Column | Type | Description | Notes |
|--------|------|-------------|-------|
| `id` | `INTEGER` | Primary key | auto-increment |
| ... | ... | ... | ... |

## Indexes

| Name | Columns | Type | Purpose |
|------|---------|------|---------|
| `idx_...` | `(...)` | B-tree / hash / GIN / etc. | Why it exists |

## Partitioning / clustering

- Partition key: `...`
- Partition strategy: range / list / hash
- Clustering / sort key (if applicable): `...`

## Operational metadata

- **Refresh cadence:** e.g. real-time, hourly, daily
- **Freshness SLA:** expected lag between event and availability
- **Retention / TTL:** how long rows are kept
- **Access control:** who can read/write, any row-level policies
- **Data quality notes:** known nulls, duplicates, or staleness caveats

## Key relationships

- Links to `other-table` via `foreign_key_id` (if applicable).
- Connects to a vault concept note like [[lora]] if the table models that concept.

## Gotchas

- Known data quality issues, nullability quirks, or stale-data caveats.

## Related
[[example-data-source-tables]] · [[data-sources]]
