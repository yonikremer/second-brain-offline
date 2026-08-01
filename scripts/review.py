import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
import yaml

# Ensure project root and scripts folder are in path
sys.path.append(str(Path.cwd()))
sys.path.append(str(Path.cwd() / "scripts"))

from process import (
    process_file,
    delete_downstream_cache,
    append_category_to_file,
    parse_allowed_values,
    STAGE_INSTRUCTIONS,
    STAGES,
    NeedsReviewException
)

def list_pending(db_path: Path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, filepath, stage, trigger_type, proposed_answer FROM review_queue WHERE status = 'pending'"
    )
    rows = cursor.fetchall()
    conn.close()
    
    if not rows:
        print("No pending review items found.")
        return
        
    print(f"{'ID':<6} | {'Stage':<12} | {'Trigger':<15} | {'Filepath':<30} | {'Proposed Answer'}")
    print("-" * 80)
    for r in rows:
        prop = r["proposed_answer"] or ""
        if len(prop) > 30:
            prop = prop[:27] + "..."
        prop = prop.replace("\n", " ")
        print(f"{r['id']:<6} | {r['stage']:<12} | {r['trigger_type']:<15} | {r['filepath']:<30} | {prop}")

def add_to_glossary(term: str, translation: str, notes: str):
    glossary_path = Path("glossary.md")
    if not glossary_path.exists():
        glossary_path.write_text(
            "# Glossary\n\n| Hebrew/Internal Term | English Translation | Notes |\n|---|---|---|\n",
            encoding="utf-8"
        )
    content = glossary_path.read_text(encoding="utf-8")
    if f"| {term} |" in content:
        print(f"    [Glossary] Term '{term}' already exists in glossary.md. Skipping append.")
        return
    
    existing_glossary = content.rstrip()
    new_row = f"| {term} | {translation} | {notes} |\n"
    glossary_path.write_text(existing_glossary + "\n" + new_row, encoding="utf-8")
    print(f"    [Glossary] Added '{term}' -> '{translation}' to glossary.md")

def parse_review_file(file_path: Path) -> dict:
    content = file_path.read_text(encoding="utf-8")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    if not m:
        raise ValueError(f"No frontmatter found in {file_path}")
    frontmatter_text = m.group(1)
    data = yaml.safe_load(frontmatter_text)
    return data

