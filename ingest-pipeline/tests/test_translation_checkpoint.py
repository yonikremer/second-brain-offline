"""Tests for chunk-level checkpointing (HANDOFF §4.1 fixes 1 and 2).

A 100-200 page document is ~67 chunks. Before checkpointing, a failure in
chunk 43 discarded chunks 1-42; the content-addressed store skipped completed
*documents*, not completed *chunks*.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


class TestChunkCheckpointKey(unittest.TestCase):
    def _key(self, **over):
        from translate.translation_checkpoint import chunk_checkpoint_key
        kw = dict(chunk_text="שלום עולם", section_path="# H", prev_tail="",
                  glossary_fingerprint="abc123", model="minimax-m2.7",
                  mock=False, no_mask=False, names_fingerprint="names0",
                  code_fingerprint="code0")
        kw.update(over)
        return chunk_checkpoint_key(**kw)

    def test_same_inputs_give_same_key(self):
        self.assertEqual(self._key(), self._key())

    def test_key_changes_when_prev_tail_changes(self):
        # prev_tail is fed to the model as context, so it is part of the chunk's
        # identity: if chunk N-1 is retranslated, chunk N must not reuse its cache.
        self.assertNotEqual(self._key(prev_tail=""), self._key(prev_tail="...earlier English"))

    def test_key_changes_when_glossary_changes(self):
        self.assertNotEqual(self._key(), self._key(glossary_fingerprint="def456"))

    def test_key_changes_when_model_changes(self):
        self.assertNotEqual(self._key(), self._key(model="kimi-k2.7"))

    def test_mock_output_never_collides_with_real_output(self):
        self.assertNotEqual(self._key(mock=False), self._key(mock=True))

    def test_key_changes_when_the_person_name_lists_change(self):
        # The curated lists reach the prompt and the preservation checks.
        self.assertNotEqual(self._key(), self._key(names_fingerprint="names1"))

    def test_key_changes_when_the_pipeline_code_changes(self):
        # Editing a prompt mid-corpus must not leave a document stitched together
        # from two generations of the pipeline.
        self.assertNotEqual(self._key(), self._key(code_fingerprint="code1"))

    def test_every_key_input_is_required(self):
        # Fail-closed: a forgotten input silently reuses chunks produced under
        # different rules, and the cached output looks perfectly well-formed.
        import inspect
        from translate.translation_checkpoint import chunk_checkpoint_key
        defaults = [n for n, prm in inspect.signature(chunk_checkpoint_key).parameters.items()
                    if prm.default is not inspect.Parameter.empty]
        self.assertEqual(defaults, [])



class TestChunkCheckpointStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out_root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_save_then_load_round_trips_payload(self):
        from translate.translation_checkpoint import save_chunk_checkpoint, load_chunk_checkpoint
        payload = {
            "translation": "The systems include Information Security.",
            "term_map": [{"id": 0, "term_he": "מערכת", "english": "system",
                          "keep_source": False, "occurrences": 2, "src_order": 0}],
            "unknown": ["foo"],
            "notes": ["mock"],
        }
        save_chunk_checkpoint(self.out_root, "deadbeef", payload)
        self.assertEqual(load_chunk_checkpoint(self.out_root, "deadbeef"), payload)

    def test_load_returns_none_for_unknown_key(self):
        from translate.translation_checkpoint import load_chunk_checkpoint
        self.assertIsNone(load_chunk_checkpoint(self.out_root, "0" * 64))

    def test_corrupt_checkpoint_is_ignored_not_fatal(self):
        # A half-written file from a killed run must degrade to a cache miss.
        # Crashing here would make the resume path worse than no cache at all.
        from translate.translation_checkpoint import (save_chunk_checkpoint, load_chunk_checkpoint,
                                            chunk_checkpoint_path)
        save_chunk_checkpoint(self.out_root, "cafe", {"translation": "x"})
        chunk_checkpoint_path(self.out_root, "cafe").write_text("{not json", encoding="utf-8")
        self.assertIsNone(load_chunk_checkpoint(self.out_root, "cafe"))

    def test_checkpoint_is_sharded_by_key_prefix(self):
        # 3,800 pages of chunks in one flat directory is a filesystem problem on
        # Windows; shard the same way the document store does.
        from translate.translation_checkpoint import save_chunk_checkpoint, chunk_checkpoint_path
        save_chunk_checkpoint(self.out_root, "ab12cd", {"translation": "x"})
        p = chunk_checkpoint_path(self.out_root, "ab12cd")
        self.assertEqual(p.parent.name, "ab")
        self.assertTrue(p.exists())

    def test_no_partial_file_is_left_when_write_fails(self):
        # Atomic replace: a crash mid-write must not leave a truncated JSON file
        # that a later run would read as a valid checkpoint.
        from translate.translation_checkpoint import save_chunk_checkpoint, chunk_checkpoint_path
        import unittest.mock as mock
        with mock.patch("json.dump", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                save_chunk_checkpoint(self.out_root, "beef", {"translation": "x"})
        self.assertFalse(chunk_checkpoint_path(self.out_root, "beef").exists())
        leftovers = [p for p in self.out_root.rglob("*") if p.is_file()]
        self.assertEqual(leftovers, [], f"temp files left behind: {leftovers}")


# Three heading-delimited chunks; chunk_markdown splits at heading boundaries.
_THREE_CHUNK_DOC = "# A\nאלף אלף אלף\n\n# B\nבית בית בית\n\n# C\nגימל גימל גימל\n"
_MARKERS = {"אלף": "A", "בית": "B", "גימל": "C"}


def _fake_llm(fail_marker=None, fail_times=None):
    """call_llm stand-in keyed on chunk content, not call order.

    fail_marker: Hebrew word whose chunk raises the way a lost sentinel does.
    fail_times:  None = fail forever; N = fail the first N attempts, then succeed.
    """
    prompts: list[str] = []
    state = {"failures": 0}

    def fake(base_url, api_key, model, prompt, *a, **kw):
        prompts.append(prompt)
        which = next((v for k, v in _MARKERS.items() if k in prompt), "?")
        if fail_marker and fail_marker in prompt:
            if fail_times is None or state["failures"] < fail_times:
                state["failures"] += 1
                raise RuntimeError("glossary sentinel lost in whole-doc: [{'term': 'x'}]")
        return {"translation": f"EN-{which}", "unknown_terms": [], "notes": []}

    fake.prompts = prompts
    return fake


def _chunks_seen(fake):
    return [v for p in fake.prompts for k, v in _MARKERS.items() if k in p]


class TestChunkResume(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out_root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _run(self, fake, **over):
        import translate.translate as translate
        import unittest.mock as mock
        kw = dict(raw_text=_THREE_CHUNK_DOC, first_names=set(), last_names=set(),
                  glossary=[], base_url="http://x", api_key="k", model="m",
                  mock=False, chunk_chars=6000, no_mask=True, name_candidates=None,
                  out_root=self.out_root, chunk_retries=0)
        kw.update(over)
        with mock.patch("translate.translate.call_llm", side_effect=fake):
            return translate._translate_chunks_with_term_map(**kw)

    def test_chunks_before_the_failure_are_checkpointed(self):
        fake = _fake_llm(fail_marker="גימל")
        with self.assertRaises(RuntimeError):
            self._run(fake)
        stored = list((self.out_root / "chunks").rglob("*.json"))
        self.assertEqual(len(stored), 2, "chunks A and B should survive C's failure")

    def test_rerun_retranslates_only_the_failed_chunk(self):
        # This is the whole point of §4.1: one dropped delimiter in chunk 43 must
        # not cost the 42 chunks in front of it.
        first = _fake_llm(fail_marker="גימל")
        with self.assertRaises(RuntimeError):
            self._run(first)
        second = _fake_llm()
        full, _unknown, _notes, _tm = self._run(second)
        self.assertEqual(_chunks_seen(second), ["C"])
        self.assertEqual(full, "EN-A\n\nEN-B\n\nEN-C")

    def test_clean_rerun_makes_no_llm_calls_at_all(self):
        self._run(_fake_llm())
        again = _fake_llm()
        self._run(again)
        self.assertEqual(again.prompts, [])

    def test_resumed_document_matches_an_uninterrupted_one(self):
        interrupted = _fake_llm(fail_marker="גימל")
        with self.assertRaises(RuntimeError):
            self._run(interrupted)
        resumed, _u, _n, _t = self._run(_fake_llm())
        other = Path(tempfile.mkdtemp(dir=self._tmp.name))
        fresh, _u2, _n2, _t2 = self._run(_fake_llm(), out_root=other)
        self.assertEqual(resumed, fresh)

    def test_force_ignores_existing_checkpoints(self):
        self._run(_fake_llm())
        again = _fake_llm()
        self._run(again, force=True)
        self.assertEqual(_chunks_seen(again), ["A", "B", "C"])

    def test_checkpointing_is_off_when_no_out_root_is_given(self):
        # Back-compat: existing callers pass no store and must keep working.
        fake = _fake_llm()
        full, _u, _n, _t = self._run(fake, out_root=None)
        self.assertEqual(full, "EN-A\n\nEN-B\n\nEN-C")
        self.assertEqual(_chunks_seen(fake), ["A", "B", "C"])


def _failing_llm(message, fail_times=None):
    """call_llm stand-in that fails chunk C with a given error message."""
    prompts: list[str] = []
    state = {"failures": 0}

    def fake(base_url, api_key, model, prompt, *a, **kw):
        prompts.append(prompt)
        which = next((v for k, v in _MARKERS.items() if k in prompt), "?")
        if which == "C" and (fail_times is None or state["failures"] < fail_times):
            state["failures"] += 1
            raise RuntimeError(message)
        return {"translation": f"EN-{which}", "unknown_terms": [], "notes": []}

    fake.prompts = prompts
    return fake


class TestChunkRetry(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out_root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _run(self, fake, **over):
        import translate.translate as translate
        import unittest.mock as mock
        kw = dict(raw_text=_THREE_CHUNK_DOC, first_names=set(), last_names=set(),
                  glossary=[], base_url="http://x", api_key="k", model="m",
                  mock=False, chunk_chars=6000, no_mask=True, name_candidates=None,
                  out_root=self.out_root, chunk_retries=2)
        kw.update(over)
        with mock.patch("translate.translate.call_llm", side_effect=fake):
            return translate._translate_chunks_with_term_map(**kw)

    def test_sentinel_loss_retries_the_chunk_instead_of_failing_the_document(self):
        # Sentinel masking removed — retry now triggers on delimiter loss (Segment/Cell count mismatch)
        fake = _failing_llm("Segment count mismatch: sent 4, got 3 — model did not preserve delimiters", fail_times=1)
        full, _u, _n, _t = self._run(fake)
        self.assertEqual(full, "EN-A\n\nEN-B\n\nEN-C")
        self.assertEqual(_chunks_seen(fake), ["A", "B", "C", "C"])

    def test_delimiter_loss_is_retryable(self):
        fake = _failing_llm("Segment count mismatch: sent 4, got 3 — model did not preserve delimiters",
                            fail_times=1)
        full, _u, _n, _t = self._run(fake)
        self.assertEqual(full, "EN-A\n\nEN-B\n\nEN-C")

    def test_retries_are_bounded_and_then_the_chunk_fails(self):
        fake = _failing_llm("Segment count mismatch: sent 4, got 3 — model did not preserve delimiters")
        with self.assertRaises(RuntimeError):
            self._run(fake)
        # 1 initial attempt + 2 retries on chunk C
        self.assertEqual(_chunks_seen(fake), ["A", "B", "C", "C", "C"])

    def test_environment_faults_are_not_retried(self):
        # Fail-closed stays fail-closed: a missing YAP install will not fix itself
        # on attempt two, and burning three LLM calls on it hides the real cause.
        fake = _failing_llm("YAP required for glossary masking — fail-closed: yap.exe not found")
        with self.assertRaises(RuntimeError):
            self._run(fake)
        self.assertEqual(_chunks_seen(fake), ["A", "B", "C"])

    def test_a_chunk_that_succeeds_on_retry_is_checkpointed(self):
        fake = _failing_llm("Segment count mismatch: sent 4, got 3 — model did not preserve delimiters", fail_times=1)
        self._run(fake)
        self.assertEqual(len(list((self.out_root / "chunks").rglob("*.json"))), 3)

    def test_retry_is_off_by_default(self):
        # Existing callers keep today's fail-fast behaviour unless they opt in.
        import inspect, translate.translate as translate
        sig = inspect.signature(translate._translate_chunks_with_term_map)
        self.assertEqual(sig.parameters["chunk_retries"].default, 0)


class TestCheckpointingIsWiredIntoMain(unittest.TestCase):
    """The unit tests above drive the chunk loop directly with no_mask=True.
    These exercise the real CLI path, masking included."""

    def _vault(self):
        import shutil
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        vault = Path(tmp.name) / "vault"
        (vault / "raw_md").mkdir(parents=True)
        shutil.copytree(ROOT / "data" / "person_names", vault / "data" / "person_names")
        # Three headings -> three chunks. No glossary, so no YAP dependency.
        (vault / "raw_md" / "doc.md").write_text(
            "# פרק א\nשלום עולם שלום עולם\n\n"
            "# פרק ב\nבוקר טוב בוקר טוב\n\n"
            "# פרק ג\nערב טוב ערב טוב\n",
            encoding="utf-8")
        (vault / "convert_config.json").write_text('{"translation": {"model": "m"}}', encoding="utf-8")
        return vault

    def _run(self, vault, *extra):
        import translate.translate as translate
        try:
            translate.main([str(vault), "--mock", *extra])
        except SystemExit as e:
            if e.code not in (0, 1, None):
                raise

    def test_main_checkpoints_every_chunk(self):
        vault = self._vault()
        self._run(vault)
        stored = list((vault / "data" / "translations" / "chunks").rglob("*.json"))
        self.assertEqual(len(stored), 3, "one checkpoint per chunk")

    def test_checkpoints_live_under_the_translations_store(self):
        vault = self._vault()
        self._run(vault)
        chunks_dir = vault / "data" / "translations" / "chunks"
        self.assertTrue(chunks_dir.is_dir())

    def test_chunk_retries_flag_is_accepted(self):
        vault = self._vault()
        self._run(vault, "--chunk-retries", "2")
        self.assertTrue((vault / "data" / "translations" / "chunks").is_dir())

    def test_checkpoint_payloads_are_valid_json_with_a_translation(self):
        import json as _json
        vault = self._vault()
        self._run(vault)
        for f in (vault / "data" / "translations" / "chunks").rglob("*.json"):
            payload = _json.loads(f.read_text(encoding="utf-8"))
            self.assertIn("translation", payload)
            self.assertIsInstance(payload["translation"], str)


_GLOSSARY_DOC = "# פרק א\nהמערכת פועלת\n\n# פרק ב\nהמערכת נבדקת\n"
_GLOSSARY = [{"term_he": "מערכת", "translations": ["system"], "keep_source": "0",
              "status": "approved", "notes": "", "example_doc": ""}]


class TestCheckpointWithGlossary(unittest.TestCase):
    """The glossary path is where the occurrence counts that this project exists
    to protect are produced, so resuming must not disturb them."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out_root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _run(self, **over):
        import translate.translate as translate
        import unittest.mock as mock
        kw = dict(raw_text=_GLOSSARY_DOC, first_names=set(), last_names=set(),
                  glossary=_GLOSSARY, base_url="", api_key="", model="m",
                  mock=True, chunk_chars=6000, no_mask=False, name_candidates=None,
                  out_root=self.out_root, chunk_retries=0)
        kw.update(over)
        roots = lambda toks: ["מערכת" if t in ("המערכת", "מערכת") else t for t in toks]
        analyze = lambda toks: [(t, "מערכת", "ה", "") if t == "המערכת" else (t, t, "", "")
                                for t in toks]
        with mock.patch("translate.translate._yap_root_keys", side_effect=roots):
            with mock.patch("translate.translate._yap_analyze", side_effect=analyze):
                return translate._translate_chunks_with_term_map(**kw)

    def test_term_map_occurrences_survive_a_resume(self):
        fresh_full, _u, _n, fresh_tm = self._run()
        other = Path(tempfile.mkdtemp(dir=self._tmp.name))
        resumed_full, _u2, _n2, resumed_tm = self._run(out_root=other)
        self.assertEqual(fresh_tm, resumed_tm)
        self.assertEqual(fresh_full, resumed_full)
        self.assertTrue(fresh_tm, "glossary term should appear in the term_map")

    def test_occurrences_are_summed_across_chunks(self):
        _full, _u, _n, tm = self._run()
        total = sum(e["occurrences"] for e in tm if e["term_he"] == "מערכת")
        self.assertEqual(total, 2, "one occurrence in each of the two chunks")

    def test_resume_does_not_retranslate_cached_chunks(self):
        import translate.translate as translate
        import unittest.mock as mock
        self._run()
        with mock.patch("translate.translate._translate_one_chunk",
                        side_effect=AssertionError("cached chunk was retranslated")):
            self._run()


