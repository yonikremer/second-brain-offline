import argparse
import hashlib
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

# Verify dependencies are present immediately at start
from docling.document_converter import DocumentConverter
from openai import OpenAI
import xml.etree.ElementTree as ET
import yaml

# Pipeline sequence
STAGES = ["docling", "filtering", "translation", "subdomain", "doc_type", "truthness"]

# Paths to stage instruction files (relative to project root)
STAGE_INSTRUCTIONS = {
    "docling": None,
    "filtering": None,
    "translation": Path("instructions/translation.md"),
    "subdomain": Path("instructions/subdomains.md"),
    "doc_type": Path("instructions/document_types.md"),
    "truthness": Path("instructions/truthness.md"),
}

def compute_file_hash(filepath: Path) -> str:
    hasher = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(8192):
            hasher.update(chunk)
    return hasher.hexdigest()

def get_instruction_hash(instruction_path: Path) -> str:
    if not instruction_path.exists():
        return ""
    hasher = hashlib.sha256()
    with open(instruction_path, "rb") as f:
        hasher.update(f.read())
    return hasher.hexdigest()

# SQLite Database Helper Functions
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

def trigger_review(db_path: Path, filepath: Path, file_hash: str, stage: str, trigger_type: str, context_json: str, proposed_answer: str):
    conn = sqlite3.connect(str(db_path))
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

def parse_truthness_human_answer(human_answer: str, default_score: int = 0, default_justification: str = "") -> tuple[int, str]:
    if not human_answer:
        return default_score, default_justification
    human_answer = human_answer.strip()
    try:
        data = json.loads(human_answer)
        if isinstance(data, dict):
            return int(data.get("score", default_score)), data.get("justification", default_justification)
    except json.JSONDecodeError:
        pass
    
    m = re.match(r"score:\s*(\d+)(?:,\s*justification:\s*(.*))?", human_answer, re.IGNORECASE)
    if m:
        score = int(m.group(1))
        justification = m.group(2).strip() if m.group(2) else default_justification
        return score, justification
        
    m_int = re.search(r"\b(\d+)\b", human_answer)
    if m_int:
        score = int(m_int.group(1))
        justification = human_answer.replace(m_int.group(0), "", 1).strip(", -:").strip()
        if not justification:
            justification = default_justification
        return score, justification
        
    return default_score, human_answer

def get_cached_stages(db_path: Path, file_hash: str) -> dict:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        "SELECT stage_name, output_text, model_name, instructions_hash FROM stage_outputs WHERE file_hash = ?",
        (file_hash,)
    )
    rows = cursor.fetchall()
    conn.close()
    return {row["stage_name"]: dict(row) for row in rows}

def upsert_file_status(db_path: Path, filepath: str, file_hash: str, status: str, error_message: str = None):
    conn = sqlite3.connect(str(db_path))
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
    conn.close()

def upsert_stage_output(db_path: Path, file_hash: str, stage_name: str, output_text: str, model_name: str = None, instructions_hash: str = None):
    conn = sqlite3.connect(str(db_path))
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
    conn.close()

def delete_downstream_cache(db_path: Path, file_hash: str, from_stage: str):
    idx = STAGES.index(from_stage)
    downstream_stages = STAGES[idx:]
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    placeholders = ",".join("?" for _ in downstream_stages)
    cursor.execute(
        f"DELETE FROM stage_outputs WHERE file_hash = ? AND stage_name IN ({placeholders})",
        [file_hash] + downstream_stages
    )
    conn.commit()
    conn.close()