def apply_reviews(db_path: Path, raw_root: Path, output_root: Path, config: dict):
    review_dir = Path("review")
    if not review_dir.exists():
        print("No review/ directory found.")
        return
        
    md_files = list(review_dir.glob("*.md"))
    if not md_files:
        print("No review files found.")
        return

    # Sort by pipeline stage order so upstream reviews (e.g. translation) are
    # resolved before downstream reviews (e.g. truthness) for the same file.
    stage_order = {stage: idx for idx, stage in enumerate(STAGES)}
    def _stage_sort_key(p: Path) -> int:
        try:
            data = parse_review_file(p)
            return stage_order.get(data.get("stage", ""), len(STAGES))
        except Exception:
            return len(STAGES)
    md_files.sort(key=_stage_sort_key)

    resolved_count = 0
    for p in md_files:
        try:
            data = parse_review_file(p)
            queue_id = data.get("queue_id")
            file_hash = data.get("file_hash")
            filepath_str = data.get("filepath")
            stage = data.get("stage")
            trigger = data.get("trigger")
            status = data.get("status")
            human_answer = str(data.get("human_answer") or "").strip()
            resolution_note = str(data.get("resolution_note") or "").strip()
            
            if not status or status == "pending":
                continue
                
            if status not in ("accepted", "rejected"):
                print(f"    [Warning] Invalid status '{status}' in {p.name}. Skipping.")
                continue
                
            conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()
            cursor.execute(
                "SELECT status, context_json, proposed_answer, filepath FROM review_queue WHERE id = ?",
                (queue_id,)
            )
            db_row = cursor.fetchone()
            conn.close()
            
            if not db_row:
                print(f"    [Warning] Review item {queue_id} not found in database. Skipping {p.name}.")
                continue
                
            db_status, context_json, proposed_answer, filepath_str = db_row
            if db_status != "pending":
                continue
                
            print(f"\nApplying review decision: {status.upper()} for stage '{stage}' ({p.name})")

            # Validate accepted reviews have a non-empty human answer where required.
            # Truthness may be empty (defaults to score 10). New category falls back
            # to the LLM's proposed value. Clarification has no fallback, so reject.
            if status == "accepted" and not human_answer:
                if stage == "translation" and trigger == "clarification":
                    print(f"    [Warning] Accepted clarification review has empty human_answer. Treating as rejected.")
                    status = "rejected"
                    resolution_note = (resolution_note + " " if resolution_note else "") + "Empty human_answer; treated as rejected."
                elif stage in ("subdomain", "doc_type") and trigger == "new_category":
                    if proposed_answer:
                        print(f"    [Warning] Accepted new_category review has empty human_answer. Using proposed answer '{proposed_answer}'.")
                        human_answer = proposed_answer
                    else:
                        print(f"    [Warning] Accepted new_category review has empty human_answer and no proposed answer. Treating as rejected.")
                        status = "rejected"
                        resolution_note = (resolution_note + " " if resolution_note else "") + "Empty human_answer; treated as rejected."

            if status == "accepted":
                if stage == "translation" and trigger == "clarification":
                    context_data = json.loads(context_json)
                    term = context_data.get("term", "")
                    add_to_glossary(term, human_answer, resolution_note)
                elif stage in ("subdomain", "doc_type") and trigger == "new_category":
                    val = human_answer if human_answer else proposed_answer
                    instr_path = STAGE_INSTRUCTIONS[stage]
                    allowed_values = parse_allowed_values(instr_path)
                    if any(val.lower() == allowed.lower() for allowed in allowed_values):
                        print(f"    [Category] Category '{val}' already allowed in {instr_path.name}. Skipping.")
                    else:
                        append_category_to_file(instr_path, val, resolution_note)
                        print(f"    [Category] Added '{val}' to {instr_path.name}")
            
            conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE review_queue
                SET status = ?, human_answer = ?, resolution_note = ?, resolved_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (status, human_answer, resolution_note, queue_id))
            conn.commit()
            conn.close()
            
            delete_downstream_cache(db_path, file_hash, stage)
            
            try:
                process_file(Path(filepath_str), raw_root, output_root, db_path, config, force_stage=None)
                print(f"Successfully processed {filepath_str} after review resolution.")
            except NeedsReviewException as e:
                print(f"File {filepath_str} stopped at downstream review: {e}")
            except Exception as e:
                print(f"Error processing {filepath_str} after review resolution: {e}")
                
            resolved_count += 1
            
        except Exception as e:
            print(f"Error parsing or applying review file {p.name}: {e}")
            
    print(f"\nApplied {resolved_count} decisions.")

def clean_reviews(db_path: Path):
    review_dir = Path("review")
    if not review_dir.exists():
        print("No review/ directory found.")
        return
        
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    
    removed_count = 0
    for p in review_dir.glob("*.md"):
        try:
            data = parse_review_file(p)
            file_hash = data.get("file_hash")
            stage = data.get("stage")
            trigger = data.get("trigger")
            
            if file_hash and stage and trigger:
                cursor.execute(
                    "SELECT status FROM review_queue WHERE file_hash = ? AND stage = ? AND trigger_type = ?",
                    (file_hash, stage, trigger)
                )
                row = cursor.fetchone()
                if row and row[0] != 'pending':
                    p.unlink()
                    print(f"Removed resolved review file: {p.name}")
                    removed_count += 1
        except Exception:
            pass
            
    conn.close()
    print(f"Cleaned {removed_count} resolved review files.")

def main():
    parser = argparse.ArgumentParser(description="CLI for Human Review Queue.")
    parser.add_argument("command", choices=["list", "apply", "clean"], help="Review command to execute.")
    args = parser.parse_args()
    
    root = Path.cwd()
    raw_root = root / "raw"
    output_root = root / "processed_md"
    db_path = root / "pipeline.db"
    config_path = root / "pipeline_config.json"
    
    if not config_path.exists():
        print(f"Error: Config file not found at {config_path}.", file=sys.stderr)
        sys.exit(1)
        
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
        
    if args.command == "list":
        list_pending(db_path)
    elif args.command == "apply":
        apply_reviews(db_path, raw_root, output_root, config)
    elif args.command == "clean":
        clean_reviews(db_path)

if __name__ == "__main__":
    main()
