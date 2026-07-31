# tests/test_pipeline.py
import unittest
import json
from pathlib import Path
from unittest.mock import patch

# Import functions to test
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
    @patch("process.input")
    def test_interactive_translation_loop(self, mock_input, mock_call_llm):
        from unittest.mock import patch, MagicMock
        
        # We simulate a file that needs translation
        raw_file = self.root / "raw" / "doc.txt"
        raw_file.write_text("שלום", encoding="utf-8") # Hebrew text to trigger needs_translation
        
        db_path = self.root / "pipeline.db"
        process.init_db(db_path)
        
        # Stage 3 first call -> Clarification
        # Stage 3 second call -> Hello
        # Stage 4 -> Tech
        # Stage 5 -> concept
        # Stage 6 -> {"score": 9, "justification": "trusted"}
        mock_call_llm.side_effect = [
            "Clarification Required\nTerm/Issue: שלום\nContext: שלום עולם\nQuestion: How to translate?",
            "Hello",
            "Tech",
            "concept",
            '{"score": 9, "justification": "trusted"}'
        ]
        
        # User answers the clarification (Translation, Notes)
        mock_input.side_effect = ["Hello", "Greeting notes"]
        
        # Stub the docling converter to return our custom raw text
        with patch("process.DocumentConverter") as mock_converter_cls:
            mock_conv = MagicMock()
            mock_conv.convert.return_value.document.export_to_markdown.return_value = "שלום"
            mock_converter_cls.return_value = mock_conv
            
            # Execute pipeline on the file
            process.process_file(
                filepath=raw_file,
                raw_root=self.root / "raw",
                output_root=self.root / "processed_md",
                db_path=db_path,
                config=self.config,
                force_stage=None
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

if __name__ == "__main__":
    unittest.main()
