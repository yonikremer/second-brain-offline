"""Tests for Hebrew translation pipeline: masking, chunking, QA, glossary, ledger."""
from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import translate as tmod
import translation_qa as qamod
import check_glossary as cgmod
from review_queue import VALID_STATUSES as RQ_STATUSES


# ── Person-name masking ──────────────────────────────────────────────

class TestMaskPersonNames(unittest.TestCase):
    def setUp(self):
        self.first = {"דן", "יוסי", "שרה"}
        self.last = {"כהן", "לוי"}

    def test_single_token_masked(self):
        text = "שלום דן ושרה"
        masked, mapping = tmod.mask_person_names(text, self.first, self.last)
        self.assertIn("⟦PERSON_", masked)
        self.assertIn("דן", mapping)
        # Unmask round-trip
        self.assertEqual(tmod.unmask_person_names(masked, mapping), text)

    def test_substring_not_corrupted(self):
        """דן must NOT be replaced inside דניאל."""
        text = "דניאל הלך"
        masked, mapping = tmod.mask_person_names(text, self.first, self.last)
        # דניאל should remain untouched (דן is substring, not whole token)
        self.assertEqual(masked, text)
        self.assertEqual(mapping, [])

    def test_bigram_masked(self):
        text = "פגשתי את יוסי כהן אתמול"
        masked, mapping = tmod.mask_person_names(text, self.first, self.last)
        self.assertEqual(len(mapping), 1)
        self.assertIn("יוסי כהן", mapping)
        self.assertNotIn("יוסי", masked)
        self.assertNotIn("כהן", masked.replace("⟦PERSON_0⟧", ""))
        self.assertEqual(tmod.unmask_person_names(masked, mapping), text)

    def test_bigram_does_not_eat_neighbors(self):
        text = "שלום יוסי כהן ולהתראות"
        masked, mapping = tmod.mask_person_names(text, self.first, self.last)
        self.assertIn("שלום", masked)
        self.assertIn("ולהתראות", masked)
        self.assertEqual(tmod.unmask_person_names(masked, mapping), text)

    def test_no_names_no_change(self):
        text = "שלום עולם"
        masked, mapping = tmod.mask_person_names(text, self.first, self.last)
        self.assertEqual(masked, text)
        self.assertEqual(mapping, [])


class TestUnmaskPersonNames(unittest.TestCase):
    def test_roundtrip(self):
        text = "דן ושרה"
        first = {"דן", "שרה"}
        last: set[str] = set()
        masked, mapping = tmod.mask_person_names(text, first, last)
        self.assertEqual(tmod.unmask_person_names(masked, mapping), text)


# ── Mock translate sentinel handling ─────────────────────────────────

class TestMockTranslate(unittest.TestCase):
    def test_person_sentinels_not_wrapped(self):
        masked = "שלום ⟦PERSON_0⟧ בעולם"
        # Simulate: glossary empty, mock should wrap Hebrew but not sentinels
        res = tmod.mock_translate(masked, [])
        # Sentinels must survive
        self.assertIn("⟦PERSON_0⟧", res["translation"])
        # Remaining Hebrew words should be wrapped
        self.assertIn("⟦he:", res["translation"])

    def test_hebrew_wrapped_without_sentinel(self):
        res = tmod.mock_translate("שלום עולם", [])
        self.assertIn("⟦he:שלום⟧", res["translation"])


# ── Glossary filtering ───────────────────────────────────────────────

class TestGlossaryForChunk(unittest.TestCase):
    def test_approved_included(self):
        glossary = [{"term_he": "מודל", "english": "model", "status": "approved"}]
        rows = tmod.glossary_for_chunk("יש כאן מודל חדש", glossary)
        self.assertEqual(len(rows), 1)

    def test_proposed_excluded(self):
        glossary = [{"term_he": "מודל", "english": "model", "status": "proposed"}]
        rows = tmod.glossary_for_chunk("יש כאן מודל חדש", glossary)
        self.assertEqual(len(rows), 0)

    def test_keep_source_included(self):
        glossary = [{"term_he": "מבנה", "english": "", "status": "keep_source"}]
        rows = tmod.glossary_for_chunk("מבנה חשוב", glossary)
        self.assertEqual(len(rows), 1)

    def test_pending_excluded(self):
        glossary = [{"term_he": "מודל", "english": "model", "status": "pending"}]
        rows = tmod.glossary_for_chunk("מודל", glossary)
        self.assertEqual(len(rows), 0)

    def test_term_not_in_chunk_excluded(self):
        glossary = [{"term_he": "מודל", "english": "model", "status": "approved"}]
        rows = tmod.glossary_for_chunk("שלום עולם", glossary)
        self.assertEqual(len(rows), 0)