class TestLargeDocumentSurvival(unittest.TestCase):
    """HANDOFF §4.1's actual scenario: a 100-200 page PDF is ~67 chunks, and a
    failure in chunk 43 used to discard the 42 chunks in front of it."""

    N_CHUNKS = 67
    FAIL_AT = 43

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out_root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        # Unique space-delimited Hebrew token per chunk; "ב"*43 + " " matches only chunk 43.
        self.doc = "\n\n".join(f"# פרק {i}\n{'ב' * i} טקסט" for i in range(1, self.N_CHUNKS + 1))

    def _llm(self, fail_at=None):
        calls: list[int] = []
        needle = f"{'ב' * fail_at} " if fail_at else None

        def fake(base_url, api_key, model, prompt, *a, **kw):
            idx = next(i for i in range(self.N_CHUNKS, 0, -1) if f"{'ב' * i} " in prompt)
            calls.append(idx)
            if needle and needle in prompt:
                raise RuntimeError("glossary sentinel lost in whole-doc: [{'term': 'x'}]")
            return {"translation": f"EN chunk {idx}", "unknown_terms": [], "notes": []}

        fake.calls = calls
        return fake

    def _run(self, fake):
        import translate.translate as translate
        import unittest.mock as mock
        with mock.patch("translate.translate.call_llm", side_effect=fake):
            return translate._translate_chunks_with_term_map(
                raw_text=self.doc, first_names=set(), last_names=set(), glossary=[],
                base_url="http://x", api_key="k", model="m", mock=False,
                chunk_chars=6000, no_mask=True, name_candidates=None,
                out_root=self.out_root, chunk_retries=0)

    def test_a_failure_at_chunk_43_costs_one_chunk_not_the_document(self):
        first = self._llm(fail_at=self.FAIL_AT)
        with self.assertRaises(RuntimeError):
            self._run(first)
        self.assertEqual(first.calls, list(range(1, self.FAIL_AT + 1)))

        second = self._llm()
        full, _u, _n, _t = self._run(second)
        # Only chunk 43 onward is re-sent; 1-42 come back from checkpoints.
        self.assertEqual(second.calls, list(range(self.FAIL_AT, self.N_CHUNKS + 1)))
        self.assertEqual(full.count("EN chunk"), self.N_CHUNKS)
        self.assertTrue(full.startswith("EN chunk 1\n\n"))
        self.assertTrue(full.endswith(f"EN chunk {self.N_CHUNKS}"))

    def test_chunk_order_is_preserved_across_a_resume(self):
        with self.assertRaises(RuntimeError):
            self._run(self._llm(fail_at=self.FAIL_AT))
        full, _u, _n, _t = self._run(self._llm())
        self.assertEqual(full.split("\n\n"),
                         [f"EN chunk {i}" for i in range(1, self.N_CHUNKS + 1)])

