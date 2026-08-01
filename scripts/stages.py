import sys
import json
import re
import subprocess
import sqlite3
from pathlib import Path

import email
from email import policy
import extract_msg

# Import helper functions and DB helpers
from db import (
    NeedsReviewException,
    trigger_review,
    upsert_file_status,
    upsert_stage_output,
)
from helpers import (
    check_guid_filename_ratio,
    needs_translation,
    load_glossary_entries,
    filter_glossary_entries,
    fix_hebrew_layout,
    parse_allowed_values,
    chunk_text,
)
from llm_client import parse_json_response

def run_docling_stage(filepath: Path, db_path: Path, file_hash: str, config: dict, conn: sqlite3.Connection, converter_cls=None) -> str:
    if converter_cls is None:
        from docling.document_converter import DocumentConverter as converter_cls
        
    text_content = ""
    if filepath.suffix.lower() == ".one":
        if sys.platform != "win32":
            print("    [OneNote] Warning: OneNote conversion is only supported on Windows.")
            raise RuntimeError("OneNote conversion is only supported on Windows.")
        
        docling_config = config.get("docling", {})
        skip_onenote = docling_config.get("skip_onenote", False)
        if skip_onenote:
            print(f"    [OneNote] Config option 'skip_onenote' is enabled. Skipping {filepath.name}.")
            upsert_file_status(db_path, str(filepath), file_hash, "skipped", conn=conn)
            return None
            
        if not hasattr(run_docling_stage, "_onenote_available"):
            print("    [OneNote] Checking Microsoft OneNote COM registry registration...")
            try:
                import winreg
                try:
                    key = winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "OneNote.Application")
                    winreg.CloseKey(key)
                    run_docling_stage._onenote_available = True
                except OSError:
                    run_docling_stage._onenote_available = False
            except Exception:
                run_docling_stage._onenote_available = (sys.platform == "win32")
            
            if not run_docling_stage._onenote_available:
                print("    [OneNote] Warning: Microsoft OneNote COM interface is not registered on this system. OneNote files will be skipped.")
                
        if not getattr(run_docling_stage, "_onenote_available", False):
            print(f"    [OneNote] Skipping {filepath.name} because OneNote is not available.")
            upsert_file_status(db_path, str(filepath), file_hash, "skipped", conn=conn)
            return None
        
        print("    [OneNote] Converting .one file to temporary .docx via PowerShell COM...")
        temp_docx = filepath.with_suffix(".temp.docx")
        try:
            cmd = [
                "powershell.exe", "-ExecutionPolicy", "Bypass", "-File",
                str(Path("scripts/convert_onenote.ps1").resolve()),
                "-OnePath", str(filepath.resolve()),
                "-DocxPath", str(temp_docx.resolve())
            ]
            timeout = docling_config.get("onenote_timeout", 300)
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if res.returncode != 0:
                raise RuntimeError(
                    f"OneNote COM conversion failed (exit code {res.returncode}).\n"
                    f"STDOUT: {res.stdout}\nSTDERR: {res.stderr}\n"
                    "Tip: Make sure OneNote is open and running in the same privilege context as this script."
                )
            
            converter = converter_cls()
            result = converter.convert(str(temp_docx))
            text_content = result.document.export_to_markdown()
        finally:
            if temp_docx.exists():
                temp_docx.unlink()
    elif filepath.suffix.lower() == ".vsdx":
        print(f"    [Visio] Converting Visio (.vsdx) file {filepath.name} to Markdown...")
        try:
            from scripts.visio_to_markdown_standalone import VisioToMarkdownConverter
            converter = VisioToMarkdownConverter()
            text_content = converter.convert(str(filepath), output_format="markdown")
        except Exception as e:
            print(f"    [Visio] Error converting Visio (.vsdx) file {filepath.name}: {e}")
            raise e
    elif filepath.suffix.lower() in (".eml", ".msg"):
        text_content = ""
        if filepath.suffix.lower() == ".eml":
            try:
                with open(filepath, 'rb') as f:
                    msg = email.message_from_binary_file(f, policy=policy.default)
                subject = msg.get('subject', '')
                if subject:
                    subject = re.sub(r'\b[fF][wW]\b', 'forward', subject)
                sender = msg.get('from', '')
                to = msg.get('to', '')
                date = msg.get('date', '')
                
                body = ""
                body_part = msg.get_body(preferencelist=('plain', 'html'))
                if body_part:
                    body = body_part.get_content()
                
                attachments_list = []
                for part in msg.walk():
                    if part.get_content_disposition() == 'attachment':
                        filename = part.get_filename()
                        if filename:
                            file_data = part.get_payload(decode=True)
                            dest_dir = filepath.parent / f"{filepath.stem}_attachments"
                            dest_dir.mkdir(exist_ok=True)
                            att_path = dest_dir / filename
                            att_path.write_bytes(file_data)
                            attachments_list.append(filename)
                            print(f"    [Email] Extracted attachment: {filename}")
                            
                att_str = f"Attachments: {', '.join(attachments_list)}\n" if attachments_list else ""
                text_content = f"Subject: {subject}\nFrom: {sender}\nTo: {to}\nDate: {date}\n{att_str}\n{body}"
            except Exception as e:
                print(f"    [Email] Warning: Failed to parse .eml file {filepath.name}: {e}")
                converter = converter_cls()
                result = converter.convert(str(filepath))
                text_content = result.document.export_to_markdown()
        else: # .msg
            try:
                with extract_msg.openMsg(str(filepath)) as msg:
                    subject = msg.subject or ""
                    if subject:
                        subject = re.sub(r'\b[fF][wW]\b', 'forward', subject)
                    sender = msg.sender or ""
                    to = msg.to or ""
                    date = msg.date or ""
                    body = msg.body or ""
                    
                    attachments_list = []
                    for att in msg.attachments:
                        dest_dir = filepath.parent / f"{filepath.stem}_attachments"
                        dest_dir.mkdir(exist_ok=True)
                        try:
                            att.save(customPath=str(dest_dir))
                            filename = att.filename or att.longFilename
                            if filename:
                                attachments_list.append(filename)
                                print(f"    [Email] Extracted attachment: {filename}")
                        except Exception:
                            filename = att.filename or att.longFilename
                            if filename:
                                try:
                                    (dest_dir / filename).write_bytes(att.data)
                                    attachments_list.append(filename)
                                    print(f"    [Email] Extracted attachment: {filename}")
                                except Exception:
                                    pass
                                    
                    att_str = f"Attachments: {', '.join(attachments_list)}\n" if attachments_list else ""
                    text_content = f"Subject: {subject}\nFrom: {sender}\nTo: {to}\nDate: {date}\n{att_str}\n{body}"
            except Exception as e:
                print(f"    [Email] Warning: Failed to parse .msg file {filepath.name}: {e}")
                converter = converter_cls()
                result = converter.convert(str(filepath))
                text_content = result.document.export_to_markdown()
    else:
        converter = converter_cls()
        result = converter.convert(str(filepath))
        text_content = result.document.export_to_markdown()
    upsert_stage_output(db_path, file_hash, "docling", text_content, None, None, conn=conn)
    return text_content