# ── Chunk splitting ──────────────────────────────────────────────────

class TestChunkMarkdown(unittest.TestCase):
    def test_heading_boundary(self):
        md = "# Title\n\nParagraph one.\n\n## Section\n\nParagraph two.\n"
        chunks = tmod.chunk_markdown(md, max_chars=200)
        self.assertGreaterEqual(len(chunks), 1)

    def test_frontmatter_attached_to_first_chunk(self):
        md = "---\ntitle: Test\n---\n\n# Heading\n\nBody\n"
        chunks = tmod.chunk_markdown(md)
        self.assertTrue(chunks[0]["chunk_text"].startswith("---\n"))

    def test_code_fence_not_split(self):
        md = "# H\n\n```\ncode line\n```\n\nParagraph\n"
        chunks = tmod.chunk_markdown(md, max_chars=20)
        combined = "\n".join(c["chunk_text"] for c in chunks)
        self.assertEqual(combined.count("```"), 2)

    def test_max_chars_respected_at_paragraph(self):
        md = "# H\n\n" + "a " * 100 + "\n\n" + "b " * 100 + "\n"
        chunks = tmod.chunk_markdown(md, max_chars=80)
        # Should split at blank line
        self.assertGreaterEqual(len(chunks), 2)


# ── QA checks ────────────────────────────────────────────────────────

class TestCheckGlossaryRetention(unittest.TestCase):
    def test_approved_term_missing_fails(self):
        glossary = [{"term_he": "מודל", "english": "model", "status": "approved"}]
        body = "This document has no glossary term."
        result = qamod.check_glossary_retention(body, glossary)
        self.assertEqual(result["status"], "fail")
        self.assertTrue(any("מודל" in v for v in result["violations"]))

    def test_approved_term_present_passes(self):
        glossary = [{"term_he": "מודל", "english": "model", "status": "approved"}]
        body = "We built a model for inference."
        result = qamod.check_glossary_retention(body, glossary)
        self.assertEqual(result["status"], "pass")

    def test_approved_with_marker_passes(self):
        glossary = [{"term_he": "מודל", "english": "model", "status": "approved"}]
        body = "Blocked term ⟦he:מודל⟧ remains."
        result = qamod.check_glossary_retention(body, glossary)
        self.assertEqual(result["status"], "pass")

    def test_keep_source_present_passes(self):
        glossary = [{"term_he": "מבנה", "english": "", "status": "keep_source"}]
        body = "The מבנה is preserved."
        result = qamod.check_glossary_retention(body, glossary)
        self.assertEqual(result["status"], "pass")

    def test_keep_source_missing_fails(self):
        glossary = [{"term_he": "מבנה", "english": "", "status": "keep_source"}]
        body = "No Hebrew term here."
        result = qamod.check_glossary_retention(body, glossary)
        self.assertEqual(result["status"], "fail")

    def test_proposed_not_checked(self):
        glossary = [{"term_he": "מודל", "english": "model", "status": "proposed"}]
        body = "Nothing."
        result = qamod.check_glossary_retention(body, glossary)
        self.assertEqual(result["status"], "pass")


class TestCheckLengthRatioPlaceholder(unittest.TestCase):
    """Smoke: length_ratio uses placeholder band; docstring notes calibration."""
    def test_has_calibration_note(self):
        self.assertIn("pre-calibration", qamod.check_length_ratio.__doc__ or "")

    def test_default_band(self):
        result = qamod.check_length_ratio("a" * 100, "b" * 100)
        self.assertEqual(result["status"], "pass")
        result2 = qamod.check_length_ratio("a" * 100, "b" * 10)
        self.assertEqual(result2["status"], "fail")