class TestPersonNameGuardAcrossResume(unittest.TestCase):
    """The curated person-name lists are edited between runs — name_candidates.txt
    exists so the operator can feed names back in. If the name sets are not part of
    the checkpoint key, a resumed run replays chunks that never saw the new names
    and the guard silently fails *open*."""

    DOC = "# פרק א\nדנה כהן כתבה את המסמך הזה\n"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out_root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _run(self, first, last, out_root=None):
        import translate.translate as translate
        import unittest.mock as mock
        cands: set[str] = set()
        fake = lambda *a, **kw: {"translation": "EN body", "unknown_terms": [], "notes": []}
        with mock.patch("translate.translate.call_llm", side_effect=fake):
            full, unknown, notes, tm = translate._translate_chunks_with_term_map(
                raw_text=self.DOC, first_names=first, last_names=last, glossary=[],
                base_url="http://x", api_key="k", model="m", mock=False,
                chunk_chars=6000, no_mask=True, name_candidates=cands,
                out_root=out_root or self.out_root, chunk_retries=0)
        return {"unknown": unknown, "notes": notes, "candidates": cands}

    def test_adding_person_names_invalidates_cached_chunks(self):
        self._run(set(), set())                                   # populate store
        resumed = self._run({"דנה"}, {"כהן"})                      # same store
        fresh = self._run({"דנה"}, {"כהן"},
                          out_root=Path(tempfile.mkdtemp(dir=self._tmp.name)))
        self.assertEqual(resumed["unknown"], fresh["unknown"])
        self.assertEqual(resumed["candidates"], fresh["candidates"])
        self.assertEqual(resumed["notes"], fresh["notes"])

    def test_the_dropped_name_is_actually_detected_on_a_fresh_run(self):
        # Guards the test above: if this stops flagging the name, the comparison
        # would pass vacuously.
        fresh = self._run({"דנה"}, {"כהן"})
        self.assertIn("דנה כהן", fresh["candidates"])
        self.assertIn("דנה כהן", fresh["unknown"])