def run_filtering_stage(text_content: str, filepath: Path, db_path: Path, file_hash: str, output_root: Path, rel_path: Path, config: dict, conn: sqlite3.Connection) -> bool:
    filtering_config = config.get("filtering", {})
    ratio_threshold = filtering_config.get("guid_filename_ratio", 0.80)
    is_filtered = check_guid_filename_ratio(text_content, ratio_threshold)
    upsert_stage_output(db_path, file_hash, "filtering", "true" if is_filtered else "false", None, None, conn=conn)
    if is_filtered:
        print("    [Filter] File contains >=80% GUIDs or filenames. Stopping.")
        upsert_file_status(db_path, str(filepath), file_hash, "filtered", conn=conn)
        out_path = output_root / rel_path.with_suffix(".md")
        if out_path.exists():
            out_path.unlink()
        return True
    return False

def run_translation_stage(text_content: str, filepath: Path, db_path: Path, file_hash: str, config: dict, current_model: str, current_instr_hash: str, conn: sqlite3.Connection, instr_path: Path, call_llm_fn: callable) -> str:
    if not needs_translation(text_content):
        print("    [Language] Entirely English. Skipping translation.")
        upsert_stage_output(db_path, file_hash, "translation", text_content, current_model, current_instr_hash, conn=conn)
        return text_content

    translation_instructions = instr_path.read_text(encoding="utf-8")
    glossary_path = Path("glossary.md")
    if glossary_path.exists():
        entries = load_glossary_entries(glossary_path)
        filtered = filter_glossary_entries(entries, text_content)
        if filtered:
            active_glossary = "\n\n# Active Glossary (glossary.md)\n\n| Hebrew/Internal Term | English Translation | Notes |\n|---|---|---|\n"
            for term, trans, notes in filtered:
                active_glossary += f"| {term} | {trans} | {notes} |\n"
            translation_instructions += active_glossary
    
    cursor = conn.cursor()
    cursor.execute(
        "SELECT status FROM review_queue WHERE file_hash = ? AND stage = 'translation' AND trigger_type = 'clarification'",
        (file_hash,)
    )
    trans_review = cursor.fetchone()
    
    if trans_review and trans_review[0] == 'rejected':
        translation_instructions += "\n\nIMPORTANT: You must proceed with best-effort translation. Do NOT trigger clarification or output 'Clarification Required'. If there are unknown terms, translate them to the best of your ability."
    
    current_text_to_translate = text_content
    if len(current_text_to_translate) <= 4000:
        while True:
            response_text = call_llm_fn(config, translation_instructions, current_text_to_translate)
            
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
            
            if "Clarification Required" in payload:
                term_match = re.search(r"Term/Issue:\s*(.*)", payload, re.IGNORECASE)
                if not term_match:
                    term_match = re.search(r"Term:\s*(.*)", payload, re.IGNORECASE)
                term_to_clarify = term_match.group(1).strip() if term_match else "unknown term"
                
                context_sentence = ""
                context_match = re.search(r"Context:\s*\"?(.*?)\"?(?:\n|$)", payload, re.IGNORECASE)
                if context_match:
                    context_sentence = context_match.group(1).strip()
                    
                if trans_review and trans_review[0] == 'rejected':
                    print("    [Warning] Translation triggered clarification despite rejected review. Proceeding best-effort.")
                else:
                    context_json = json.dumps({
                        "term": term_to_clarify,
                        "context_sentence": context_sentence
                    })
                    trigger_review(db_path, filepath, file_hash, "translation", "clarification", context_json, "", conn=conn)
                    upsert_file_status(db_path, str(filepath), file_hash, "needs_review", conn=conn)
                    raise NeedsReviewException("Translation clarification required.")
            
            translated_text = payload
            break
    else:
        while True:
            sample = current_text_to_translate[:2000]
            sample_response = call_llm_fn(config, translation_instructions, sample)
            
            status_line = ""
            if sample_response.startswith("RTL_STATUS:"):
                lines = sample_response.split('\n', 1)
                status_line = lines[0].strip()
                
            if any(x in status_line for x in ("REVERSED_WORDS", "REVERSED_SENTENCES", "REVERSED_BOTH")):
                detected_status = "REVERSED_BOTH" if "REVERSED_BOTH" in status_line else ("REVERSED_WORDS" if "REVERSED_WORDS" in status_line else "REVERSED_SENTENCES")
                print(f"    [RTL Status] Detected corruption: {detected_status}. Fixing locally and retrying...")
                current_text_to_translate = fix_hebrew_layout(current_text_to_translate, detected_status)
                continue
            break
            
        chunks = chunk_text(current_text_to_translate, 4000)
        translated_chunks = []
        for idx, chunk in enumerate(chunks):
            if len(chunks) > 1:
                print(f"    [Translation] Translating chunk {idx+1}/{len(chunks)}...")
            response_text = call_llm_fn(config, translation_instructions, chunk)
            
            payload = response_text
            if response_text.startswith("RTL_STATUS:"):
                lines = response_text.split('\n', 1)
                payload = lines[1].strip() if len(lines) > 1 else ""
                
            if "Clarification Required" in payload:
                term_match = re.search(r"Term/Issue:\s*(.*)", payload, re.IGNORECASE)
                if not term_match:
                    term_match = re.search(r"Term:\s*(.*)", payload, re.IGNORECASE)
                term_to_clarify = term_match.group(1).strip() if term_match else "unknown term"
                
                context_sentence = ""
                context_match = re.search(r"Context:\s*\"?(.*?)\"?(?:\n|$)", payload, re.IGNORECASE)
                if context_match:
                    context_sentence = context_match.group(1).strip()
                    
                if trans_review and trans_review[0] == 'rejected':
                    print("    [Warning] Translation triggered clarification despite rejected review. Proceeding best-effort.")
                else:
                    context_json = json.dumps({
                        "term": term_to_clarify,
                        "context_sentence": context_sentence
                    })
                    trigger_review(db_path, filepath, file_hash, "translation", "clarification", context_json, "", conn=conn)
                    upsert_file_status(db_path, str(filepath), file_hash, "needs_review", conn=conn)
                    raise NeedsReviewException("Translation clarification required.")
                    
            translated_chunks.append(payload)
        translated_text = "\n".join(translated_chunks)
        
    upsert_stage_output(db_path, file_hash, "translation", translated_text, current_model, current_instr_hash, conn=conn)
    return translated_text

