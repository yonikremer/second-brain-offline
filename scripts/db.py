import sqlite3
import json
from pathlib import Path
import yaml
from constants import STAGES

class NeedsReviewException(Exception):
    pass

def init_db(db_path: Path):
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    
    # files table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS files (
        filepath TEXT PRIMARY KEY,
        file_hash TEXT NOT NULL,
        status TEXT NOT NULL,          -- 'pending', 'processed', 'filtered', 'error', 'needs_review'
        error_message TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)
    
    # stage_outputs table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS stage_outputs (
        file_hash TEXT NOT NULL,
        stage_name TEXT NOT NULL,
        output_text TEXT,
        model_name TEXT,
        instructions_hash TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (file_hash, stage_name)
    );
    """)
    
    # review_queue table
    cursor.execute("""
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
    """)
    
    conn.commit()
    conn.close()

def trigger_review(db_path: Path, filepath: Path, file_hash: str, stage: str, trigger_type: str, context_json: str, proposed_answer: str, conn: sqlite3.Connection = None):
    should_close = conn is None
    if conn is None:
        conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()
        
        cursor.execute("""
        INSERT INTO review_queue (file_hash, filepath, stage, trigger_type, context_json, proposed_answer, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 'pending', CURRENT_TIMESTAMP)
        ON CONFLICT(file_hash, stage, trigger_type) DO UPDATE SET
            filepath = excluded.filepath,
            context_json = excluded.context_json,
            proposed_answer = excluded.proposed_answer
        """, (file_hash, str(filepath), stage, trigger_type, context_json, proposed_answer))
        
        cursor.execute(
            "SELECT id FROM review_queue WHERE file_hash = ? AND stage = ? AND trigger_type = ?",
            (file_hash, stage, trigger_type)
        )
        row = cursor.fetchone()
        queue_id = row[0] if row else None
        
        conn.commit()
    finally:
        if should_close:
            conn.close()
            
    short_hash = file_hash[:8]
    review_dir = Path("review")
    review_dir.mkdir(exist_ok=True)
    review_filename = f"{filepath.name}--{short_hash}--{stage}--{trigger_type}.md"
    review_path = review_dir / review_filename
    
    try:
        display_path = filepath.relative_to(Path.cwd()).as_posix()
    except ValueError:
        display_path = filepath.name
        
    if review_path.exists():
        print(f"    [Warning] Review file {review_path} already exists. Skipping write.")
    else:
        body = ""
        if trigger_type == "clarification":
            context_data = json.loads(context_json)
            term = context_data.get("term", "")
            context_sentence = context_data.get("context_sentence", "")
            body = f'## Term/Issue\n"{term}"\n\n## Context\n> {context_sentence}\n\n## Proposed answer\n*(none — LLM could not infer this term)*'
        elif trigger_type == "new_category":
            context_data = json.loads(context_json)
            proposed_val = context_data.get("proposed_value", "")
            existing_vals = context_data.get("existing_values", [])
            existing_list = "\n".join(f"- {v}" for v in existing_vals)
            body = f'## Proposed Category\n"{proposed_val}"\n\n## Existing Categories\n{existing_list}'
        elif trigger_type == "parse_failure":
            context_data = json.loads(context_json)
            raw_resp = context_data.get("raw_response", "")
            body = f'## Raw LLM Response\n```json\n{raw_resp}\n```\n\n## Expected answer format\nProvide your expert assessment as JSON:\n```json\n{{"score": 9, "justification": "Your reasoning here"}}\n```\nOr as plain text: `score: 9, justification: Your reasoning here`\n\nIf you accept without specifying a score, it will default to **10** (highest trust).'
        elif trigger_type == "low_score":
            context_data = json.loads(context_json)
            parsed_score = context_data.get("parsed_score", "")
            parsed_just = context_data.get("parsed_justification", "")
            body = f'## Score\n{parsed_score}\n\n## Justification\n{parsed_just}\n\n## Expected answer format\nProvide your expert assessment as JSON:\n```json\n{{"score": 9, "justification": "Your reasoning here"}}\n```\nOr as plain text: `score: 9, justification: Your reasoning here`\n\nIf you accept without specifying a score, it will default to **10** (highest trust).'

        title_map = {
            "clarification": "Translation Clarification",
            "new_category": "New Category Proposed",
            "parse_failure": "Truthness Parse Failure",
            "low_score": "Truthness Low Score"
        }
        title = title_map.get(trigger_type, "Review Needed")

        frontmatter = {
            "queue_id": queue_id,
            "file_hash": file_hash,
            "filepath": display_path,
            "stage": stage,
            "trigger": trigger_type,
            "status": "pending",
            "proposed_answer": proposed_answer or "",
            "human_answer": "",
            "resolution_note": "",
        }
        fm_text = yaml.safe_dump(
            frontmatter,
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
        )
        content = f"""---
{fm_text}---

# Review Needed: {title}

**File:** `{display_path}`  
**Stage:** {stage}  
**Trigger:** {trigger_type}

{body}

## Your answer
Edit `human_answer` in the frontmatter, then change `status` to `accepted` or `rejected`.
"""
        review_path.write_text(content, encoding="utf-8")
        print(f"    [Review] Created review file: {review_path}")

def get_cached_stages(db_path: Path, file_hash: str, conn: sqlite3.Connection = None) -> dict:
    should_close = conn is None
    if conn is None:
        conn = sqlite3.connect(str(db_path))
    original_row_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT stage_name, output_text, model_name, instructions_hash FROM stage_outputs WHERE file_hash = ?",
            (file_hash,)
        )
        rows = cursor.fetchall()
        return {row["stage_name"]: dict(row) for row in rows}
    finally:
        conn.row_factory = original_row_factory
        if should_close:
            conn.close()

def upsert_file_status(db_path: Path, filepath: str, file_hash: str, status: str, error_message: str = None, conn: sqlite3.Connection = None):
    should_close = conn is None
    if conn is None:
        conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()
        cursor.execute("""
        INSERT INTO files (filepath, file_hash, status, error_message, updated_at)
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(filepath) DO UPDATE SET
            file_hash = excluded.file_hash,
            status = excluded.status,
            error_message = excluded.error_message,
            updated_at = CURRENT_TIMESTAMP
        """, (filepath, file_hash, status, error_message))
        conn.commit()
    finally:
        if should_close:
            conn.close()

def upsert_stage_output(db_path: Path, file_hash: str, stage_name: str, output_text: str, model_name: str = None, instructions_hash: str = None, conn: sqlite3.Connection = None):
    should_close = conn is None
    if conn is None:
        conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()
        cursor.execute("""
        INSERT INTO stage_outputs (file_hash, stage_name, output_text, model_name, instructions_hash, updated_at)
        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(file_hash, stage_name) DO UPDATE SET
            output_text = excluded.output_text,
            model_name = excluded.model_name,
            instructions_hash = excluded.instructions_hash,
            updated_at = CURRENT_TIMESTAMP
        """, (file_hash, stage_name, output_text, model_name, instructions_hash))
        conn.commit()
    finally:
        if should_close:
            conn.close()

def delete_downstream_cache(db_path: Path, file_hash: str, from_stage: str, conn: sqlite3.Connection = None):
    idx = STAGES.index(from_stage)
    downstream_stages = STAGES[idx:]
    should_close = conn is None
    if conn is None:
        conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()
        placeholders = ",".join("?" for _ in downstream_stages)
        cursor.execute(
            f"DELETE FROM stage_outputs WHERE file_hash = ? AND stage_name IN ({placeholders})",
            [file_hash] + downstream_stages
        )
        conn.commit()
    finally:
        if should_close:
            conn.close()