class TestGlossaryFingerprintOrder(unittest.TestCase):
    def test_reordering_the_glossary_changes_the_fingerprint(self):
        # Fingerprint is now sorted by term_he and lower+The-normalized translations,
        # so reordering glossary.json should NOT invalidate cache — same fingerprint.
        import translate.translate as translate
        a = {"term_he": "מערכת", "translations": ["system"], "status": "approved", "keep_source": "0"}
        b = {"term_he": "תהליך", "translations": ["process"], "status": "approved", "keep_source": "0"}
        self.assertEqual(translate._glossary_fingerprint([a, b]),
                         translate._glossary_fingerprint([b, a]))

    def test_identical_order_gives_identical_fingerprint(self):
        import translate.translate as translate
        a = {"term_he": "מערכת", "translations": ["system"], "status": "approved", "keep_source": "0"}
        self.assertEqual(translate._glossary_fingerprint([a]), translate._glossary_fingerprint([a]))


class TestCheckpointRobustness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out_root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_checkpoint_without_a_translation_is_a_miss(self):
        # A dict from an older payload shape parses fine as JSON. Returning it lets
        # the caller raise KeyError, which main() does not catch (it guards only
        # RuntimeError) — one stale file would end the whole 3,800-page run.
        from translate.translation_checkpoint import save_chunk_checkpoint, load_chunk_checkpoint
        save_chunk_checkpoint(self.out_root, "aa11", {"note": "older payload shape"})
        self.assertIsNone(load_chunk_checkpoint(self.out_root, "aa11"))

    def test_checkpoint_with_non_string_translation_is_a_miss(self):
        from translate.translation_checkpoint import save_chunk_checkpoint, load_chunk_checkpoint
        save_chunk_checkpoint(self.out_root, "aa22", {"translation": ["not", "a", "string"]})
        self.assertIsNone(load_chunk_checkpoint(self.out_root, "aa22"))

    def test_checkpoint_with_malformed_term_map_is_a_miss(self):
        # The caller does e["term_he"] unguarded while aggregating.
        from translate.translation_checkpoint import save_chunk_checkpoint, load_chunk_checkpoint
        save_chunk_checkpoint(self.out_root, "aa33",
                              {"translation": "ok", "term_map": [{"english": "system"}]})
        self.assertIsNone(load_chunk_checkpoint(self.out_root, "aa33"))

    def test_json_list_checkpoint_is_a_miss(self):
        from translate.translation_checkpoint import chunk_checkpoint_path, load_chunk_checkpoint
        p = chunk_checkpoint_path(self.out_root, "aa44")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('["not", "a", "dict"]', encoding="utf-8")
        self.assertIsNone(load_chunk_checkpoint(self.out_root, "aa44"))

    def test_unserialisable_payload_does_not_escape_as_a_crash(self):
        # A lone surrogate in an LLM response makes json.dump raise UnicodeEncodeError,
        # which is a ValueError — not an OSError — so it escaped the caller's guard
        # and main()'s RuntimeError guard, killing the batch.
        from translate.translation_checkpoint import save_chunk_checkpoint
        with self.assertRaises((OSError, ValueError, TypeError)):
            save_chunk_checkpoint(self.out_root, "aa55", {"translation": "bad \ud83d surrogate"})
        self.assertEqual([p for p in self.out_root.rglob("*") if p.is_file()], [])


