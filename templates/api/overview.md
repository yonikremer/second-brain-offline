---
title: "<API Name> — Overview"
type: concept
tags: [api, overview]
sources: []
---

# <API Name> — Overview

**One-sentence description of the API and its primary purpose.**

## What it is

Describe the service, provider, and protocol (REST, GraphQL, gRPC, etc.).
Include base URL, version, and any authentication scheme.

## Main use cases

- Use case 1 — what endpoint(s) serve it and what decision it enables.
- Use case 2 — downstream consumers or integrations.
- Use case 3 — exploratory or debugging workflows.

## Limits and constraints

- **Rate limits:** tiered quotas, burst limits, window size.
- **Authentication:** required headers, token expiry, refresh flow.
- **Data limits:** max payload size, pagination defaults, result caps.
- **Network:** IP allow-listing, VPN requirements, regional endpoints.
- **Known issues:** deprecated endpoints, versioning quirks, breaking changes.

## Ownership

- **Provider / maintainer:** name / team / contact
- **Consumers:** internal services or teams that depend on this API
- **Change process:** how to request new endpoints or report issues

## Endpoints

See `endpoints.md` for the endpoint guide and `endpoints/*.md` for per-endpoint details.

## Authentication

See `auth.py` for authentication details and sample code.

## Related
[[apis]]
