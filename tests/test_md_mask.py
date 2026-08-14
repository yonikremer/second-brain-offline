"""Tests for md_mask placeholder masking."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import re
import unittest


class TestPlaceholderVocabulary(unittest.TestCase):
    def test_placeholder_pattern_covers_all_types(self):
        import md_mask

        for name in [
            "FRONTMATTER",
            "MULTILINE_CODE",
            "TABLE",
            "TABLE_CELL",
            "CODE",
            "LATEX_BLOCK",
            "LATEX_INLINE",
            "LINK_PRE",
            "LINK_SUF",
            "LINK",
            "HEADING",
            "LIST",
            "BLOCKQUOTE",
            "HTML",
            "WIKILINK",
        ]:
            self.assertIn(name, md_mask.PLACEHOLDER_PATTERN)

    def test_split_regex_captures_token(self):
        import md_mask

        parts = md_mask.PLACEHOLDER_SPLIT_RE.split("a<<<CODE_100>>>b")
        self.assertIn("<<<CODE_100>>>", parts)

    def test_replace_regex_matches_all(self):
        import md_mask

        text = "<<<FRONTMATTER_100>>> and <<<TABLE_CELL_101>>>"
        hits = md_mask.PLACEHOLDER_REPLACE_RE.findall(text)
        self.assertEqual(len(hits), 2)

    def test_counter_seed_avoids_collision(self):
        import md_mask

        lines = ["hello <<<CODE_105>>> world", "next"]
        seed = md_mask.compute_counter_seed(lines)
        self.assertGreaterEqual(seed, 106)
        self.assertEqual(md_mask.compute_counter_seed(["hello world"]), 100)


class TestBlockMasks(unittest.TestCase):
    def test_frontmatter_masked(self):
        import md_mask

        lines = ["---", "title: Test", "---", "", "# Heading", "Body"]
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        joined = "\n".join(res.content_lines)
        self.assertIn("<<<FRONTMATTER_", joined)
        self.assertIn("FRONTMATTER_", str(res.maps["frontmatter"]))

    def test_frontmatter_hr_not_masked(self):
        import md_mask

        lines = ["---", "", "Paragraph after HR", "", "---", "more"]
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        self.assertNotIn("FRONTMATTER_", "\n".join(res.content_lines))

    def test_fenced_code_masked(self):
        import md_mask

        lines = ["# H", "```python", "print('hi')", "```", "paragraph"]
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        joined = "\n".join(res.content_lines)
        self.assertIn("<<<MULTILINE_CODE_", joined)
        self.assertNotIn("print('hi')", joined)

    def test_blockquote_fenced_code_masked(self):
        import md_mask

        lines = ["> ```js", "> console.log(1)", "> ```", "after"]
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        joined = "\n".join(res.content_lines)
        self.assertIn("<<<MULTILINE_CODE_", joined)
        self.assertNotIn("console.log", joined)

    def test_html_comment_masked(self):
        import md_mask

        lines = ["text <!-- comment", "spanning --> more"]
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        joined = "\n".join(res.content_lines)
        self.assertIn("<<<HTML_", joined)
        self.assertNotIn("comment", joined)

    def test_latex_block_masked(self):
        import md_mask

        opts = md_mask.MdOptions(translate_latex=False)
        lines = ["text $$", "E=mc^2", "$$ more"]
        res = md_mask.filter_markdown_lines(lines, opts)
        joined = "\n".join(res.content_lines)
        self.assertIn("<<<LATEX_BLOCK_", joined)

    def test_latex_block_not_across_heading(self):
        import md_mask

        opts = md_mask.MdOptions(translate_latex=False)
        lines = ["$$ block one $$", "# Heading", "$$ block two $$"]
        res = md_mask.filter_markdown_lines(lines, opts)
        joined = "\n".join(res.content_lines)
        # Heading must not be swallowed into LaTeX — it should be separate (as HEADING placeholder)
        self.assertIn("<<<HEADING_", joined)
        self.assertIn("<<<LATEX_BLOCK_", joined)
        # LaTeX blocks must be two separate placeholders, not one spanning heading
        self.assertEqual(joined.count("<<<LATEX_BLOCK_"), 2)


class TestInlineMasks(unittest.TestCase):
    def test_inline_code_masked(self):
        import md_mask

        lines = ["Use `code --flag` here"]
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        joined = "\n".join(res.content_lines)
        self.assertIn("<<<CODE_", joined)
        self.assertNotIn("code --flag", joined)

    def test_inline_code_unpaired_kept(self):
        import md_mask

        lines = ["Unpaired `code here"]
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        self.assertIn("`code", "\n".join(res.content_lines))

    def test_inline_latex_masked(self):
        import md_mask

        lines = ["Formula $E=mc^2$ done"]
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions(translate_latex=False))
        self.assertIn("<<<LATEX_INLINE_", "\n".join(res.content_lines))

    def test_currency_not_masked_as_latex(self):
        import md_mask

        lines = ["Price $100 and $50 total"]
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions(translate_latex=False))
        self.assertNotIn("LATEX_INLINE_", "\n".join(res.content_lines))
        self.assertIn("$100", "\n".join(res.content_lines))

    def test_wikilink_masked(self):
        import md_mask

        lines = ["See [[My Note]] and ![[embed.png]]"]
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        joined = "\n".join(res.content_lines)
        self.assertIn("<<<WIKILINK_", joined)
        self.assertNotIn("[[My Note]]", joined)

    def test_wikilink_alias_stays_with_target(self):
        import md_mask

        lines = ["Link [[target|alias text]] here"]
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        joined = "\n".join(res.content_lines)
        self.assertIn("<<<WIKILINK_", joined)
        self.assertNotIn("alias text", joined)

    def test_image_split(self):
        import md_mask

        lines = ["![alt text](path/to/img.png)"]
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        joined = "\n".join(res.content_lines)
        self.assertIn("<<<LINK_PRE_", joined)
        self.assertIn("<<<LINK_SUF_", joined)
        self.assertIn("alt text", joined)

    def test_heading_prefix_masked(self):
        import md_mask

        lines = ["## Section Title"]
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        joined = "\n".join(res.content_lines)
        self.assertIn("<<<HEADING_", joined)
        self.assertIn("Section Title", joined)

    def test_list_prefix_masked(self):
        import md_mask

        lines = ["- item one", "1. numbered"]
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        joined = "\n".join(res.content_lines)
        self.assertEqual(joined.count("<<<LIST_"), 2)

    def test_html_tag_masked(self):
        import md_mask

        lines = ['Text <span class="x">hi</span> more']
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        joined = "\n".join(res.content_lines)
        self.assertIn("<<<HTML_", joined)
        self.assertNotIn("<span", joined)


class TestTableMasks(unittest.TestCase):
    def test_simple_table_cells_extracted(self):
        import md_mask

        lines = [
            "| Hebrew | English | Notes |",
            "|---|---|---|",
            "| שלום | hello | greeting |",
            "| תודה | thanks | polite |",
            "after table",
        ]
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        cells = md_mask.extract_table_cells(lines)
        # 3 header + 3 row1 + 3 row2 = 9 total; but test checks at least 6 data cells
        self.assertGreaterEqual(len(cells), 6)
        joined = "\n".join(res.content_lines)
        self.assertIn("<<<TABLE_CELL_", joined)
        self.assertNotIn("שלום", joined)

    def test_table_separator_not_translated(self):
        import md_mask

        lines = ["| A | B |", "|---|---|", "| x | y |"]
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        joined = "\n".join(res.content_lines)
        self.assertIn("<<<TABLE_", joined)
        segs = md_mask.split_markdown_segments(res.content_lines, res.source_line_numbers)
        for t in segs.texts_to_translate:
            self.assertNotIn("---", t)

    def test_pipe_inside_inline_code_not_split(self):
        import md_mask

        lines = ["| `a|b` | code |", "|---|---|", "| `x|y` | z |"]
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        # Inline code `a|b` is masked to <<<CODE>>> before table split,
        # so the pipe inside code must not create an extra column.
        # CODE placeholder lives inside TABLE_CELL values, not directly in rows.
        all_cell_values = " ".join(res.maps["table_cell"].values())
        self.assertIn("<<<CODE_", all_cell_values)
        # Table must still have correct column count (2 columns per row)
        self.assertEqual(len(res.maps["table_cell"]), 4)  # 2 header + 2*2 data cells

    def test_table_roundtrip(self):
        import md_mask

        lines = [
            "| H1 | H2 |",
            "|---|---|",
            "| שלום | world |",
            "| foo | בר |",
        ]
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        restored = md_mask.restore_placeholders("\n".join(res.content_lines), res.maps)
        self.assertEqual(restored.strip(), "\n".join(lines).strip())

    def test_table_cell_pipe_escaped(self):
        import md_mask

        lines = ["| a \\| b | c |", "|---|---|", "| d | e |"]
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        joined = "\n".join(res.content_lines)
        self.assertIn("<<<TABLE_CELL_", joined)

    def test_no_table_no_cells(self):
        import md_mask

        lines = ["# Heading", "No table here | just a pipe"]
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        self.assertNotIn("TABLE_CELL_", "\n".join(res.content_lines))
        self.assertEqual(md_mask.extract_table_cells(lines), [])

    def test_alignment_colons_preserved(self):
        import md_mask

        lines = ["| Left | Center | Right |", "|:---|:---:|---:|", "| a | b | c |"]
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        restored = md_mask.restore_placeholders("\n".join(res.content_lines), res.maps)
        self.assertIn("|:---|:---:|---:|", restored)


class TestSegmentation(unittest.TestCase):
    def test_split_segments(self):
        import md_mask

        lines = ["Hello `code` world", "Second line"]
        filt = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        segs = md_mask.split_markdown_segments(filt.content_lines, filt.source_line_numbers)
        self.assertGreater(len(segs.texts_to_translate), 0)
        for t in segs.texts_to_translate:
            self.assertNotIn("<<<CODE_", t)

    def test_merge_roundtrip(self):
        import md_mask

        lines = ["# Title", "Paragraph with `code` and $x$ formula."]
        filt = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        segs = md_mask.split_markdown_segments(filt.content_lines, filt.source_line_numbers)
        merged = md_mask.merge_markdown_segments(segs.line_segments, segs.texts_to_translate)
        restored = md_mask.restore_placeholders("\n".join(merged), filt.maps)
        self.assertEqual(restored.strip(), "\n".join(lines).strip())

    def test_restore_nested_placeholders(self):
        import md_mask

        maps = {
            "frontmatter": {},
            "code": {"<<<CODE_100>>>": "`x`"},
            "html": {"<<<HTML_101>>>": "<span><<<CODE_100>>></span>"},
            "latex_block": {},
            "latex_inline": {},
            "link": {},
            "heading": {},
            "list": {},
            "blockquote": {},
            "wikilink": {},
            "table": {},
            "table_cell": {},
        }
        out = md_mask.restore_placeholders("a <<<HTML_101>>> b", maps)
        self.assertEqual(out, "a <span>`x`</span> b")

    def test_remove_chars_skips_placeholders(self):
        import md_mask

        text = "hello-<<<CODE_100>>>-world"
        out = md_mask.apply_remove_chars_to_markdown(text, "-")
        self.assertIn("<<<CODE_100>>>", out)
        self.assertNotIn("-", out.replace("<<<CODE_100>>>", ""))

    def test_source_line_numbers_with_table(self):
        import md_mask

        lines = ["# H", "| A | B |", "|---|---|", "| x | y |", "after"]
        filt = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        self.assertEqual(filt.source_line_numbers[0], 1)
        self.assertTrue(all(b >= a for a, b in zip(filt.source_line_numbers, filt.source_line_numbers[1:])))

    def test_table_cell_segments_not_in_text(self):
        import md_mask

        lines = ["| שלום | hello |", "|---|---|", "| תודה | thanks |"]
        filt = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        segs = md_mask.split_markdown_segments(filt.content_lines, filt.source_line_numbers)
        # Table cells are in maps, not in texts_to_translate directly
        # The row line contains TABLE_CELL placeholders, not raw Hebrew
        joined = "\n".join(filt.content_lines)
        self.assertIn("<<<TABLE_CELL_", joined)
        # Cell map holds the Hebrew
        cell_texts = md_mask.get_table_cell_texts(filt.maps)
        self.assertTrue(any("שלום" in c for c in cell_texts))


class TestTranslateIntegration(unittest.TestCase):
    def test_mask_translate_restore_table_identity(self):
        import md_mask

        md_text = "| Hebrew | English |\n|---|---|\n| שלום | hello |\n| תודה | thanks |\n"
        filt = md_mask.filter_markdown_lines(md_text.split("\n"), md_mask.MdOptions())
        segs = md_mask.split_markdown_segments(filt.content_lines, filt.source_line_numbers)
        Hebrew_WORD = re.compile(r"[א-ת]{2,}")
        mocked = [Hebrew_WORD.sub(lambda m: f"⟦he:{m.group(0)}⟧", t) for t in segs.texts_to_translate]
        cell_texts = md_mask.get_table_cell_texts(filt.maps)
        mocked_cells = [Hebrew_WORD.sub(lambda m: f"⟦he:{m.group(0)}⟧", c) for c in cell_texts]
        md_mask.inject_translated_table_cells(filt.maps, mocked_cells)
        merged = md_mask.merge_markdown_segments(segs.line_segments, mocked)
        restored = md_mask.restore_placeholders("\n".join(merged), filt.maps)
        self.assertEqual(restored.count("|"), md_text.count("|"))
        self.assertIn("|---|---|", restored)
        self.assertIn("hello", restored)

    def test_invariants_still_verified_after_mask(self):
        import md_mask

        chunk = "See [[My Note]] and ![alt](http://example.com/img.png) and `code|pipe`"
        filt = md_mask.filter_markdown_lines(chunk.split("\n"), md_mask.MdOptions())
        segs = md_mask.split_markdown_segments(filt.content_lines, filt.source_line_numbers)
        merged = md_mask.merge_markdown_segments(segs.line_segments, segs.texts_to_translate)
        restored = md_mask.restore_placeholders("\n".join(merged), filt.maps)
        self.assertIn("[[My Note]]", restored)
        self.assertIn("http://example.com/img.png", restored)
        self.assertIn("`code|pipe`", restored)


class TestTableQA(unittest.TestCase):
    def test_column_count_mismatch_fails(self):
        import translation_qa as qa

        src = "| A | B | C |\n|---|---|---|\n| 1 | 2 | 3 |\n"
        trans = "| A | B |\n|---|---|\n| 1 | 2 |\n"
        result = qa.check_table_fidelity(src, trans)
        self.assertEqual(result["status"], "fail")

    def test_separator_lost_fails(self):
        import translation_qa as qa

        src = "| A | B |\n|---|---|\n| 1 | 2 |\n"
        trans = "| A | B |\n| 1 | 2 |\n"
        result = qa.check_table_fidelity(src, trans)
        self.assertEqual(result["status"], "fail")

    def test_pipe_in_cell_causes_column_drift(self):
        import translation_qa as qa

        src = "| A | B |\n|---|---|\n| hello | world |\n"
        trans = "| A | B |\n|---|---|\n| hel|lo | world |\n"
        result = qa.check_table_fidelity(src, trans)
        self.assertEqual(result["status"], "fail")

    def test_correct_table_passes(self):
        import translation_qa as qa

        src = "| A | B |\n|---|---|\n| 1 | 2 |\n"
        trans = "| A | B |\n|---|---|\n| one | two |\n"
        result = qa.check_table_fidelity(src, trans)
        self.assertEqual(result["status"], "pass")

    def test_alignment_colons_preserved(self):
        import translation_qa as qa

        src = "| A | B | C |\n|:---|:---:|---:|\n| 1 | 2 | 3 |\n"
        trans = "| A | B | C |\n|---|---|---|\n| 1 | 2 | 3 |\n"
        result = qa.check_table_fidelity(src, trans)
        self.assertEqual(result["status"], "fail")


class TestGoldenFiles(unittest.TestCase):
    def _roundtrip(self, path: Path):
        import md_mask

        lines = path.read_text(encoding="utf-8").split("\n")
        filt = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        segs = md_mask.split_markdown_segments(filt.content_lines, filt.source_line_numbers)
        merged = md_mask.merge_markdown_segments(segs.line_segments, segs.texts_to_translate)
        restored = md_mask.restore_placeholders("\n".join(merged), filt.maps)
        self.assertEqual(restored, "\n".join(lines))

    def test_golden_mixed(self):
        self._roundtrip(Path("tests/fixtures/md_mask/golden_mixed.md"))

    def test_golden_tables_basic(self):
        self._roundtrip(Path("tests/fixtures/md_mask/tables_basic.md"))

    def test_golden_hebrew_table(self):
        self._roundtrip(Path("tests/fixtures/md_mask/golden_hebrew_table.md"))

    def test_golden_prose_pipe(self):
        self._roundtrip(Path("tests/fixtures/md_mask/golden_prose_pipe.md"))

    def test_outerless_table(self):
        import md_mask

        # GFM table without outer pipes must still be detected
        lines = ["Hebrew | English", "---|---", "שלום | hello"]
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        self.assertIn("<<<TABLE_CELL_", "\n".join(res.content_lines))
        restored = md_mask.restore_placeholders("\n".join(res.content_lines), res.maps)
        # Outer-pipe canonicalization is intentional (see review issue #2, skipped)
        # — content must survive, pipes may be normalized to outer-pipe form
        self.assertIn("Hebrew", restored)
        self.assertIn("English", restored)
        self.assertIn("שלום", restored)
        self.assertIn("hello", restored)
        # QA must see it as a table
        import translation_qa as qa

        self.assertEqual(qa.check_table_fidelity("\n".join(lines), restored)["status"], "pass")

    def test_prose_pipe_no_table(self):
        import md_mask

        lines = ["This is prose with a | pipe but no separator after."]
        res = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        self.assertNotIn("TABLE_CELL_", "\n".join(res.content_lines))
        self.assertNotIn("TABLE_", "\n".join(res.content_lines))


class TestRemoveCharsOrdering(unittest.TestCase):
    def test_remove_chars_before_restore(self):
        import md_mask

        lines = ["Hello `code-with-dash` world", "| A | B |", "|---|---|", "| x | y |"]
        filt = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        segs = md_mask.split_markdown_segments(filt.content_lines, filt.source_line_numbers)
        merged = md_mask.merge_markdown_segments(segs.line_segments, segs.texts_to_translate)
        cleaned = [md_mask.apply_remove_chars_to_markdown(ln, "-") for ln in merged]
        restored = md_mask.restore_placeholders("\n".join(cleaned), filt.maps)
        # Code content must survive cleaning because it was inside a CODE placeholder
        self.assertIn("code-with-dash", restored)

    def test_wikilink_hebrew_table(self):
        import md_mask

        lines = ["| [[Note|alias]] | תודה |", "|---|---|", "| [[Link]] | hello |"]
        filt = md_mask.filter_markdown_lines(lines, md_mask.MdOptions())
        # Wikilinks inside table cells are masked before table split,
        # so they live inside TABLE_CELL values
        all_cell_values = " ".join(filt.maps["table_cell"].values())
        self.assertIn("<<<WIKILINK_", all_cell_values)


if __name__ == "__main__":
    unittest.main()
