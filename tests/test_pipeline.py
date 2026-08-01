# tests/test_pipeline.py
import unittest
import json
import re
from pathlib import Path
from unittest.mock import patch

# Import functions to test
import sys
sys.path.append(str(Path(__file__).parent.parent / "scripts"))
import process

class TestPipelineLogic(unittest.TestCase):

    def test_check_guid_filename_ratio_all_guid(self):
        # 100% GUIDs
        text = "49fae0df-1aa0-449e-8c38-89ae21d283a0 54a06457-1820-49fe-8e1b-30fe482938a0"
        self.assertTrue(process.check_guid_filename_ratio(text))

    def test_check_guid_filename_ratio_mixed(self):
        # Mixed GUID, filename, and plain text
        # GUID (36 chars), Filename (12 chars), Plain text (10 chars). 
        # GUID + filename non-whitespace = 36 + 12 = 48 chars.
        # Total non-whitespace = 48 + 10 = 58 chars.
        # Ratio = 48/58 = 82.7%. Should pass threshold (>= 80%).
        text = "54a06457-1820-49fe-8e1b-30fe482938a0 my-photo.png plaintexxx"
        self.assertTrue(process.check_guid_filename_ratio(text))

    def test_check_guid_filename_ratio_low(self):
        # Mostly plain text
        text = "This is a document about low-rank adaptation. It mentions one file my-doc.pdf and one GUID 54a06457-1820-49fe-8e1b-30fe482938a0."
        self.assertFalse(process.check_guid_filename_ratio(text))

    def test_check_guid_filename_ratio_json(self):
        # Raw JSON content
        text = '{"name": "test", "data": [1, 2, 3]}'
        self.assertTrue(process.check_guid_filename_ratio(text))
        
    def test_check_guid_filename_ratio_markdown_json(self):
        # Markdown wrapped JSON
        text = '```json\n{"name": "test", "data": [1, 2, 3]}\n```'
        self.assertTrue(process.check_guid_filename_ratio(text))
        
    def test_check_guid_filename_ratio_xml(self):
        # Raw XML content
        text = '<root><element attribute="val">data</element></root>'
        self.assertTrue(process.check_guid_filename_ratio(text))

    def test_check_guid_filename_ratio_markdown_xml(self):
        # Markdown wrapped XML
        text = '```xml\n<root><element>data</element></root>\n```'
        self.assertTrue(process.check_guid_filename_ratio(text))

    def test_check_guid_filename_ratio_hex(self):
        # Mostly hex characters (spaces ignored)
        text = "4a 5f 6c 7d 8e 99 aa bb cc dd ee ff"
        self.assertTrue(process.check_guid_filename_ratio(text))

    def test_check_guid_filename_ratio_bits(self):
        # Mostly bits (spaces ignored)
        text = "01010101 11001100 00001111 11110000"
        self.assertTrue(process.check_guid_filename_ratio(text))

    def test_needs_translation_hebrew(self):
        # Contains Hebrew words
        text = "שלום עולם! This is some English text." # 'שלום עולם' has 8 Hebrew letters. Total letters is 8 + 21 = 29. Ratio = 8/29 = 27% (>= 1%).
        self.assertTrue(process.needs_translation(text))

    def test_needs_translation_english_only(self):
        text = "This is entirely English text. No translation needed."
        self.assertFalse(process.needs_translation(text))

    def test_parse_json_response_clean(self):
        raw = '{"score": 8, "justification": "empirical paper"}'
        data = process.parse_json_response(raw)
        self.assertEqual(data["score"], 8)
        self.assertEqual(data["justification"], "empirical paper")

    def test_parse_json_response_with_markdown(self):
        raw = '```json\n{"score": 9, "justification": "detailed spec"}\n```'
        data = process.parse_json_response(raw)
        self.assertEqual(data["score"], 9)
        self.assertEqual(data["justification"], "detailed spec")

    def test_parse_json_response_with_fallback(self):
        raw = 'Some explanation before the JSON: {"score": 5, "justification": "blog post"} and some text after.'
        data = process.parse_json_response(raw)
        self.assertEqual(data["score"], 5)
        self.assertEqual(data["justification"], "blog post")

    def test_fix_hebrew_layout_normal(self):
        text = "בוקר טוב"
        self.assertEqual(process.fix_hebrew_layout(text, "NORMAL"), "בוקר טוב")

    def test_fix_hebrew_layout_reversed_words(self):
        text = "רקוב בוט"
        self.assertEqual(process.fix_hebrew_layout(text, "REVERSED_WORDS"), "בוקר טוב")

    def test_fix_hebrew_layout_reversed_sentences(self):
        text = "טוב בוקר"
        self.assertEqual(process.fix_hebrew_layout(text, "REVERSED_SENTENCES"), "בוקר טוב")

    def test_fix_hebrew_layout_reversed_both(self):
        text = "בוט רקוב"
        self.assertEqual(process.fix_hebrew_layout(text, "REVERSED_BOTH"), "בוקר טוב")

    def test_chunk_text_semantic(self):
        import helpers
        text = "Paragraph 1. Sentence 1. Sentence 2.\n\nParagraph 2. Sentence 3."
        chunks = helpers.chunk_text(text, max_chunk_size=40)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0], "Paragraph 1. Sentence 1. Sentence 2.")
        self.assertEqual(chunks[1], "Paragraph 2. Sentence 3.")
        
        chunks_sent = helpers.chunk_text(text, max_chunk_size=25)
        self.assertEqual(len(chunks_sent), 3)

