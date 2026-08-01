import json
import re
import sqlite3
import xml.etree.ElementTree as ET
import hashlib
from pathlib import Path


def db_conn(db_path: Path) -> sqlite3.Connection:
    """Return a connection with row_factory set to sqlite3.Row."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn

def compute_file_hash(filepath: Path) -> str:
    hasher = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(8192):
            hasher.update(chunk)
    return hasher.hexdigest()

def get_instruction_hash(instruction_path: Path) -> str:
    if not instruction_path.exists():
        return ""
    content = instruction_path.read_text(encoding="utf-8")
    if "## Allowed" in content:
        content = content.split("## Allowed")[0]
    hasher = hashlib.sha256()
    hasher.update(content.encode("utf-8"))
    return hasher.hexdigest()

def check_guid_filename_ratio(text: str, ratio: float = 0.80) -> bool:
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
        m = re.match(r"^```(?:json|xml)?\s+(.*?)\s+```$", cleaned, re.DOTALL | re.IGNORECASE)
        if m:
            stripped = m.group(1).strip()
            if is_valid_json(stripped) or is_valid_xml(stripped):
                return True

    no_ws = "".join(text.split())
    if no_ws:
        hex_count = len(re.findall(r"[0-9a-fA-F]", no_ws))
        if (hex_count / len(no_ws)) >= ratio:
            return True

    guid_pattern = re.compile(r"\b[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}\b")
    filename_pattern = re.compile(r"\b[\w\-]+\.(?:pdf|docx|doc|txt|md|html|png|jpg|jpeg|zip|json|yml|yaml|csv|xml|xls|xlsx|wav|32fc|16c|32f|one)\b")
    
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
        
    calculated_ratio = matched_non_ws / total_non_ws
    return calculated_ratio >= ratio

def fix_hebrew_layout(text: str, status: str) -> str:
    if status == "NORMAL":
        return text
        
    fixed_lines = []
    for line in text.split('\n'):
        words = line.split()
        if not words:
            fixed_lines.append("")
            continue
            
        if status in ("REVERSED_WORDS", "REVERSED_BOTH"):
            words = [
                w[::-1] if any('\u0590' <= c <= '\u05FF' for c in w) else w 
                for w in words
            ]
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

def load_glossary_entries(glossary_path: Path) -> list[tuple[str, str, str]]:
    if not glossary_path.exists():
        return []
    content = glossary_path.read_text(encoding="utf-8")
    entries = []
    lines = content.splitlines()
    for line in lines:
        line_strip = line.strip()
        if not line_strip.startswith("|"):
            continue
        parts = [p.strip() for p in line_strip.split("|")]
        if len(parts) >= 4:
            term = parts[1]
            translation = parts[2]
            if term == "Hebrew/Internal Term" or all(c in "-:| " for c in term) or not term:
                continue
            notes = parts[3] if len(parts) > 3 else ""
            entries.append((term, translation, notes))
    return entries

def filter_glossary_entries(entries: list[tuple[str, str, str]], text: str) -> list[tuple[str, str, str]]:
    filtered = []
    for term, translation, notes in entries:
        if term and term.lower() in text.lower():
            filtered.append((term, translation, notes))
    return filtered

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

def chunk_text(text: str, max_chunk_size: int = 4000) -> list[str]:
    text = text.replace("\r\n", "\n")
    paragraphs = text.split("\n\n")
    chunks = []
    current_chunk = []
    current_len = 0
    
    for para in paragraphs:
        para_len = len(para)
        if current_len + (2 if current_chunk else 0) + para_len <= max_chunk_size:
            current_chunk.append(para)
            current_len += (2 if len(current_chunk) > 1 else 0) + para_len
        else:
            if current_chunk:
                chunks.append("\n\n".join(current_chunk))
                current_chunk = []
                current_len = 0
                
            if para_len > max_chunk_size:
                sentences = re.split(r'(?<=[.!?])\s+', para)
                current_sentence_chunk = []
                current_sent_len = 0
                
                for sent in sentences:
                    sent_len = len(sent)
                    if current_sent_len + (1 if current_sentence_chunk else 0) + sent_len <= max_chunk_size:
                        current_sentence_chunk.append(sent)
                        current_sent_len += (1 if len(current_sentence_chunk) > 1 else 0) + sent_len
                    else:
                        if current_sentence_chunk:
                            chunks.append(" ".join(current_sentence_chunk))
                            current_sentence_chunk = []
                            current_sent_len = 0
                        if sent_len > max_chunk_size:
                            for i in range(0, sent_len, max_chunk_size):
                                chunks.append(sent[i:i+max_chunk_size])
                        else:
                            current_sentence_chunk.append(sent)
                            current_sent_len = sent_len
                
                if current_sentence_chunk:
                    current_chunk.append(" ".join(current_sentence_chunk))
                    current_len = len(current_chunk[-1])
            else:
                current_chunk.append(para)
                current_len = para_len
                
    if current_chunk:
        chunks.append("\n\n".join(current_chunk))
    return chunks