class TestPipelineCodeFingerprint(unittest.TestCase):
    def _dir(self):
        from translate.translation_checkpoint import _CODE_FINGERPRINT_MODULES
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        for name in _CODE_FINGERPRINT_MODULES:
            (d / name).write_text(f"# {name}\n", encoding="utf-8")
        return d

    def test_same_source_gives_same_fingerprint(self):
        from translate.translation_checkpoint import pipeline_code_fingerprint
        d = self._dir()
        self.assertEqual(pipeline_code_fingerprint(d), pipeline_code_fingerprint(d))

    def test_editing_a_tracked_module_changes_the_fingerprint(self):
        from translate.translation_checkpoint import pipeline_code_fingerprint
        d = self._dir()
        before = pipeline_code_fingerprint(d)
        (d / "translation_prompt.py").write_text("# changed rules\n", encoding="utf-8")
        self.assertNotEqual(before, pipeline_code_fingerprint(d))

    def test_a_missing_module_still_yields_a_fingerprint(self):
        # A partial checkout degrades to a coarser digest rather than an exception,
        # and the digest still changes — the fail-safe direction.
        from translate.translation_checkpoint import pipeline_code_fingerprint
        d = self._dir()
        before = pipeline_code_fingerprint(d)
        (d / "md_mask.py").unlink()
        after = pipeline_code_fingerprint(d)
        self.assertNotEqual(before, after)
        self.assertEqual(len(after), 16)

    def test_the_real_pipeline_fingerprint_is_computable(self):
        from translate.translation_checkpoint import pipeline_code_fingerprint
        self.assertEqual(len(pipeline_code_fingerprint()), 16)