# Stage Implementation Logic
def check_guid_filename_ratio(text: str) -> bool:
    # Check if content is JSON or XML (either raw or wrapped in markdown code blocks)
    def is_valid_json(s: str) -> bool:
        try:
            json.loads(s)
            return True
        except ValueError:
            return False

    def is_valid_xml(s: str) -> bool:
        try:
            ET.fromstring(s)
            return True
        except ET.ParseError:
            return False

    cleaned = text.strip()
    if cleaned:
        if is_valid_json(cleaned) or is_valid_xml(cleaned):
            return True
        # Try stripping markdown code fences
        m = re.match(r"^```(?:json|xml)?\s+(.*?)\s+```$", cleaned, re.DOTALL | re.IGNORECASE)
        if m:
            stripped = m.group(1).strip()
            if is_valid_json(stripped) or is_valid_xml(stripped):
                return True

    # Check if content is mostly hex or bits (ignore spaces and newlines)
    no_ws = "".join(text.split())
    if no_ws:
        hex_count = len(re.findall(r"[0-9a-fA-F]", no_ws))
        if (hex_count / len(no_ws)) >= 0.80:
            return True

    guid_pattern = re.compile(r"\b[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}\b")
    filename_pattern = re.compile(r"\b[\w\-]+\.(?:pdf|docx|doc|txt|md|html|png|jpg|jpeg|zip|json|yml|yaml|csv|xml|xls|xlsx|wav|32fc|16c|32f|one)\b")
    
    # Find spans to calculate union (avoid double-counting overlapping regions)
    spans = []
    for pattern in (guid_pattern, filename_pattern):
        for m in pattern.finditer(text):
            spans.append((m.start(), m.end()))
            
    if not spans:
        return False
        
    spans.sort(key=lambda x: x[0])
    merged_spans = []
    for current in spans:
        if not merged_spans:
            merged_spans.append(current)
        else:
            prev_start, prev_end = merged_spans[-1]
            curr_start, curr_end = current
            if curr_start <= prev_end:
                merged_spans[-1] = (prev_start, max(prev_end, curr_end))
            else:
                merged_spans.append(current)
                
    matched_non_ws = 0
    for start, end in merged_spans:
        matched_non_ws += len("".join(text[start:end].split()))
        
    total_non_ws = len("".join(text.split()))
    if total_non_ws == 0:
        return False
        
    ratio = matched_non_ws / total_non_ws
    return ratio >= 0.80

def fix_hebrew_layout(text: str, status: str) -> str:
    if status == "NORMAL":
        return text
        
    fixed_lines = []
    for line in text.split('\n'):
        words = line.split()
        if not words:
            fixed_lines.append("")
            continue
            
        # Reverse characters of Hebrew words
        if status in ("REVERSED_WORDS", "REVERSED_BOTH"):
            words = [
                w[::-1] if any('\u0590' <= c <= '\u05FF' for c in w) else w 
                for w in words
            ]
        # Reverse word order of the sentence
        if status in ("REVERSED_SENTENCES", "REVERSED_BOTH"):
            words = words[::-1]
            
        fixed_lines.append(" ".join(words))
    return "\n".join(fixed_lines)

def needs_translation(text: str) -> bool:
    hebrew_chars = len(re.findall(r"[\u0590-\u05FF]", text))
    total_letters = len(re.findall(r"[a-zA-Z\u0590-\u05FF]", text))
    if total_letters == 0:
        return False
    return (hebrew_chars / total_letters) >= 0.01

def parse_allowed_values(instruction_path: Path) -> list[str]:
    if not instruction_path.exists():
        return []
    content = instruction_path.read_text(encoding="utf-8")
    lines = content.splitlines()
    allowed_values = []
    in_allowed_section = False
    for line in lines:
        if line.strip().startswith("## Allowed"):
            in_allowed_section = True
            continue
        if in_allowed_section and line.strip().startswith("## "):
            break
        if in_allowed_section:
            m = re.search(r"\*\*(.*?)\*\*", line)
            if m:
                allowed_values.append(m.group(1).strip())
    return allowed_values

