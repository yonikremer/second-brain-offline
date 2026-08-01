# Human Review Queue — Design

## Overview

The document pipeline (`process.py`) currently blocks on interactive `input()` calls when it encounters ambiguous Hebrew terms, new classification categories, or uncertain truthness scores. This is unacceptable for batch runs over thousands of enterprise documents.

This design introduces a **human review queue**:
- **Canonical state lives in SQLite** (`pipeline.db`).
- **Review items are materialized as editable markdown files** under `review/` so reviewers can use VSCode's markdown viewer and editor.
- The batch pipeline **never blocks**; it inserts queue items and continues processing other files.
- A small CLI (`scripts/review.py`) applies reviewer decisions and re-runs affected downstream stages.

## Goals

1. Remove all interactive `input()` from `process.py`.
2. Keep the pipeline running unattended through ambiguous cases.
3. Let expert reviewers answer translation clarifications, classification disputes, and truthness overrides in a comfortable file-editing workflow.
4. Ensure decisions persist and downstream stages are re-run automatically once resolved.

## Non-goals

- Real-time web UI for review (can be added later).
- Multi-user concurrency control beyond "pipeline never overwrites a human-edited file."
- Automatic re-queueing of already-resolved items when the raw file changes.

## Triggers

A review item is created when any of the following stages cannot proceed with confidence:

| Stage | Trigger | Reason |
|-------|---------|--------|
| `translation` | `clarification` | Unknown internal term, acronym, or code name ("Zero-Guessing Rule"). |
| `subdomain` | `new_category` | LLM proposes a subdomain not in `instructions/subdomains.md`. |
| `doc_type` | `new_category` | LLM proposes a document type not in `instructions/document_types.md`. |
| `truthness` | `low_score` | Score falls below a configurable threshold (default: 4). |
| `truthness` | `parse_failure` | LLM response is not valid JSON. |

## Data Model

### New table: `review_queue`

```sql
CREATE TABLE IF NOT EXISTS review_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_hash TEXT NOT NULL,
    filepath TEXT NOT NULL,
    stage TEXT NOT NULL,
    trigger_type TEXT NOT NULL,
    context_json TEXT NOT NULL,
    proposed_answer TEXT,
    human_answer TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    resolution_note TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    resolved_at TIMESTAMP,
    UNIQUE(file_hash, stage, trigger_type)
);
```

- `context_json` is a JSON blob whose shape depends on the trigger (see Trigger Contexts below).
- `status` values: `pending`, `accepted`, `rejected`, `stale`.
- The unique constraint prevents duplicate queue items for the same file/stage/trigger.

### Trigger contexts

**`clarification`**
```json
{
  "term": "פרויקט ארז",
  "context_sentence": "בפרויקט ארז אנחנו משתמשים ב-K8s cluster..."
}
```

**`new_category`**
```json
{
  "proposed_value": "security",
  "existing_values": ["tech", "other"],
  "focus_hint": "Added automatically in non-interactive run."
}
```

**`low_score` / `parse_failure`**
```json
{
  "raw_response": "{\"score\": 2, ...}",
  "parsed_score": 2,
  "parsed_justification": "..."
}
```

## Review File Format

Each pending item is materialized as:

```
review/<original-filename>--<short-hash>--<stage>--<trigger>.md
```

Example:

```
review/onboarding.docx--a3f7d2--translation--clarification.md
```

The original filename comes first so reviewers can browse the directory naturally. The short hash (first 8 chars of `file_hash`) disambiguates files with the same name from different `raw/` subdirectories.

### File contents

```markdown
---
queue_id: 42
file_hash: a3f7d2e9...
filepath: raw/team/onboarding.docx
stage: translation
trigger: clarification
status: pending
proposed_answer: ""
human_answer: ""
resolution_note: ""
---

# Review Needed: Translation Clarification

**File:** `raw/team/onboarding.docx`  
**Stage:** translation  
**Trigger:** clarification

## Term/Issue
"פרויקט ארז"

## Context
> בפרויקט ארז אנחנו משתמשים ב-K8s cluster...

## Proposed answer
*(none — LLM could not infer this term)*

## Your answer
Edit `human_answer` in the frontmatter, then change `status` to `accepted` or `rejected`.
```

The reviewer edits only the YAML frontmatter. The body is informational.

## Pipeline Integration

### Inserting a review item

When a stage needs human input, `process.py`:

