import argparse
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path

# Verify dependencies are present immediately at start
from docling.document_converter import DocumentConverter
from openai import OpenAI

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
def init_db(db_path: Path):
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    
    # files table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS files (
        filepath TEXT PRIMARY KEY,
        file_hash TEXT NOT NULL,
        status TEXT NOT NULL,          -- 'pending', 'processed', 'filtered', 'error'
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
    
    conn.commit()
    conn.close()

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
            import xml.etree.ElementTree as ET
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

def needs_translation(text: str) -> bool:
    hebrew_chars = len(re.findall(r"[\u0590-\u05FF]", text))
    total_letters = len(re.findall(r"[a-zA-Z\u0590-\u05FF]", text))
    if total_letters == 0:
        return False
    return (hebrew_chars / total_letters) >= 0.01

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
                
            instr_ok = (cache["instructions_hash"] == current_instr_hash)
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
                translated_text = call_llm(config, translation_instructions, text_content)
                text_content = translated_text
                upsert_stage_output(db_path, file_hash, "translation", translated_text, current_model, current_instr_hash)
                
        elif stage == "subdomain":
            # Consume translation output
            trans_text = get_cached_stages(db_path, file_hash)["translation"]["output_text"]
            subdomain_instructions = instr_path.read_text(encoding="utf-8")
            subdomain_val = call_llm(config, subdomain_instructions, trans_text)
            upsert_stage_output(db_path, file_hash, "subdomain", subdomain_val, current_model, current_instr_hash)
            
        elif stage == "doc_type":
            trans_text = get_cached_stages(db_path, file_hash)["translation"]["output_text"]
            doctype_instructions = instr_path.read_text(encoding="utf-8")
            doctype_val = call_llm(config, doctype_instructions, trans_text)
            upsert_stage_output(db_path, file_hash, "doc_type", doctype_val, current_model, current_instr_hash)
            
        elif stage == "truthness":
            trans_text = get_cached_stages(db_path, file_hash)["translation"]["output_text"]
            truthness_instructions = instr_path.read_text(encoding="utf-8")
            truthness_val = call_llm(config, truthness_instructions, trans_text)
            upsert_stage_output(db_path, file_hash, "truthness", truthness_val, current_model, current_instr_hash)
            
    # Write final output MD file (re-load all processed state to write frontmatter)
    final_cache = get_cached_stages(db_path, file_hash)
    trans_output = final_cache["translation"]["output_text"]
    subdomain = final_cache["subdomain"]["output_text"]
    doctype = final_cache["doc_type"]["output_text"]
    truthness_raw = final_cache["truthness"]["output_text"]
    
    score = 0
    justification = ""
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
    
    for fpath in files_to_process:
        try:
            # We hash inside process_file
            # In case of explicit stage re-run request without --force, we only re-run if invalid.
            # But if a stage is explicitly specified AND --force is active, it invalidates.
            process_file(fpath, raw_root, output_root, db_path, config, force_stage)
            success_count += 1
        except Exception as e:
            print(f"    [Error] Failed to process {fpath}: {e}", file=sys.stderr)
            import traceback
            traceback.print_exc()
            error_count += 1
            # Try to save file status as error in DB
            try:
                file_hash = compute_file_hash(fpath)
                upsert_file_status(db_path, str(fpath), file_hash, "error", str(e))
            except Exception:
                pass
                
    print(f"\nPipeline finished. Success: {success_count}, Errors/Failures: {error_count}")
    if error_count > 0:
        sys.exit(1)
    sys.exit(0)

if __name__ == "__main__":
    main()