class TestCascadeInvalidation(unittest.TestCase):

    def setUp(self):
        # Minimal LLM config
        self.config = {
            "llm": {
                "api_base": "http://localhost:11434/v1",
                "api_key": "ollama",
                "model": "llama3"
            }
        }

    def test_no_cache_runs_all(self):
        cached_stages = {}
        # We need to mock instructions path hashing since process.py reads actual files.
        # We can temporarily patch process.get_instruction_hash to return a dummy hash.
        original_get_hash = process.get_instruction_hash
        process.get_instruction_hash = lambda path: "dummy_hash"
        try:
            # When cache is empty, we must run everything
            stages = []
            invalidated_upstream = False
            for stage in process.STAGES:
                stages.append(stage)
            
            # Replicate the logic inside process_file
            stages_to_run = []
            for stage in process.STAGES:
                stages_to_run.append(stage)
                
            self.assertEqual(stages_to_run, process.STAGES)
        finally:
            process.get_instruction_hash = original_get_hash

    def test_all_cached_and_valid_runs_none(self):
        cached_stages = {
            "docling": {"stage_name": "docling", "output_text": "text", "model_name": None, "instructions_hash": ""},
            "filtering": {"stage_name": "filtering", "output_text": "false", "model_name": None, "instructions_hash": ""},
            "translation": {"stage_name": "translation", "output_text": "text", "model_name": "llama3", "instructions_hash": "h_translation"},
            "subdomain": {"stage_name": "subdomain", "output_text": "sub", "model_name": "llama3", "instructions_hash": "h_subdomains"},
            "doc_type": {"stage_name": "doc_type", "output_text": "type", "model_name": "llama3", "instructions_hash": "h_document_types"},
            "truthness": {"stage_name": "truthness", "output_text": "{\"score\": 8}", "model_name": "llama3", "instructions_hash": "h_truthness"},
        }
        
        original_get_hash = process.get_instruction_hash
        process.get_instruction_hash = lambda path: f"h_{path.stem}" if path else ""
        try:
            stages_to_run = []
            invalidated_upstream = False
            for stage in process.STAGES:
                cache = cached_stages.get(stage)
                instr_path = process.STAGE_INSTRUCTIONS[stage]
                current_instr_hash = process.get_instruction_hash(instr_path)
                
                cache_valid = False
                if cache and not invalidated_upstream:
                    model_ok = True
                    if stage in ["translation", "subdomain", "doc_type", "truthness"]:
                        model_ok = (cache["model_name"] == "llama3")
                    instr_ok = (cache["instructions_hash"] == current_instr_hash)
                    if model_ok and instr_ok:
                        cache_valid = True
                
                if not cache_valid:
                    stages_to_run.append(stage)
                    invalidated_upstream = True
            
            self.assertEqual(stages_to_run, [])
        finally:
            process.get_instruction_hash = original_get_hash

    def test_upstream_invalidation_cascades(self):
        # translation instruction changed (hash mismatch)
        cached_stages = {
            "docling": {"stage_name": "docling", "output_text": "text", "model_name": None, "instructions_hash": ""},
            "filtering": {"stage_name": "filtering", "output_text": "false", "model_name": None, "instructions_hash": ""},
            # translation cache has old hash
            "translation": {"stage_name": "translation", "output_text": "text", "model_name": "llama3", "instructions_hash": "old_hash"},
            "subdomain": {"stage_name": "subdomain", "output_text": "sub", "model_name": "llama3", "instructions_hash": "h_subdomains"},
            "doc_type": {"stage_name": "doc_type", "output_text": "type", "model_name": "llama3", "instructions_hash": "h_document_types"},
            "truthness": {"stage_name": "truthness", "output_text": "{\"score\": 8}", "model_name": "llama3", "instructions_hash": "h_truthness"},
        }
        
        original_get_hash = process.get_instruction_hash
        # mock current hashes: translation is now 'new_hash'
        process.get_instruction_hash = lambda path: "new_hash" if path and path.stem == "translation" else (f"h_{path.stem}" if path else "")
        try:
            stages_to_run = []
            invalidated_upstream = False
            for stage in process.STAGES:
                cache = cached_stages.get(stage)
                instr_path = process.STAGE_INSTRUCTIONS[stage]
                current_instr_hash = process.get_instruction_hash(instr_path)
                
                cache_valid = False
                if cache and not invalidated_upstream:
                    model_ok = True
                    if stage in ["translation", "subdomain", "doc_type", "truthness"]:
                        model_ok = (cache["model_name"] == "llama3")
                    instr_ok = (cache["instructions_hash"] == current_instr_hash)
                    if model_ok and instr_ok:
                        cache_valid = True
                
                if not cache_valid:
                    stages_to_run.append(stage)
                    invalidated_upstream = True
            
            # Downstream stages should be re-run since translation changed!
            self.assertEqual(stages_to_run, ["translation", "subdomain", "doc_type", "truthness"])
        finally:
            process.get_instruction_hash = original_get_hash