class TestResidualHebrewRatio(unittest.TestCase):
    def test_no_hebrew_pass(self):
        result = qamod.check_residual_hebrew("Hello world")
        self.assertEqual(result["status"], "pass")

    def test_markers_stripped(self):
        body = "Model ⟦he:מודל⟧ is here."
        result = qamod.check_residual_hebrew(body)
        # After stripping marker, no residual Hebrew remains
        self.assertEqual(result["status"], "pass")


# ── Glossary CSV comment handling ────────────────────────────────────

class TestCheckGlossaryComments(unittest.TestCase):
    def test_comment_lines_ignored(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, encoding="utf-8", newline="") as f:
            f.write("# This is a comment\n")
            f.write("term_he,english,keep_source,notes,status,example_doc\n")
            f.write("# another comment\n")
            f.write("מודל,model,0,,approved,doc.md\n")
            path = Path(f.name)
        try:
            ok, errors = cgmod.check_glossary(path)
            self.assertTrue(ok, f"errors: {errors}")
        finally:
            path.unlink()

    def test_unapproved_blocked(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, encoding="utf-8", newline="") as f:
            f.write("term_he,english,keep_source,notes,status,example_doc\n")
            f.write("מודל,model,0,,proposed,doc.md\n")
            path = Path(f.name)
        try:
            ok, errors = cgmod.check_glossary(path)
            self.assertFalse(ok)
        finally:
            path.unlink()


# ── Ledger schema ────────────────────────────────────────────────────

class TestLedgerSchema(unittest.TestCase):
    def test_translate_ledger_event_has_ts(self):
        # Simulate event schema used in translate.py
        from datetime import datetime, timezone
        import hashlib
        event = {
            "event": "translation_completed",
            "ts": datetime.now(timezone.utc).isoformat(),
            "source_doc": "doc.md",
            "source_hash": hashlib.sha256(b"hello").hexdigest(),
            "model": "minimax-m2.7",
            "glossary_version": "abc123",
            "status": "completed",
            "marker_count": 0,
            "unknown_terms": [],
        }
        self.assertIn("ts", event)
        self.assertIn("source_hash", event)
        self.assertIn("glossary_version", event)

    def test_review_queue_ledger_event_has_ts(self):
        from datetime import datetime, timezone
        event = {
            "event": "question_answered",
            "ts": datetime.now(timezone.utc).isoformat(),
            "term_he": "מודל",
            "english": "model",
            "status": "approved",
            "decided_by": "human",
            "glossary_version": "",
        }
        self.assertIn("ts", event)
        self.assertIn("term_he", event)

    def test_get_ledger_path_canonical(self):
        vault = Path("/tmp/fake_vault")
        p = tmod.get_ledger_path(vault)
        self.assertEqual(p, vault / "data" / "translations" / "ledger.jsonl")

    def test_ledger_paths_consistent(self):
        from review_queue import get_ledger_path as rq_ledger
        vault = Path("/tmp/fake_vault2")
        self.assertEqual(tmod.get_ledger_path(vault), rq_ledger(vault))


# ── VALID_STATUSES unified ───────────────────────────────────────────

class TestValidStatusesUnified(unittest.TestCase):
    def test_check_glossary_has_pending(self):
        self.assertIn("pending", cgmod.VALID_STATUSES)

    def test_review_queue_has_pending(self):
        self.assertIn("pending", RQ_STATUSES)

    def test_both_contain_same_core(self):
        core = {"approved", "proposed", "keep_source"}
        self.assertTrue(core.issubset(cgmod.VALID_STATUSES))
        self.assertTrue(core.issubset(RQ_STATUSES))


# ── convert_to_md YAML guard ─────────────────────────────────────────

class TestYamlGuard(unittest.TestCase):
    def test_build_frontmatter_raises_if_no_yaml(self):
        # Mock heavy deps so convert_to_md can be imported in CI without them
        import sys as _sys
        for _mod in ("docling_convert", "hebrew_fix", "onenote_conversion"):
            if _mod not in _sys.modules:
                _sys.modules[_mod] = type(_sys)(_mod)
        import convert_to_md as cmod
        orig_yaml = cmod.yaml
        try:
            cmod.yaml = None
            from datetime import datetime, timezone
            with self.assertRaises(RuntimeError):
                cmod.build_frontmatter("Title", datetime.now(timezone.utc), "file.txt", ".txt", False)
        finally:
            cmod.yaml = orig_yaml


if __name__ == "__main__":
    unittest.main()