def append_category_to_file(instr_path: Path, val: str, focus: str):
    content = instr_path.read_text(encoding="utf-8")
    lines = content.splitlines()
    
    allowed_start_idx = -1
    allowed_end_idx = -1
    for idx, line in enumerate(lines):
        if line.strip().startswith("## Allowed"):
            allowed_start_idx = idx
        elif allowed_start_idx != -1 and line.strip().startswith("## ") and allowed_end_idx == -1:
            allowed_end_idx = idx
            break
            
    if allowed_end_idx == -1:
        allowed_end_idx = len(lines)
        
    allowed_lines = lines[allowed_start_idx:allowed_end_idx]
    
    item_regex = re.compile(r"^(\d+)\.\s+\*\*(.*?)\*\*")
    items = []
    for rel_idx, line in enumerate(allowed_lines):
        m = item_regex.match(line.strip())
        if m:
            items.append((rel_idx, int(m.group(1)), m.group(2).strip()))
            
    if not items:
        new_item_lines = [
            f"1. **{val}**",
            f"   - Focus: {focus}" if focus else f"   - Focus: User-defined focus description."
        ]
        lines[allowed_end_idx:allowed_end_idx] = [""] + new_item_lines
    else:
        last_item_rel_idx, last_num, last_name = items[-1]
        
        insert_rel_idx = -1
        new_num = -1
        
        if last_name.lower() == "other":
            insert_rel_idx = last_item_rel_idx
            new_num = last_num
        else:
            curr = last_item_rel_idx + 1
            while curr < len(allowed_lines) and allowed_lines[curr].strip():
                curr += 1
            insert_rel_idx = curr
            new_num = last_num + 1
            
        new_item_lines = [
            f"{new_num}. **{val}**",
            f"   - Focus: {focus}" if focus else f"   - Focus: User-defined focus description."
        ]
        
        new_allowed_lines = list(allowed_lines[:insert_rel_idx])
        if new_allowed_lines and new_allowed_lines[-1].strip():
            new_allowed_lines.append("")
        new_allowed_lines.extend(new_item_lines)
        new_allowed_lines.append("")
        
        remaining_lines = allowed_lines[insert_rel_idx:]
        for rel_line_idx in range(len(remaining_lines)):
            line = remaining_lines[rel_line_idx]
            m = item_regex.match(line.strip())
            if m:
                old_num = int(m.group(1))
                line = line.replace(f"{old_num}.", f"{old_num + 1}.", 1)
            new_allowed_lines.append(line)
            
        cleaned_allowed_lines = []
        last_was_blank = False
        for line in new_allowed_lines:
            is_blank = not line.strip()
            if is_blank and last_was_blank:
                continue
            cleaned_allowed_lines.append(line)
            last_was_blank = is_blank
            
        lines[allowed_start_idx:allowed_end_idx] = cleaned_allowed_lines
        
    instr_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

def call_llm(config: dict, system_prompt: str, user_prompt: str) -> str:
    api_base = config["llm"]["api_base"]
    api_key = config["llm"]["api_key"]
    model = config["llm"]["model"]
    
    client = OpenAI(base_url=api_base, api_key=api_key)
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.0
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Error connecting to local LLM at {api_base}: {e}", file=sys.stderr)
        print("Please check if Ollama or your LLM server is running.", file=sys.stderr)
        raise

def parse_json_response(response_text: str) -> dict:
    cleaned = response_text.strip()
    if cleaned.startswith("```"):
        nl_idx = cleaned.find("\n")
        if nl_idx != -1:
            cleaned = cleaned[nl_idx:].strip()
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        raise ValueError(f"Response not valid JSON: {response_text}") from e