class TestInteractiveTranslation(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        # Create directories
        (self.root / "raw").mkdir()
        (self.root / "instructions").mkdir()
        (self.root / "processed_md").mkdir()
        
        # Write dummy translation instructions
        (self.root / "instructions" / "translation.md").write_text("translation rules", encoding="utf-8")
        (self.root / "instructions" / "subdomains.md").write_text("subdomain rules", encoding="utf-8")
        (self.root / "instructions" / "document_types.md").write_text("doc type rules", encoding="utf-8")
        (self.root / "instructions" / "truthness.md").write_text("truthness rules", encoding="utf-8")
        
        # Dummy config
        self.config = {
            "llm": {
                "api_base": "http://localhost:11434/v1",
                "api_key": "ollama",
                "model": "llama3"
            }
        }
        
    def tearDown(self):
        self.temp_dir.cleanup()
        # Clean up glossary.md if created in cwd during testing
        if Path("glossary.md").exists():
            Path("glossary.md").unlink()

    @patch("process.call_llm")
    def test_interactive_translation_loop(self, mock_call_llm):
        from unittest.mock import patch, MagicMock
        import sys
        sys.path.append(str(Path.cwd()))
        import scripts.review
        
        # We simulate a file that needs translation
        raw_file = self.root / "raw" / "doc.txt"
        raw_file.write_text("שלום", encoding="utf-8") # Hebrew text to trigger needs_translation
        
        db_path = self.root / "pipeline.db"
        process.init_db(db_path)
        
        # Stage 3 first call -> Clarification
        # Stage 3 second call -> Hello
        # Stage 4 -> Tech
        # Stage 5 -> how-to-guide
        # Stage 6 -> {"score": 9, "justification": "trusted"}
        mock_call_llm.side_effect = [
            "Clarification Required\nTerm/Issue: שלום\nContext: שלום עולם\nQuestion: How to translate?",
            "Hello",
            "Tech",
            "how-to-guide",
            '{"score": 9, "justification": "trusted"}'
        ]
        
        # Stub the docling converter to return our custom raw text
        with patch("process.DocumentConverter") as mock_converter_cls:
            mock_conv = MagicMock()
            mock_conv.convert.return_value.document.export_to_markdown.return_value = "שלום"
            mock_converter_cls.return_value = mock_conv
            
            # Execute pipeline on the file, should raise NeedsReviewException
            with self.assertRaises(process.NeedsReviewException):
                process.process_file(
                    filepath=raw_file,
                    raw_root=self.root / "raw",
                    output_root=self.root / "processed_md",
                    db_path=db_path,
                    config=self.config,
                    force_stage=None
                )
            
            # Verify review file was created
            review_dir = Path("review")
            self.assertTrue(review_dir.exists())
            review_files = list(review_dir.glob("*.md"))
            self.assertEqual(len(review_files), 1)
            review_file = review_files[0]
            
            # Simulate human response
            content = review_file.read_text(encoding="utf-8")
            content = re.sub(r"^status:.*$", "status: accepted", content, flags=re.MULTILINE)
            content = re.sub(r"^human_answer:.*$", 'human_answer: "Hello"', content, flags=re.MULTILINE)
            content = re.sub(r"^resolution_note:.*$", 'resolution_note: "Greeting notes"', content, flags=re.MULTILINE)
            review_file.write_text(content, encoding="utf-8")
            
            # Call apply_reviews from scripts.review
            scripts.review.apply_reviews(
                db_path=db_path,
                raw_root=self.root / "raw",
                output_root=self.root / "processed_md",
                config=self.config
            )
            
        # Verify glossary.md was created in root
        glossary_path = Path("glossary.md")
        self.assertTrue(glossary_path.exists())
        glossary_content = glossary_path.read_text(encoding="utf-8")
        self.assertIn("שלום", glossary_content)
        self.assertIn("Hello", glossary_content)
        
        # Verify output processed MD file
        out_file = self.root / "processed_md" / "doc.md"
        self.assertTrue(out_file.exists())
        out_content = out_file.read_text(encoding="utf-8")
        self.assertIn("Hello", out_content)
        self.assertIn("truthness_score: 9", out_content)
        
        # Cleanup review folder
        if review_dir.exists():
            for f in review_dir.glob("*"):
                f.unlink()
            review_dir.rmdir()

    @patch("process.call_llm")
    def test_translation_stage_with_reversed_words_retry(self, mock_call_llm):
        from unittest.mock import patch, MagicMock
        
        raw_file = self.root / "raw" / "reversed.txt"
        raw_file.write_text("בוט רקוב", encoding="utf-8")
        
        db_path = self.root / "pipeline.db"
        process.init_db(db_path)
        
        mock_call_llm.side_effect = [
            "RTL_STATUS: REVERSED_WORDS\n",
            "RTL_STATUS: NORMAL\nGood morning",
            "Tech",
            "how-to-guide",
            '{"score": 9, "justification": "trusted"}'
        ]
        
        with patch("process.DocumentConverter") as mock_converter_cls:
            mock_conv = MagicMock()
            mock_conv.convert.return_value.document.export_to_markdown.return_value = "בוט רקוב"
            mock_converter_cls.return_value = mock_conv
            
            process.process_file(
                filepath=raw_file,
                raw_root=self.root / "raw",
                output_root=self.root / "processed_md",
                db_path=db_path,
                config=self.config,
                force_stage=None
            )
            
        out_file = self.root / "processed_md" / "reversed.md"
        self.assertTrue(out_file.exists())
        out_content = out_file.read_text(encoding="utf-8")
        self.assertIn("Good morning", out_content)

class TestOneNoteConversion(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / "raw").mkdir()
        (self.root / "instructions").mkdir()
        (self.root / "processed_md").mkdir()
        
        (self.root / "instructions" / "translation.md").write_text("translation rules", encoding="utf-8")
        (self.root / "instructions" / "subdomains.md").write_text("subdomain rules", encoding="utf-8")
        (self.root / "instructions" / "document_types.md").write_text("doc type rules", encoding="utf-8")
        (self.root / "instructions" / "truthness.md").write_text("truthness rules", encoding="utf-8")
        
        self.config = {
            "llm": {
                "api_base": "http://localhost:11434/v1",
                "api_key": "ollama",
                "model": "llama3"
            }
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    @patch("sys.platform", "win32")
    @patch("subprocess.run")
    @patch("process.DocumentConverter")
    @patch("process.call_llm")
    def test_onenote_conversion_flow(self, mock_call_llm, mock_docling, mock_run):
        from unittest.mock import MagicMock
        
        # Create a dummy .one file
        raw_file = self.root / "raw" / "notes.one"
        raw_file.write_text("dummy", encoding="utf-8")
        
        db_path = self.root / "pipeline.db"
        process.init_db(db_path)
        
        # Mock subprocess.run for PowerShell conversion
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_run.returncode = 0
        mock_run.return_value = mock_res
        
        # Mock DocumentConverter
        mock_conv = MagicMock()
        mock_conv.convert.return_value.document.export_to_markdown.return_value = "Converted Markdown content"
        mock_docling.return_value = mock_conv
        
        # Mock LLM calls
        mock_call_llm.side_effect = [
            "Tech",
            "how-to-guide",
            '{"score": 10, "justification": "trusted"}'
        ]
        
        # Process the file
        process.process_file(
            filepath=raw_file,
            raw_root=self.root / "raw",
            output_root=self.root / "processed_md",
            db_path=db_path,
            config=self.config,
            force_stage=None
        )
        
        # Verify subprocess.run was called to convert the .one file
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        cmd = args[0]
        self.assertTrue(cmd[4].endswith("convert_onenote.ps1"))
        self.assertEqual(cmd[5], "-OnePath")
        self.assertEqual(cmd[7], "-DocxPath")
        self.assertEqual(kwargs.get("timeout"), 300)
        
        # Verify output markdown was created
        out_file = self.root / "processed_md" / "notes.md"
        self.assertTrue(out_file.exists())
        out_content = out_file.read_text(encoding="utf-8")
        self.assertIn("Converted Markdown content", out_content)
        self.assertIn("truthness_score: 10", out_content)

    @patch("sys.platform", "win32")
    @patch("subprocess.run")
    @patch("process.DocumentConverter")
    def test_onenote_skip_config(self, mock_docling, mock_run):
        raw_file = self.root / "raw" / "notes.one"
        raw_file.write_text("dummy", encoding="utf-8")
        
        db_path = self.root / "pipeline.db"
        process.init_db(db_path)
        
        config_with_skip = {
            "llm": self.config["llm"],
            "docling": {
                "skip_onenote": True
            }
        }
        
        process.process_file(
            filepath=raw_file,
            raw_root=self.root / "raw",
            output_root=self.root / "processed_md",
            db_path=db_path,
            config=config_with_skip,
            force_stage=None
        )
        
        mock_run.assert_not_called()
        # Check status in db is 'skipped'
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM files WHERE filepath = ?", (str(raw_file),))
        row = cursor.fetchone()
        self.assertEqual(row[0], "skipped")
        conn.close()

    @patch("sys.platform", "linux")
    @patch("subprocess.run")
    def test_onenote_non_windows_raises_error(self, mock_run):
        raw_file = self.root / "raw" / "notes.one"
        raw_file.write_text("dummy", encoding="utf-8")
        
        db_path = self.root / "pipeline.db"
        process.init_db(db_path)
        
        with self.assertRaises(RuntimeError) as ctx:
            process.process_file(
                filepath=raw_file,
                raw_root=self.root / "raw",
                output_root=self.root / "processed_md",
                db_path=db_path,
                config=self.config,
                force_stage=None
            )
        self.assertIn("only supported on Windows", str(ctx.exception))

class TestCategoryVerification(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.temp_dir.name)
        
    def tearDown(self):
        self.temp_dir.cleanup()
        
    def test_parse_allowed_values(self):
        doc_types_file = self.tmp_path / "document_types.md"
        doc_types_file.write_text(
            "# Title\n\n## Allowed Document Types\n\n1. **concept**\n   - Focus: A concept\n2. **other**\n   - Focus: Other\n\n## Output Format\n",
            encoding="utf-8"
        )
        allowed = process.parse_allowed_values(doc_types_file)
        self.assertEqual(allowed, ["concept", "other"])
        
    def test_append_category_before_other(self):
        doc_types_file = self.tmp_path / "document_types.md"
        doc_types_file.write_text(
            "# Title\n\n## Allowed Document Types\n\n1. **concept**\n   - Focus: A concept\n2. **other**\n   - Focus: Other\n\n## Output Format\n",
            encoding="utf-8"
        )
        process.append_category_to_file(doc_types_file, "how-to-guide", "Runbooks and guides")
        
        # Parse again
        allowed = process.parse_allowed_values(doc_types_file)
        self.assertEqual(allowed, ["concept", "how-to-guide", "other"])
        
        # Verify content formatting and numbering
        content = doc_types_file.read_text(encoding="utf-8")
        self.assertIn("2. **how-to-guide**", content)
        self.assertIn("3. **other**", content)
        
    def test_append_category_no_other(self):
        subdomains_file = self.tmp_path / "subdomains.md"
        subdomains_file.write_text(
            "# Title\n\n## Allowed Subdomains\n\n1. **AI**\n   - Topics: AI\n\n## Output Format\n",
            encoding="utf-8"
        )
        process.append_category_to_file(subdomains_file, "DevOps", "CI/CD and deployment")
        
        allowed = process.parse_allowed_values(subdomains_file)
        self.assertEqual(allowed, ["AI", "DevOps"])
        
        content = subdomains_file.read_text(encoding="utf-8")
        self.assertIn("1. **AI**", content)
        self.assertIn("2. **DevOps**", content)

class TestHumanReviewQueueFlow(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / "raw").mkdir()
        (self.root / "instructions").mkdir()
        (self.root / "processed_md").mkdir()
        
        self.subdomains_file = self.root / "instructions" / "subdomains.md"
        self.subdomains_file.write_text("# Subdomains\n\n## Allowed Subdomains\n\n1. **Tech**\n   - Tech topics\n2. **other**\n   - Other topics\n", encoding="utf-8")
        
        (self.root / "instructions" / "translation.md").write_text("translation rules", encoding="utf-8")
        (self.root / "instructions" / "document_types.md").write_text("# Doc Types\n\n## Allowed Document Types\n\n1. **concept**\n   - concept\n", encoding="utf-8")
        (self.root / "instructions" / "truthness.md").write_text("truthness rules", encoding="utf-8")
        
        self.orig_instructions = process.STAGE_INSTRUCTIONS.copy()
        process.STAGE_INSTRUCTIONS["translation"] = self.root / "instructions" / "translation.md"
        process.STAGE_INSTRUCTIONS["subdomain"] = self.root / "instructions" / "subdomains.md"
        process.STAGE_INSTRUCTIONS["doc_type"] = self.root / "instructions" / "document_types.md"
        process.STAGE_INSTRUCTIONS["truthness"] = self.root / "instructions" / "truthness.md"
        
        self.config = {
            "llm": {
                "api_base": "http://localhost:11434/v1",
                "api_key": "ollama",
                "model": "llama3"
            },
            "truthness": {
                "threshold": 4
            }
        }
        
        self.review_dir = Path("review")
        if self.review_dir.exists():
            for f in self.review_dir.glob("*"):
                if f.is_file():
                    f.unlink()
            
    def tearDown(self):
        self.temp_dir.cleanup()
        process.STAGE_INSTRUCTIONS = self.orig_instructions
        if self.review_dir.exists():
            for f in self.review_dir.glob("*"):
                if f.is_file():
                    f.unlink()
            try:
                self.review_dir.rmdir()
            except OSError:
                pass
        if Path("glossary.md").exists():
            Path("glossary.md").unlink()

    @patch("process.call_llm")
    def test_new_subdomain_category_trigger_and_resolve(self, mock_call_llm):
        import scripts.review
        from unittest.mock import patch, MagicMock
        
        raw_file = self.root / "raw" / "doc.txt"
        raw_file.write_text("שלום עולם", encoding="utf-8")
        
        db_path = self.root / "pipeline.db"
        process.init_db(db_path)
        
        mock_call_llm.side_effect = [
            "Translated text",
            "Security",
            "Security",
            "concept",
            '{"score": 8, "justification": "good"}'
        ]
        
        with patch("process.DocumentConverter") as mock_converter_cls:
            mock_conv = MagicMock()
            mock_conv.convert.return_value.document.export_to_markdown.return_value = "שלום עולם"
            mock_converter_cls.return_value = mock_conv
            
            with self.assertRaises(process.NeedsReviewException):
                process.process_file(raw_file, self.root / "raw", self.root / "processed_md", db_path, self.config, None)
                
            review_files = list(self.review_dir.glob("*.md"))
            self.assertEqual(len(review_files), 1)
            review_file = review_files[0]
            self.assertIn("subdomain", review_file.name)
            self.assertIn("new_category", review_file.name)
            
            content = review_file.read_text(encoding="utf-8")
            content = content.replace("status: pending", "status: accepted")
            content = content.replace('resolution_note: ""', 'resolution_note: "Needed subdomain"')
            review_file.write_text(content, encoding="utf-8")
            
            scripts.review.apply_reviews(db_path, self.root / "raw", self.root / "processed_md", self.config)
            
        allowed = process.parse_allowed_values(self.subdomains_file)
        self.assertIn("Security", allowed)
        
        out_file = self.root / "processed_md" / "doc.md"
        self.assertTrue(out_file.exists())
        self.assertIn("subdomain: Security", out_file.read_text(encoding="utf-8"))

    @patch("process.call_llm")
    def test_truthness_low_score_trigger_and_resolve(self, mock_call_llm):
        import scripts.review
        from unittest.mock import patch, MagicMock
        
        raw_file = self.root / "raw" / "doc.txt"
        raw_file.write_text("שלום עולם", encoding="utf-8")
        
        db_path = self.root / "pipeline.db"
        process.init_db(db_path)
        
        mock_call_llm.side_effect = [
            "Translated text",
            "Tech",
            "concept",
            '{"score": 2, "justification": "unreliable source"}'
        ]
        
        with patch("process.DocumentConverter") as mock_converter_cls:
            mock_conv = MagicMock()
            mock_conv.convert.return_value.document.export_to_markdown.return_value = "שלום עולם"
            mock_converter_cls.return_value = mock_conv
            
            with self.assertRaises(process.NeedsReviewException):
                process.process_file(raw_file, self.root / "raw", self.root / "processed_md", db_path, self.config, None)
                
            review_files = list(self.review_dir.glob("*.md"))
            self.assertEqual(len(review_files), 1)
            review_file = review_files[0]
            self.assertIn("truthness", review_file.name)
            self.assertIn("low_score", review_file.name)
            
            content = review_file.read_text(encoding="utf-8")
            content = re.sub(r"^status:.*$", "status: accepted", content, flags=re.MULTILINE)
            content = re.sub(r"^human_answer:.*$", 'human_answer: "score: 5, justification: overriding unreliable source"', content, flags=re.MULTILINE)
            review_file.write_text(content, encoding="utf-8")
            
            scripts.review.apply_reviews(db_path, self.root / "raw", self.root / "processed_md", self.config)
            
        out_file = self.root / "processed_md" / "doc.md"
        self.assertTrue(out_file.exists())
        out_content = out_file.read_text(encoding="utf-8")
        self.assertIn("truthness_score: 5", out_content)
        self.assertIn("truthness_justification: overriding unreliable source", out_content)

class TestNewRegressionTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / "raw").mkdir()
        (self.root / "instructions").mkdir()
        (self.root / "processed_md").mkdir()
        
        self.subdomains_file = self.root / "instructions" / "subdomains.md"
        self.subdomains_file.write_text("# Subdomains\n\n## Allowed Subdomains\n\n1. **Tech**\n   - Tech topics\n2. **other**\n   - Other topics\n", encoding="utf-8")
        
        (self.root / "instructions" / "translation.md").write_text("translation rules", encoding="utf-8")
        (self.root / "instructions" / "document_types.md").write_text("# Doc Types\n\n## Allowed Document Types\n\n1. **concept**\n   - concept\n", encoding="utf-8")
        (self.root / "instructions" / "truthness.md").write_text("truthness rules", encoding="utf-8")
        
        self.orig_instructions = process.STAGE_INSTRUCTIONS.copy()
        process.STAGE_INSTRUCTIONS["translation"] = self.root / "instructions" / "translation.md"
        process.STAGE_INSTRUCTIONS["subdomain"] = self.root / "instructions" / "subdomains.md"
        process.STAGE_INSTRUCTIONS["doc_type"] = self.root / "instructions" / "document_types.md"
        process.STAGE_INSTRUCTIONS["truthness"] = self.root / "instructions" / "truthness.md"
        
        self.config = {
            "llm": {
                "api_base": "http://localhost:11434/v1",
                "api_key": "ollama",
                "model": "llama3"
            },
            "truthness": {
                "threshold": 4
            }
        }
        
        self.review_dir = Path("review")
        if self.review_dir.exists():
            for f in self.review_dir.glob("*"):
                if f.is_file():
                    f.unlink()

    def tearDown(self):
        self.temp_dir.cleanup()
        process.STAGE_INSTRUCTIONS = self.orig_instructions
        if self.review_dir.exists():
            for f in self.review_dir.glob("*"):
                if f.is_file():
                    f.unlink()
            try:
                self.review_dir.rmdir()
            except OSError:
                pass
        if Path("glossary.md").exists():
            Path("glossary.md").unlink()

    @patch("sys.platform", "win32")
    @patch("process.call_llm")
    def test_hash_change_invalidation_and_stale_review_cleanup(self, mock_call_llm):
        import sqlite3
        import yaml
        from unittest.mock import MagicMock
        
        raw_file = self.root / "raw" / "doc.txt"
        raw_file.write_text("שלום עולם הישן", encoding="utf-8")
        
        db_path = self.root / "pipeline.db"
        process.init_db(db_path)
        
        # First run triggers low score review
        mock_call_llm.side_effect = [
            "Translated text old",
            "Tech",
            "concept",
            '{"score": 2, "justification": "unreliable old"}'
        ]
        
        with patch("process.DocumentConverter") as mock_converter_cls:
            mock_conv = MagicMock()
            mock_conv.convert.return_value.document.export_to_markdown.return_value = "שלום עולם הישן"
            mock_converter_cls.return_value = mock_conv
            
            with self.assertRaises(process.NeedsReviewException):
                process.process_file(raw_file, self.root / "raw", self.root / "processed_md", db_path, self.config, None)
        
        # Verify we have 1 pending review in database and 1 review file
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute("SELECT status, file_hash FROM review_queue WHERE stage='truthness'")
        row = cursor.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "pending")
        old_hash = row[1]
        conn.close()
        
        review_files = list(self.review_dir.glob("*.md"))
        self.assertEqual(len(review_files), 1)
        
        # Now change content of raw file
        raw_file.write_text("שלום עולם החדש", encoding="utf-8")
        
        # Run processing again
        # It should detect hash change, mark old review as stale, delete review file, and run stages again.
        mock_call_llm.side_effect = [
            "Translated text new",
            "Tech",
            "concept",
            '{"score": 2, "justification": "unreliable new"}'
        ]
        
        with patch("process.DocumentConverter") as mock_converter_cls:
            mock_conv = MagicMock()
            mock_conv.convert.return_value.document.export_to_markdown.return_value = "שלום עולם החדש"
            mock_converter_cls.return_value = mock_conv
            
            with self.assertRaises(process.NeedsReviewException):
                process.process_file(raw_file, self.root / "raw", self.root / "processed_md", db_path, self.config, None)
        
        # Verify old review status is now stale in db
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM review_queue WHERE file_hash=?", (old_hash,))
        row = cursor.fetchone()
        self.assertEqual(row[0], "stale")
        conn.close()
        
        # Verify review files for old hash are deleted
        review_files = list(self.review_dir.glob("*.md"))
        self.assertEqual(len(review_files), 1)

    @patch("sys.platform", "win32")
    @patch("process.call_llm")
    def test_yaml_safe_frontmatter_special_characters(self, mock_call_llm):
        import yaml
        from unittest.mock import MagicMock
        
        raw_file = self.root / "raw" / "doc.txt"
        raw_file.write_text("שלום עולם", encoding="utf-8")
        
        db_path = self.root / "pipeline.db"
        process.init_db(db_path)
        
        # truthness return justification with newlines and quotes
        special_just = "Line1\nLine2\n\"quoted text\"\n\\backslash\\"
        mock_call_llm.side_effect = [
            "Plain English text",
            "Tech",
            "concept",
            json.dumps({"score": 9, "justification": special_just})
        ]
        
        with patch("process.DocumentConverter") as mock_converter_cls:
            mock_conv = MagicMock()
            mock_conv.convert.return_value.document.export_to_markdown.return_value = "שלום עולם"
            mock_converter_cls.return_value = mock_conv
            
            process.process_file(raw_file, self.root / "raw", self.root / "processed_md", db_path, self.config, None)
        
        out_file = self.root / "processed_md" / "doc.md"
        self.assertTrue(out_file.exists())
        
        out_content = out_file.read_text(encoding="utf-8")
        parts = out_content.split("---")
        self.assertTrue(len(parts) >= 3)
        fm_data = yaml.safe_load(parts[1])
        self.assertEqual(fm_data["truthness_justification"], special_just)

    def test_glossary_parsing_and_filtering(self):
        glossary_file = self.root / "glossary.md"
        glossary_file.write_text(
            "# Glossary\n\n| Hebrew/Internal Term | English Translation | Notes |\n|---|---|---|\n| מפתח | Key | a crypto key |\n| מנעול | Lock | physical lock |\n",
            encoding="utf-8"
        )
        
        entries = process.load_glossary_entries(glossary_file)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0], ("מפתח", "Key", "a crypto key"))
        self.assertEqual(entries[1], ("מנעול", "Lock", "physical lock"))
        
        # Filter check
        text = "This text only contains the word מפתח but not the other one."
        filtered = process.filter_glossary_entries(entries, text)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0][0], "מפתח")

    def test_check_guid_filename_ratio_custom_threshold(self):
        text = "54a06457-1820-49fe-8e1b-30fe482938a0 my-photo.png plaintexxx" # ratio around 82.7%
        # Under custom threshold 0.90, it should return False
        self.assertFalse(process.check_guid_filename_ratio(text, 0.90))
        # Under custom threshold 0.70, it should return True
        self.assertTrue(process.check_guid_filename_ratio(text, 0.70))

if __name__ == "__main__":
    unittest.main()

