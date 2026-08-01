import argparse
import json
import re
import sqlite3
import subprocess
import sys
import os
import concurrent.futures
from pathlib import Path
import yaml
from docling.document_converter import DocumentConverter

# Import constants and sub-modules
from constants import STAGES, STAGE_INSTRUCTIONS
from db import (
    NeedsReviewException,
    init_db,
    trigger_review,
    get_cached_stages,
    upsert_file_status,
    upsert_stage_output,
    delete_downstream_cache
)
from helpers import (
    compute_file_hash,
    get_instruction_hash,
    check_guid_filename_ratio,
    fix_hebrew_layout,
    needs_translation,
    parse_allowed_values,
    append_category_to_file,
    load_glossary_entries,
    filter_glossary_entries,
    parse_truthness_human_answer
)
from llm_client import call_llm, parse_json_response
from stages import (
    run_docling_stage,
    run_filtering_stage,
    run_translation_stage,
    run_classification_stage,
    run_truthness_stage
)

# Export functions for backwards-compatibility with existing scripts/tests
__all__ = [
    "STAGES",
    "STAGE_INSTRUCTIONS",
    "NeedsReviewException",
    "init_db",
    "trigger_review",
    "get_cached_stages",
    "upsert_file_status",
    "upsert_stage_output",
    "delete_downstream_cache",
    "compute_file_hash",
    "get_instruction_hash",
    "check_guid_filename_ratio",
    "fix_hebrew_layout",
    "needs_translation",
    "parse_allowed_values",
    "append_category_to_file",
    "load_glossary_entries",
    "filter_glossary_entries",
    "parse_truthness_human_answer",
    "call_llm",
    "parse_json_response",
    "process_file",
    "main"
]

def handle_hash_change(db_path: Path, filepath: Path, file_hash: str, conn: sqlite3.Connection):
    cursor = conn.cursor()
    cursor.execute("SELECT file_hash FROM files WHERE filepath = ?", (str(filepath),))
    row = cursor.fetchone()
    
    if row and row[0] != file_hash:
        old_hash = row[0]
        print(f"    [Info] Raw file content changed. Invalidating all cached stages for old hash {old_hash[:8]}.")
        delete_downstream_cache(db_path, old_hash, "docling", conn=conn)
        
        # Mark all pending review-queue rows for the old hash as stale
        cursor.execute(
            "UPDATE review_queue SET status = 'stale' WHERE file_hash = ? AND status = 'pending'",
            (old_hash,)
        )
        conn.commit()
        
        # Delete any review/ files whose file_hash matches the old hash
        review_dir = Path("review")
        if review_dir.exists():
            for p in review_dir.glob("*.md"):
                try:
                    content = p.read_text(encoding="utf-8")
                    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
                    if m:
                        data = yaml.safe_load(m.group(1))
                        if data and data.get("file_hash") == old_hash:
                            p.unlink()
                            print(f"    [Info] Deleted stale review file: {p.name}")
                except Exception as e:
                    print(f"    [Warning] Failed to clean stale review file {p.name}: {e}")
                    
        upsert_file_status(db_path, str(filepath), file_hash, "pending", conn=conn)

def determine_stages_to_run(db_path: Path, file_hash: str, force_stage: str | None, config: dict, conn: sqlite3.Connection) -> list[str]:
    cached_stages = get_cached_stages(db_path, file_hash, conn=conn)
    current_model = config["llm"]["model"]
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
            
    return stages_to_run