def process_file(
    filepath: Path,
    raw_root: Path,
    output_root: Path,
    db_path: Path,
    config: dict,
    force_stage: str | None
):
    rel_path = filepath.relative_to(raw_root) if filepath.is_relative_to(raw_root) else Path(filepath.name)
    print(f"\n>>> Processing: {rel_path}")
    
    file_hash = compute_file_hash(filepath)
    
    # Check if there is any pending review item for this file_hash
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    cursor.execute(
        "SELECT stage, trigger_type FROM review_queue WHERE file_hash = ? AND status = 'pending'",
        (file_hash,)
    )
    pending_items = cursor.fetchall()
    conn.close()
    
    if pending_items:
        print(f"    [Pending Review] File is blocked by pending review for stage '{pending_items[0][0]}' (trigger: '{pending_items[0][1]}').")
        upsert_file_status(db_path, str(filepath), file_hash, "needs_review")
        raise NeedsReviewException(f"Blocked by pending review for stage '{pending_items[0][0]}'")
    
    # Invalidate file-level cache if file hash changed
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    cursor.execute("SELECT file_hash FROM files WHERE filepath = ?", (str(filepath),))
    row = cursor.fetchone()
    conn.close()
    
    if row and row[0] != file_hash:
        print("    [Info] Raw file content changed. Invalidating all cached stages.")
        delete_downstream_cache(db_path, file_hash, "docling")
        upsert_file_status(db_path, str(filepath), file_hash, "pending")
        
    cached_stages = get_cached_stages(db_path, file_hash)
    current_model = config["llm"]["model"]
    
    # Determine which stages to run (Cascading Invalidation)
    stages_to_run = []
    invalidated_upstream = False
    
    for stage in STAGES:
        is_forced = (force_stage is not None and STAGES.index(stage) >= STAGES.index(force_stage))
        cache = cached_stages.get(stage)
        
        instr_path = STAGE_INSTRUCTIONS[stage]
        current_instr_hash = get_instruction_hash(instr_path) if instr_path else ""
        
        cache_valid = False
        if cache and not is_forced and not invalidated_upstream:
            model_ok = True
            if stage in ["translation", "subdomain", "doc_type", "truthness"]:
                model_ok = (cache["model_name"] == current_model)
                
            instr_ok = (cache["instructions_hash"] or "") == (current_instr_hash or "")
            if model_ok and instr_ok:
                cache_valid = True
                
        if not cache_valid:
            stages_to_run.append(stage)
            invalidated_upstream = True
            
    if not stages_to_run:
        print("    [Cache] All stages up-to-date. Skipping.")
        return
        
    print(f"    [Stages to run] {', '.join(stages_to_run)}")
    
    # Clear cache for the run stages and downstream stages to enforce cascade consistency
    if stages_to_run:
        delete_downstream_cache(db_path, file_hash, stages_to_run[0])
        
    # Execute stages
    text_content = cached_stages.get("docling", {}).get("output_text", "")
    
    for stage in stages_to_run:
        print(f"    -> Running stage: {stage}...")
        instr_path = STAGE_INSTRUCTIONS[stage]
        current_instr_hash = get_instruction_hash(instr_path) if instr_path else ""
        
        if stage == "docling":
            if filepath.suffix.lower() == ".one":
                print("    [OneNote] Converting .one file to temporary .docx via PowerShell COM...")
                temp_docx = filepath.with_suffix(".temp.docx")
                try:
                    cmd = [
                        "powershell.exe", "-ExecutionPolicy", "Bypass", "-File",
                        str(Path("scripts/convert_onenote.ps1").resolve()),
                        "-OnePath", str(filepath.resolve()),
                        "-DocxPath", str(temp_docx.resolve())
                    ]
                    res = subprocess.run(cmd, capture_output=True, text=True)
                    if res.returncode != 0:
                        raise RuntimeError(
                            f"OneNote COM conversion failed (exit code {res.returncode}).\n"
                            f"STDOUT: {res.stdout}\nSTDERR: {res.stderr}\n"
                            "Tip: Make sure OneNote is open and running in the same privilege context as this script."
                        )
                    
                    converter = DocumentConverter()
                    result = converter.convert(str(temp_docx))
                    text_content = result.document.export_to_markdown()
                finally:
                    if temp_docx.exists():
                        temp_docx.unlink()
            else:
                converter = DocumentConverter()
                result = converter.convert(str(filepath))
                text_content = result.document.export_to_markdown()
            upsert_stage_output(db_path, file_hash, "docling", text_content, None, None)
            
        elif stage == "filtering":
            is_filtered = check_guid_filename_ratio(text_content)
            upsert_stage_output(db_path, file_hash, "filtering", "true" if is_filtered else "false", None, None)
            if is_filtered:
                print("    [Filter] File contains >=80% GUIDs or filenames. Stopping.")
                upsert_file_status(db_path, str(filepath), file_hash, "filtered")
                # Remove output MD file if it exists from previous runs
                out_path = output_root / rel_path.with_suffix(".md")
                if out_path.exists():
                    out_path.unlink()
                return
                
        elif stage == "translation":
            if not needs_translation(text_content):
                print("    [Language] Entirely English. Skipping translation.")
                upsert_stage_output(db_path, file_hash, "translation", text_content, current_model, current_instr_hash)
            else:
                translation_instructions = instr_path.read_text(encoding="utf-8")
                glossary_path = Path("glossary.md")
                if glossary_path.exists():
                    translation_instructions += f"\n\n# Active Glossary (glossary.md)\n{glossary_path.read_text(encoding='utf-8')}"
                
                # Check for rejected review to apply best-effort
                conn = sqlite3.connect(str(db_path))
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT status FROM review_queue WHERE file_hash = ? AND stage = 'translation' AND trigger_type = 'clarification'",
                    (file_hash,)
                )
                trans_review = cursor.fetchone()
                conn.close()
                
                if trans_review and trans_review[0] == 'rejected':
                    translation_instructions += "\n\nIMPORTANT: You must proceed with best-effort translation. Do NOT trigger clarification or output 'Clarification Required'. If there are unknown terms, translate them to the best of your ability."
                
                current_text_to_translate = text_content
                while True:
                    response_text = call_llm(config, translation_instructions, current_text_to_translate)
                    
                    status_line = ""
                    payload = response_text
                    if response_text.startswith("RTL_STATUS:"):
                        lines = response_text.split('\n', 1)
                        status_line = lines[0].strip()
                        payload = lines[1].strip() if len(lines) > 1 else ""
                    
                    if any(x in status_line for x in ("REVERSED_WORDS", "REVERSED_SENTENCES", "REVERSED_BOTH")):
                        detected_status = "REVERSED_BOTH" if "REVERSED_BOTH" in status_line else ("REVERSED_WORDS" if "REVERSED_WORDS" in status_line else "REVERSED_SENTENCES")
                        print(f"    [RTL Status] Detected corruption: {detected_status}. Fixing locally and retrying...")
                        current_text_to_translate = fix_hebrew_layout(current_text_to_translate, detected_status)
                        continue
                    
                    if "NORMAL" in status_line:
                        translated_text = payload
                    else:
                        translated_text = response_text
                        
                    if "Clarification Required" in translated_text:
                        term_match = re.search(r"Term/Issue:\s*(.*)", translated_text, re.IGNORECASE)
                        if not term_match:
                            term_match = re.search(r"Term:\s*(.*)", translated_text, re.IGNORECASE)
                        term_to_clarify = term_match.group(1).strip() if term_match else "unknown term"
                        
                        context_sentence = ""
                        context_match = re.search(r"Context:\s*\"?(.*?)\"?(?:\n|$)", translated_text, re.IGNORECASE)
                        if context_match:
                            context_sentence = context_match.group(1).strip()
                            
                        if trans_review and trans_review[0] == 'rejected':
                            print("    [Warning] Translation triggered clarification despite rejected review. Proceeding best-effort.")
                            text_content = translated_text
                            upsert_stage_output(db_path, file_hash, "translation", translated_text, current_model, current_instr_hash)
                            break
                            
                        context_json = json.dumps({
                            "term": term_to_clarify,
                            "context_sentence": context_sentence
                        })
                        trigger_review(db_path, filepath, file_hash, "translation", "clarification", context_json, "")
                        upsert_file_status(db_path, str(filepath), file_hash, "needs_review")
                        raise NeedsReviewException("Translation clarification required.")
                    else:
                        text_content = translated_text
                        upsert_stage_output(db_path, file_hash, "translation", translated_text, current_model, current_instr_hash)
                        break
                
        elif stage in ("subdomain", "doc_type"):
            trans_text = get_cached_stages(db_path, file_hash)["translation"]["output_text"]
            instructions = instr_path.read_text(encoding="utf-8")
            
            conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()
            cursor.execute(
                "SELECT status, proposed_answer FROM review_queue WHERE file_hash = ? AND stage = ? AND trigger_type = 'new_category'",
                (file_hash, stage)
            )
            cat_review = cursor.fetchone()
            conn.close()
            
            rejected_category = None
            if cat_review and cat_review[0] == 'rejected':
                rejected_category = cat_review[1]
                instructions += f"\n\nNote: The category '{rejected_category}' is NOT allowed. Please classify into one of the other allowed categories."
            
            val = call_llm(config, instructions, trans_text)
            val_clean = val.strip()
            
            allowed_values = parse_allowed_values(instr_path)
            matched = None
            for allowed in allowed_values:
                if val_clean.lower() == allowed.lower():
                    matched = allowed
                    break
                    
            if matched:
                upsert_stage_output(db_path, file_hash, stage, matched, current_model, current_instr_hash)
            else:
                if rejected_category and val_clean.lower() == rejected_category.lower():
                    fallback = "other" if "other" in [a.lower() for a in allowed_values] else (allowed_values[0] if allowed_values else "other")
                    for allowed in allowed_values:
                        if allowed.lower() == fallback.lower():
                            fallback = allowed
                            break
                    print(f"    [Warning] LLM proposed rejected category '{val_clean}'. Falling back to '{fallback}'.")
                    upsert_stage_output(db_path, file_hash, stage, fallback, current_model, current_instr_hash)
                else:
                    context_json = json.dumps({
                        "proposed_value": val_clean,
                        "existing_values": allowed_values,
                        "focus_hint": "Added automatically in non-interactive run."
                    })
                    trigger_review(db_path, filepath, file_hash, stage, "new_category", context_json, val_clean)
                    upsert_file_status(db_path, str(filepath), file_hash, "needs_review")
                    raise NeedsReviewException(f"New {stage} category '{val_clean}' proposed.")
            
        elif stage == "truthness":
            trans_text = get_cached_stages(db_path, file_hash)["translation"]["output_text"]
            truthness_instructions = instr_path.read_text(encoding="utf-8")
            
            # Check if there is already a resolved review for truthness to avoid calling LLM
            conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()
            cursor.execute(
                "SELECT status, proposed_answer FROM review_queue WHERE file_hash = ? AND stage = 'truthness'",
                (file_hash,)
            )
            truth_review_row = cursor.fetchone()
            conn.close()
            
            if truth_review_row and truth_review_row[0] in ('accepted', 'rejected'):
                truthness_val = truth_review_row[1]
                print(f"    [Truthness Review] Already resolved: {truth_review_row[0]}. Using cached proposed answer.")
            else:
                truthness_val = call_llm(config, truthness_instructions, trans_text)
            
            score = 0
            justification = ""
            is_parse_failure = False
            is_low_score = False
            
            try:
                truthness_data = parse_json_response(truthness_val)
                score = truthness_data.get("score", 0)
                justification = truthness_data.get("justification", "")
                
                threshold = config.get("truthness", {}).get("threshold", 4)
                if score < threshold:
                    is_low_score = True
            except Exception as e:
                is_parse_failure = True
                justification = truthness_val
                
            if is_parse_failure or is_low_score:
                trigger_type = "parse_failure" if is_parse_failure else "low_score"
                conn = sqlite3.connect(str(db_path))
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT status FROM review_queue WHERE file_hash = ? AND stage = 'truthness' AND trigger_type = ?",
                    (file_hash, trigger_type)
                )
                truth_review = cursor.fetchone()
                conn.close()
                
                if truth_review and truth_review[0] in ('accepted', 'rejected'):
                    print(f"    [Truthness Review] Already resolved: {truth_review[0]}. Proceeding.")
                    upsert_stage_output(db_path, file_hash, "truthness", truthness_val, current_model, current_instr_hash)
                else:
                    if is_parse_failure:
                        context_json = json.dumps({
                            "raw_response": truthness_val,
                            "parsed_score": None,
                            "parsed_justification": None
                        })
                        trigger_review(db_path, filepath, file_hash, "truthness", "parse_failure", context_json, truthness_val)
                        upsert_file_status(db_path, str(filepath), file_hash, "needs_review")
                        raise NeedsReviewException("Truthness parse failure.")
                    else:
                        context_json = json.dumps({
                            "raw_response": truthness_val,
                            "parsed_score": score,
                            "parsed_justification": justification
                        })
                        trigger_review(db_path, filepath, file_hash, "truthness", "low_score", context_json, json.dumps({"score": score, "justification": justification}))
                        upsert_file_status(db_path, str(filepath), file_hash, "needs_review")
                        raise NeedsReviewException(f"Truthness score {score} is below threshold.")
            else:
                upsert_stage_output(db_path, file_hash, "truthness", truthness_val, current_model, current_instr_hash)
            
    # Write final output MD file (re-load all processed state to write frontmatter)
    final_cache = get_cached_stages(db_path, file_hash)
    trans_output = final_cache["translation"]["output_text"]
    subdomain = final_cache["subdomain"]["output_text"]
    doctype = final_cache["doc_type"]["output_text"]
    truthness_raw = final_cache["truthness"]["output_text"]
    
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    cursor.execute(
        "SELECT status, human_answer FROM review_queue WHERE file_hash = ? AND stage = 'truthness'",
        (file_hash,)
    )
    truth_review = cursor.fetchone()
    conn.close()
    
    score = 0
    justification = ""
    
    if truth_review and truth_review[0] == 'accepted':
        human_ans = truth_review[1]
        orig_score = 0
        orig_just = truthness_raw
        try:
            truthness_data = parse_json_response(truthness_raw)
            orig_score = truthness_data.get("score", 0)
            orig_just = truthness_data.get("justification", "")
        except Exception:
            pass
        score, justification = parse_truthness_human_answer(human_ans, 10, orig_just)
        print(f"    [Truthness Override] Applying human review decision: score={score}")
    else:
        try:
            truthness_data = parse_json_response(truthness_raw)
            score = truthness_data.get("score", 0)
            justification = truthness_data.get("justification", "")
        except Exception as e:
            print(f"    [Warning] Failed to parse truthness JSON: {e}. Storing raw string instead.")
            justification = truthness_raw
        
    orig_text = final_cache["docling"]["output_text"]
    lang_status = "hebrew -> english" if needs_translation(orig_text) else "english (skipped translation)"
    
    frontmatter = f"""---
original_path: {rel_path.as_posix()}
file_hash: {file_hash}
subdomain: {subdomain}
document_type: {doctype}
truthness_score: {score}
truthness_justification: "{justification.replace('"', '\\"')}"
language: {lang_status}
model: {current_model}
---

"""
    out_path = output_root / rel_path.with_suffix(".md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(frontmatter + trans_output, encoding="utf-8")
    print(f"    [Output] Wrote file to {out_path}")
    
    upsert_file_status(db_path, str(filepath), file_hash, "processed")

def main():
    parser = argparse.ArgumentParser(description="Staged document processing pipeline.")
    parser.add_argument("--stage", choices=STAGES, help="Stage to selectively run or force re-run.")
    parser.add_argument("--force", action="store_true", help="Force re-run the stage and all downstream stages.")
    parser.add_argument("--files", nargs="+", help="Explicit file paths to process instead of all files in raw/.")
    args = parser.parse_args()
    
    root = Path.cwd()
    raw_root = root / "raw"
    output_root = root / "processed_md"
    db_path = root / "pipeline.db"
    config_path = root / "pipeline_config.json"
    
    if not config_path.exists():
        print(f"Error: Config file not found at {config_path}. Please create it first.", file=sys.stderr)
        sys.exit(1)
        
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
        
    init_db(db_path)
    
    # Collect files to process
    if args.files:
        files_to_process = [Path(f).resolve() for f in args.files]
        for f in files_to_process:
            if not f.exists():
                print(f"Error: Target file {f} does not exist.", file=sys.stderr)
                sys.exit(1)
    else:
        if not raw_root.exists():
            print(f"raw/ directory not found at {raw_root}. Creating it.", file=sys.stderr)
            raw_root.mkdir(parents=True, exist_ok=True)
        # Find all files in raw/ except hidden files
        files_to_process = []
        for p in raw_root.rglob("*"):
            if p.is_file() and not p.name.startswith("."):
                files_to_process.append(p)
                
    if not files_to_process:
        print("No files found to process.")
        sys.exit(0)
        
    force_stage = args.stage if args.force else None
    
    success_count = 0
    error_count = 0
    review_count = 0
    
    for fpath in files_to_process:
        try:
            process_file(fpath, raw_root, output_root, db_path, config, force_stage)
            success_count += 1
        except NeedsReviewException as e:
            print(f"    [Needs Review] {fpath.name}: {e}")
            review_count += 1
        except Exception as e:
            print(f"    [Error] Failed to process {fpath}: {e}", file=sys.stderr)
            import traceback
            traceback.print_exc()
            error_count += 1
            try:
                file_hash = compute_file_hash(fpath)
                upsert_file_status(db_path, str(fpath), file_hash, "error", str(e))
            except Exception:
                pass
                
    print(f"\nPipeline finished. Success: {success_count}, Errors/Failures: {error_count}, Needs Review: {review_count}")
    print(f"Summary: {success_count} processed, {review_count} need review")
    if error_count > 0:
        sys.exit(1)
    sys.exit(0)

if __name__ == "__main__":
    main()