1. Inserts a `pending` row into `review_queue`.
2. Writes the corresponding `review/` file **if it does not already exist**.
3. Sets the file's `files.status` to `needs_review`.
4. Stops processing that file for this run and continues with the next file.

No downstream stages run for a file with pending review items that block them.

### Stage-specific behavior

**Translation clarification**
- The file stops at the `translation` stage.
- `files.status` = `needs_review`.
- On `accepted`: append the term/translation pair to `glossary.md`, re-run `translation`, then continue through `subdomain`, `doc_type`, `truthness`.
- On `rejected`: proceed with the LLM's best-effort translation or skip the file (configurable; default to LLM best-effort).

**New subdomain / doc_type**
- The file stops at the classification stage.
- On `accepted`: append the new value to the relevant instruction file, re-run the classification stage, then continue downstream.
- On `rejected`: prompt the LLM again with the corrected allowed list, or fall back to `other`.

**Truthness low score / parse failure**
- The file has completed translation and classification but needs a trustworthiness override.
- On `accepted`: use `human_answer` as the final score/justification and write the final output file.
- On `rejected`: keep the LLM output and write the final output file.

### Re-running downstream stages on resolution

`scripts/review.py apply` uses the existing `delete_downstream_cache` helper to invalidate cached stages starting from the affected stage, then calls `process_file` with the resolved input.

## CLI Commands

```bash
# List pending review items
python scripts/review.py list

# Apply decisions from edited review/ files and re-run affected stages
python scripts/review.py apply

# Remove files for resolved items
python scripts/review.py clean
```

`sync` is intentionally absent; review files are created automatically by `process.py`.

### `list`

Shows pending items with file path, stage, trigger, and proposed answer. Useful for quick triage in the terminal.

### `apply`

1. Scans `review/` for files whose frontmatter `status` is `accepted` or `rejected`.
2. For each accepted item:
   - Updates global files (`glossary.md`, `instructions/subdomains.md`, `instructions/document_types.md`) if needed.
   - Stores the human answer in `review_queue.human_answer`.
   - Sets `status` to `accepted` and `resolved_at` to now.
3. For each rejected item:
   - Sets `status` to `rejected` and records the resolution note.
4. Re-runs downstream stages for each affected file.
5. Updates `files.status` to `processed` or `error` as appropriate.

### `clean`

Removes `review/` files whose DB row is no longer `pending`. This is safe to run anytime and prevents the directory from growing indefinitely.

## Reviewer Workflow

1. Run the batch pipeline:
   ```bash
   python process.py
   ```
   Summary prints: `42 processed, 7 need review`.

2. Open generated `review/` files in VSCode.

3. Edit frontmatter:
   - Fill in `human_answer`.
   - Change `status` to `accepted` or `rejected`.
   - Optionally add a `resolution_note`.

4. Apply decisions:
   ```bash
   python scripts/review.py apply
   ```

5. Optionally clean resolved files:
   ```bash
   python scripts/review.py clean
   ```

## Edge Cases & Guardrails

- **Pipeline never overwrites a human-edited file.** If a review file already exists for a given file/stage/trigger, `process.py` skips writing it and logs a warning. This protects in-progress reviews if the raw file is re-processed.
- **Duplicate triggers.** The `UNIQUE(file_hash, stage, trigger_type)` constraint plus the "write if not exists" rule prevent duplicate files.
- **Global file changes.** Adding a term to `glossary.md` or a category to an instruction file changes the instruction hash, which naturally invalidates cache for affected LLM stages on future pipeline runs. Existing already-processed files are not retroactively re-processed unless the user forces it.
- **Rejected items.** Default behavior is to fall back to the LLM's proposed answer and continue. If no proposed answer exists (e.g. clarification), the file is marked `error`.
- **Concurrent reviewers.** This design does not lock review files. In a multi-reviewer setup, reviewers should coordinate or use separate file sets.

## Future Extensions

- Web reviewer: a small FastAPI app that reads/writes the same `review_queue` table and `review/` files.
- Priority queue: add a `priority` column and sort `list` output.
- Batch accept/reject: allow `apply` to accept all items matching a filter.
- Review assignments: add `assigned_to` column for teams.

## Success Criteria

1. `python process.py` completes a batch of 6k documents without blocking for human input.
2. Ambiguous cases create review files that are readable and editable in VSCode.
3. `python scripts/review.py apply` correctly updates global files, persists decisions, and produces final outputs.
4. Re-running the pipeline after resolution does not recreate the same resolved review item.