def run_classification_stage(stage: str, trans_text: str, filepath: Path, db_path: Path, file_hash: str, config: dict, current_model: str, current_instr_hash: str, conn: sqlite3.Connection, instr_path: Path, call_llm_fn: callable) -> str:
    instructions = instr_path.read_text(encoding="utf-8")
    
    cursor = conn.cursor()
    cursor.execute(
        "SELECT status, proposed_answer FROM review_queue WHERE file_hash = ? AND stage = ? AND trigger_type = 'new_category'",
        (file_hash, stage)
    )
    cat_review = cursor.fetchone()
    
    rejected_category = None
    if cat_review and cat_review[0] == 'rejected':
        rejected_category = cat_review[1]
        instructions += f"\n\nNote: The category '{rejected_category}' is NOT allowed. Please classify into one of the other allowed categories."
    
    val = call_llm_fn(config, instructions, trans_text)
    val_clean = val.strip()
    
    allowed_values = parse_allowed_values(instr_path)
    matched = None
    for allowed in allowed_values:
        if val_clean.lower() == allowed.lower():
            matched = allowed
            break
            
    if matched:
        upsert_stage_output(db_path, file_hash, stage, matched, current_model, current_instr_hash, conn=conn)
        return matched
    else:
        if rejected_category and val_clean.lower() == rejected_category.lower():
            fallback = "other" if "other" in [a.lower() for a in allowed_values] else (allowed_values[0] if allowed_values else "other")
            for allowed in allowed_values:
                if allowed.lower() == fallback.lower():
                    fallback = allowed
                    break
            print(f"    [Warning] LLM proposed rejected category '{val_clean}'. Falling back to '{fallback}'.")
            upsert_stage_output(db_path, file_hash, stage, fallback, current_model, current_instr_hash, conn=conn)
            return fallback
        else:
            context_json = json.dumps({
                "proposed_value": val_clean,
                "existing_values": allowed_values,
                "focus_hint": "Added automatically in non-interactive run."
            })
            trigger_review(db_path, filepath, file_hash, stage, "new_category", context_json, val_clean, conn=conn)
            upsert_file_status(db_path, str(filepath), file_hash, "needs_review", conn=conn)
            raise NeedsReviewException(f"New {stage} category '{val_clean}' proposed.")