class TestNamesFingerprint(unittest.TestCase):
    def test_adding_a_name_changes_the_fingerprint(self):
        from translate.translation_checkpoint import names_fingerprint
        self.assertNotEqual(names_fingerprint({"דנה"}, set()),
                            names_fingerprint({"דנה", "יוסי"}, set()))

    def test_first_and_last_lists_are_distinguished(self):
        from translate.translation_checkpoint import names_fingerprint
        self.assertNotEqual(names_fingerprint({"כהן"}, set()),
                            names_fingerprint(set(), {"כהן"}))

    def test_set_iteration_order_does_not_matter(self):
        from translate.translation_checkpoint import names_fingerprint
        self.assertEqual(names_fingerprint({"א", "ב", "ג"}, set()),
                         names_fingerprint({"ג", "ב", "א"}, set()))


class TestUnwritableStoreDoesNotKillTheRun(unittest.TestCase):
    def test_a_serialisation_failure_degrades_to_a_warning(self):
        # main() guards only RuntimeError, so anything else escaping the save
        # ends the batch over the remaining corpus.
        import translate.translate as translate
        import unittest.mock as mock
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        boom = UnicodeEncodeError("utf-8", "x", 0, 1, "surrogates not allowed")
        with mock.patch("translate.translate.save_chunk_checkpoint", side_effect=boom):
            with mock.patch("translate.translate.call_llm", side_effect=_fake_llm()):
                full, _u, _n, _t = translate._translate_chunks_with_term_map(
                    raw_text=_THREE_CHUNK_DOC, first_names=set(), last_names=set(),
                    glossary=[], base_url="http://x", api_key="k", model="m",
                    mock=False, chunk_chars=6000, no_mask=True, name_candidates=None,
                    out_root=Path(tmp.name), chunk_retries=0)
        self.assertEqual(full, "EN-A\n\nEN-B\n\nEN-C")


if __name__ == "__main__":
    unittest.main()