def write_final_output_file(
    filepath: Path,
    db_path: Path,
    file_hash: str,
    output_root: Path,
    rel_path: Path,
    current_model: str,
    conn: sqlite3.Connection
):
    final_cache = get_cached_stages(db_path, file_hash, conn=conn)
    trans_output = final_cache["translation"]["output_text"]
    subdomain = final_cache["subdomain"]["output_text"]
    doctype = final_cache["doc_type"]["output_text"]
    truthness_raw = final_cache["truthness"]["output_text"]
    
    cursor = conn.cursor()
    cursor.execute(
        "SELECT status, human_answer FROM review_queue WHERE file_hash = ? AND stage = 'truthness'",
        (file_hash,)
    )
    truth_review = cursor.fetchone()
    
    score = 0
    justification = ""
    
    if truth_review and truth_review[0] == 'accepted':
        human_ans = truth_review[1]
        orig_just = truthness_raw
        try:
            truthness_data = parse_json_response(truthness_raw)
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
    
    fm_data = {
        "original_path": rel_path.as_posix(),
        "file_hash": file_hash,
        "subdomain": subdomain,
        "document_type": doctype,
        "truthness_score": score,
        "truthness_justification": justification,
        "language": lang_status,
        "model": current_model
    }
    fm_text = yaml.safe_dump(
        fm_data,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False
    )
    frontmatter = f"---\n{fm_text}---\n\n"
    
    out_path = output_root / rel_path.with_suffix(".md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(frontmatter + trans_output, encoding="utf-8")
    print(f"    [Output] Wrote file to {out_path}")
    
    upsert_file_status(db_path, str(filepath), file_hash, "processed", conn=conn)

def process_file(
    filepath: Path,
    raw_root: Path,
    output_root: Path,
    db_path: Path,
    config: dict,
    force_stage: str | None,
    conn: sqlite3.Connection = None
):
    should_close = conn is None
    if conn is None:
        conn = sqlite3.connect(str(db_path), timeout=30.0)
    try:
        rel_path = filepath.relative_to(raw_root) if filepath.is_relative_to(raw_root) else Path(filepath.name)
        print(f"\n>>> Processing: {rel_path}")
        
        file_hash = compute_file_hash(filepath)
        
        handle_hash_change(db_path, filepath, file_hash, conn)
        
        # Check if there is any pending review item for this file_hash
        cursor = conn.cursor()
        cursor.execute(
            "SELECT stage, trigger_type FROM review_queue WHERE file_hash = ? AND status = 'pending'",
            (file_hash,)
        )
        pending_items = cursor.fetchall()
        
        if pending_items:
            print(f"    [Pending Review] File is blocked by pending review for stage '{pending_items[0][0]}' (trigger: '{pending_items[0][1]}').")
            upsert_file_status(db_path, str(filepath), file_hash, "needs_review", conn=conn)
            raise NeedsReviewException(f"Blocked by pending review for stage '{pending_items[0][0]}'")
        
        stages_to_run = determine_stages_to_run(db_path, file_hash, force_stage, config, conn)
        
        if not stages_to_run:
            print("    [Cache] All stages up-to-date. Skipping.")
            return
            
        print(f"    [Stages to run] {', '.join(stages_to_run)}")
        
        # Clear cache for the run stages and downstream stages to enforce cascade consistency
        delete_downstream_cache(db_path, file_hash, stages_to_run[0], conn=conn)
        
        # Execute stages
        cached_stages = get_cached_stages(db_path, file_hash, conn=conn)
        text_content = cached_stages.get("docling", {}).get("output_text", "")
        current_model = config["llm"]["model"]
        
        for stage in stages_to_run:
            print(f"    -> Running stage: {stage}...")
            instr_path = STAGE_INSTRUCTIONS[stage]
            current_instr_hash = get_instruction_hash(instr_path) if instr_path else ""
            
            if stage == "docling":
                text_content = run_docling_stage(filepath, db_path, file_hash, config, conn, DocumentConverter)
                if text_content is None:
                    # OneNote skipped
                    return
            elif stage == "filtering":
                if run_filtering_stage(text_content, filepath, db_path, file_hash, output_root, rel_path, config, conn):
                    return
            elif stage == "translation":
                text_content = run_translation_stage(text_content, filepath, db_path, file_hash, config, current_model, current_instr_hash, conn, instr_path, call_llm)
            elif stage in ("subdomain", "doc_type"):
                run_classification_stage(stage, text_content, filepath, db_path, file_hash, config, current_model, current_instr_hash, conn, instr_path, call_llm)
            elif stage == "truthness":
                run_truthness_stage(text_content, filepath, db_path, file_hash, config, current_model, current_instr_hash, conn, instr_path, call_llm)
                
        write_final_output_file(filepath, db_path, file_hash, output_root, rel_path, current_model, conn)
    finally:
        if should_close:
            conn.close()

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
    
    import os
    import concurrent.futures

    pipeline_config = config.get("pipeline", {})
    default_workers = min(4, os.cpu_count() or 1)
    workers = pipeline_config.get("workers", default_workers)
    
    success_count = 0
    error_count = 0
    review_count = 0
    
    if workers > 1:
        def run_one(fpath):
            conn = sqlite3.connect(str(db_path), timeout=30.0)
            try:
                process_file(fpath, raw_root, output_root, db_path, config, force_stage, conn=conn)
                return "success", fpath, None
            except NeedsReviewException as e:
                return "review", fpath, e
            except Exception as e:
                try:
                    file_hash = compute_file_hash(fpath)
                    upsert_file_status(db_path, str(fpath), file_hash, "error", str(e), conn=conn)
                except Exception:
                    pass
                return "error", fpath, e
            finally:
                conn.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(run_one, fpath): fpath for fpath in files_to_process}
            for future in concurrent.futures.as_completed(futures):
                fpath = futures[future]
                try:
                    res_type, _, err = future.result()
                    if res_type == "success":
                        success_count += 1
                    elif res_type == "review":
                        print(f"    [Needs Review] {fpath.name}: {err}")
                        review_count += 1
                    else:
                        print(f"    [Error] Failed to process {fpath}: {err}", file=sys.stderr)
                        error_count += 1
                except Exception as e:
                    print(f"    [Error] Future raised exception for {fpath}: {e}", file=sys.stderr)
                    error_count += 1
    else:
        conn = sqlite3.connect(str(db_path), timeout=30.0)
        try:
            for fpath in files_to_process:
                try:
                    process_file(fpath, raw_root, output_root, db_path, config, force_stage, conn=conn)
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
                        upsert_file_status(db_path, str(fpath), file_hash, "error", str(e), conn=conn)
                    except Exception:
                        pass
        finally:
            conn.close()
                
    print(f"\nPipeline finished. Success: {success_count}, Errors/Failures: {error_count}, Needs Review: {review_count}")
    print(f"Summary: {success_count} processed, {review_count} need review")
    if error_count > 0:
        sys.exit(1)
    sys.exit(0)

if __name__ == "__main__":
    main()