def run_truthness_stage(trans_text: str, filepath: Path, db_path: Path, file_hash: str, config: dict, current_model: str, current_instr_hash: str, conn: sqlite3.Connection, instr_path: Path, call_llm_fn: callable) -> str:
    truthness_instructions = instr_path.read_text(encoding="utf-8")
    
    cursor = conn.cursor()
    cursor.execute(
        "SELECT status, proposed_answer FROM review_queue WHERE file_hash = ? AND stage = 'truthness'",
        (file_hash,)
    )
    truth_review_row = cursor.fetchone()
    
    if truth_review_row and truth_review_row[0] in ('accepted', 'rejected'):
        truthness_val = truth_review_row[1]
        print(f"    [Truthness Review] Already resolved: {truth_review_row[0]}. Using cached proposed answer.")
    else:
        truthness_val = call_llm_fn(config, truthness_instructions, trans_text)
    
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
        cursor.execute(
            "SELECT status FROM review_queue WHERE file_hash = ? AND stage = 'truthness' AND trigger_type = ?",
            (file_hash, trigger_type)
        )
        truth_review = cursor.fetchone()
        
        if truth_review and truth_review[0] in ('accepted', 'rejected'):
            print(f"    [Truthness Review] Already resolved: {truth_review[0]}. Proceeding.")
            upsert_stage_output(db_path, file_hash, "truthness", truthness_val, current_model, current_instr_hash, conn=conn)
        else:
            if is_parse_failure:
                context_json = json.dumps({
                    "raw_response": truthness_val,
                    "parsed_score": None,
                    "parsed_justification": None
                })
                trigger_review(db_path, filepath, file_hash, "truthness", "parse_failure", context_json, truthness_val, conn=conn)
                upsert_file_status(db_path, str(filepath), file_hash, "needs_review", conn=conn)
                raise NeedsReviewException("Truthness parse failure.")
            else:
                context_json = json.dumps({
                    "raw_response": truthness_val,
                    "parsed_score": score,
                    "parsed_justification": justification
                })
                trigger_review(db_path, filepath, file_hash, "truthness", "low_score", context_json, json.dumps({"score": score, "justification": justification}), conn=conn)
                upsert_file_status(db_path, str(filepath), file_hash, "needs_review", conn=conn)
                raise NeedsReviewException(f"Truthness score {score} is below threshold.")
    else:
        upsert_stage_output(db_path, file_hash, "truthness", truthness_val, current_model, current_instr_hash, conn=conn)
    return truthness_val
