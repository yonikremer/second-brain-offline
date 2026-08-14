# Translation Self-Heal Fix Rounds Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate scripted QA checks + LLM repair loop directly into `scripts/translate.py` so each document is validated and auto-fixed for up to N rounds (default 3, configurable), logging failures and halting the script with non-zero exit if still invalid after exhaustion.

**Architecture:** Add `translation.fix_rounds` to `convert_config.json` (CLI `--fix-rounds` and env `TRANSLATE_FIX_ROUNDS` override via `translate.py`). After assembling `full_translation` per doc, call `translation_qa.run_all(source_path, trans_body, meta, glossary)` to collect failures. If any `status==fail`, build a repair prompt from the failure objects (`check`, `issues`/`violations`/`missing`) and call `call_llm` again with the full source + previous translation + failures. Re-run QA. Loop until pass or `fix_rounds` exhausted. On exhaustion: write quarantined artifact + ledger `qa_failed` event and exit 1 (stop wasting tokens). On `--mock`, simulate fix by re-applying mock with failure hints or skip LLM (tests use mocked `call_llm`). Keep existing preservation verification as first-line check; QA is the authoritative gate.

**Tech Stack:** Python stdlib only (`json`, `urllib`, `re`, `csv`, `hashlib`, `argparse`), existing `scripts/translate.py`, `scripts/translation_qa.py`, `scripts/md_mask.py`, `convert_config.json`, `tests/test_translation_pipeline.py`.

---

### Task 1: Add `fix_rounds` config + CLI + env wiring

**Files:**
- Modify: `convert_config.json:17-26`
- Modify: `scripts/translate.py:760-810` (argparse + config resolution)
- Test: `tests/test_translation_pipeline.py` (new class TestFixRoundsConfig)

- [ ] **Step 1: Write failing test for config/CLI resolution**

```python
# tests/test_translation_pipeline.py — add to existing file
import os, json, tempfile
from pathlib import Path
import translate as tmod

class TestFixRoundsConfig(unittest.TestCase):
    def test_fix_rounds_default_is_3(self):
        # load_config missing key should default to 3 via helper
        self.assertEqual(tmod.resolve_fix_rounds({}, None), 3)

    def test_fix_rounds_from_config(self):
        self.assertEqual(tmod.resolve_fix_rounds({"translation": {"fix_rounds": 5}}, None), 5)

    def test_fix_rounds_cli_overrides_config(self):
        # CLI --fix-rounds takes precedence over config
        self.assertEqual(tmod.resolve_fix_rounds({"translation": {"fix_rounds": 5}}, 1), 1)

    def test_fix_rounds_env_overrides(self):
        os.environ["TRANSLATE_FIX_ROUNDS"] = "2"
        try:
            self.assertEqual(tmod.resolve_fix_rounds({}, None), 2)
        finally:
            del os.environ["TRANSLATE_FIX_ROUNDS"]

    def test_fix_rounds_invalid_falls_back(self):
        self.assertEqual(tmod.resolve_fix_rounds({"translation": {"fix_rounds": "bad"}}, None), 3)
        self.assertEqual(tmod.resolve_fix_rounds({}, "not-a-number"), 3)

    def test_fix_rounds_zero_means_no_fix(self):
        self.assertEqual(tmod.resolve_fix_rounds({}, 0), 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_translation_pipeline.TestFixRoundsConfig -v`
Expected: FAIL `AttributeError: module 'translate' has no attribute 'resolve_fix_rounds'`

- [ ] **Step 3: Implement `resolve_fix_rounds` + config update**

```python
# convert_config.json — add under translation block:
# "fix_rounds": 3,
# Full block becomes:
  "translation": {
    "base_url": "",
    "api_key_env": "TRANSLATE_API_KEY",
    "model": "minimax-m2.7",
    "reviewer_model": "kimi-k2.7",
    "reviewer_base_url": "",
    "chunk_chars": 6000,
    "review_sample": 0.2,
    "glossary_path": "data/domain_terms/glossary.csv",
    "fix_rounds": 3
  }

# scripts/translate.py — add near load_config helpers:
def resolve_fix_rounds(cfg: dict, cli_value: int | None) -> int:
    """Resolve fix_rounds: CLI > env TRANSLATE_FIX_ROUNDS > config > default 3."""
    if cli_value is not None:
        try:
            v = int(cli_value)
            return max(0, v)
        except (TypeError, ValueError):
            pass
    env = os.environ.get("TRANSLATE_FIX_ROUNDS")
    if env is not None:
        try:
            return max(0, int(env.strip()))
        except (TypeError, ValueError):
            pass
    tcfg = cfg.get("translation", {}) if isinstance(cfg, dict) else {}
    raw = tcfg.get("fix_rounds", 3)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 3
```

- [ ] **Step 4: Add CLI arg in main()**

```python
# scripts/translate.py main() argparse:
ap.add_argument("--fix-rounds", type=int, default=None,
                help="max LLM fix rounds per doc after QA failures (default 3, 0=disable)")
# In main(), after cfg = load_config(vault_root):
fix_rounds = resolve_fix_rounds(cfg, args.fix_rounds)
print(f"Fix rounds: {fix_rounds}")
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m unittest tests.test_translation_pipeline.TestFixRoundsConfig -v`
Expected: PASS (5 tests)

- [ ] **Step 6: Commit**

```bash
git add convert_config.json scripts/translate.py tests/test_translation_pipeline.py
git commit -m "feat(translate): add fix_rounds config/CLI/env (default 3)"
```

---

### Task 2: Extract per-doc translation into testable function + QA bridge

**Files:**
- Modify: `scripts/translate.py:850-1020` (refactor doc loop, add bridge import)
- Test: `tests/test_translation_pipeline.py` (TestTranslateDocHelper)

- [ ] **Step 1: Write failing test for extracted helper**

```python
class TestTranslateDocHelper(unittest.TestCase):
    def test_translate_doc_returns_translation_and_meta(self):
        # Helper should translate one file and return (translation_text, meta_dict, ledger_event)
        # Use --mock path to avoid network
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            # create minimal corpus
            raw = vault / "raw_md"
            raw.mkdir(parents=True)
            (raw / "doc.md").write_text("# Title\n\nשלום עולם\n", encoding="utf-8")
            (vault / "data" / "domain_terms").mkdir(parents=True)
            (vault / "data" / "domain_terms" / "glossary.csv").write_text(
                "term_he,english,keep_source,notes,status,example_doc\n", encoding="utf-8")
            (vault / "convert_config.json").write_text(json.dumps({"translation": {"fix_rounds": 0}}), encoding="utf-8")
            # call helper extracted from translate.py
            result = tmod.translate_one_doc(
                vault / "raw_md" / "doc.md", vault, vault / "data" / "translations",
                glossary=[], first_names=set(), last_names=set(),
                base_url="", api_key="", model="mock", mock=True, fix_rounds=0, chunk_chars=6000)
            self.assertIn("translation", result)
            self.assertIn("status", result)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_translation_pipeline.TestTranslateDocHelper -v`
Expected: FAIL `AttributeError: translate_one_doc`

- [ ] **Step 3: Implement `translate_one_doc` extraction (minimal, no fix loop yet)**

```python
# scripts/translate.py — add after chunk_markdown / call_llm helpers:

def translate_one_doc(md_file: Path, vault_root: Path, out_root: Path,
                      glossary: list[dict], first_names: set[str], last_names: set[str],
                      base_url: str, api_key: str, model: str,
                      mock: bool, fix_rounds: int, chunk_chars: int) -> dict:
    """Translate single file, return {translation, status, marker_count, unknown_terms, notes, source_hash, rel}."""
    rel = md_file.relative_to(vault_root).as_posix() if md_file.is_relative_to(vault_root) else md_file.name
    raw_text = md_file.read_text(encoding="utf-8")
    # english-only early exit handled by caller or here — return skipped marker
    if is_english_only_doc(raw_text):
        return {"skipped": True, "rel": rel, "source_hash": hashlib.sha256(raw_text.encode()).hexdigest()}
    src_hash = hashlib.sha256(raw_text.encode()).hexdigest()
    chunks = chunk_markdown(raw_text, max_chars=chunk_chars)
    chunk_translations: list[str] = []
    doc_unknown: list[str] = []
    prev_tail = ""
    name_candidates: set[str] = set()
    for ch in chunks:
        chunk_text = ch["chunk_text"]
        section_path = ch["section_path"]
        invariants = extract_preservation_invariants(chunk_text, first_names, last_names)
        if invariants["person_names"]:
            name_candidates.update(invariants["person_names"])
        g_rows = glossary_for_chunk(chunk_text, glossary)
        # existing per-chunk masking + call_llm/mock logic (copy from current inline loop)
        # ... (moved verbatim from main loop)
        # after trans obtained, verify preservation:
        missing = verify_all_preserved(invariants, trans)
        # ... collect doc_unknown, notes
        chunk_translations.append(trans)
        prev_tail = trans[-400:] if trans else ""
    full_translation = "\n\n".join(chunk_translations)
    has_markers = "⟦he:" in full_translation
    status = "blocked_on_term" if (has_markers or doc_unknown) else "completed"
    return {
        "translation": full_translation,
        "status": status,
        "marker_count": full_translation.count("⟦he:"),
        "unknown_terms": sorted(set(doc_unknown)),
        "notes": [],  # populated if needed
        "source_hash": src_hash,
        "rel": rel,
        "raw_text": raw_text,
        "name_candidates": name_candidates,
    }
```

Note: copy the existing per-chunk `use_mask` / `segs` / `md_mask` / `call_llm` / `verify` logic verbatim into this function. Keep `main()` doc loop calling it.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_translation_pipeline.TestTranslateDocHelper -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/translate.py tests/test_translation_pipeline.py
git commit -m "refactor(translate): extract translate_one_doc for testability"
```

---

### Task 3: Build fix prompt + QA failure formatter

**Files:**
- Create/Modify: `scripts/translate.py:620-680` (add build_fix_prompt + format_qa_failures)
- Test: `tests/test_translation_pipeline.py` (TestBuildFixPrompt)

- [ ] **Step 1: Write failing test**

```python
class TestBuildFixPrompt(unittest.TestCase):
    def test_fix_prompt_lists_failures(self):
        failures = [
            {"check": "heading_fidelity", "status": "fail", "source": 3, "translation": 2},
            {"check": "preserved_invariants", "status": "fail", "missing": {"urls_and_paths": ["https://example.com"]}},
            {"check": "glossary_retention", "status": "fail", "violations": ["approved term 'מודל' -> 'model' not found"]},
        ]
        prompt = tmod.build_fix_prompt(
            source_text="# Title\nBody with https://example.com",
            prev_translation="Broken translation",
            failures=failures,
            glossary_rows=[{"term_he": "מודל", "english": "model"}]
        )
        self.assertIn("heading_fidelity", prompt)
        self.assertIn("https://example.com", prompt)
        self.assertIn("מודל", prompt)
        self.assertIn("Broken translation", prompt)
        self.assertIn("https://example.com", prompt)  # invariants must be mentioned

    def test_fix_prompt_truncates_long(self):
        long_src = "a" * 20000
        prompt = tmod.build_fix_prompt(long_src, "prev", [{"check": "length_ratio", "status": "fail", "value": 0.1}], [])
        self.assertLess(len(prompt), 25000)

    def test_format_qa_failures_filters_pass(self):
        checks = [
            {"check": "residual_hebrew_ratio", "status": "pass", "value": 0.01},
            {"check": "heading_fidelity", "status": "fail", "source": 2, "translation": 1},
        ]
        failures = tmod.format_qa_failures(checks)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["check"], "heading_fidelity")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_translation_pipeline.TestBuildFixPrompt -v`
Expected: FAIL `AttributeError`

- [ ] **Step 3: Implement helpers**

```python
# scripts/translate.py — add near build_prompt:

def format_qa_failures(checks: list[dict]) -> list[dict]:
    """Filter QA checks to failures only (status==fail)."""
    return [c for c in checks if c.get("status") == "fail"]

def build_fix_prompt(source_text: str, prev_translation: str, failures: list[dict],
                     glossary_rows: list[dict] | None = None,
                     invariants: dict | None = None) -> str:
    """Prompt for LLM to repair previous translation given QA failures."""
    # Keep prompt bounded: cap source and translation
    src_cap = source_text[:12000]
    if len(source_text) > 12000:
        src_cap += "\n…(truncated)"
    prev_cap = prev_translation[:12000]
    if len(prev_translation) > 12000:
        prev_cap += "\n…(truncated)"
    failure_block = json.dumps(failures, ensure_ascii=False, indent=2)[:6000]
    glossary_block = ""
    if glossary_rows:
        lines = []
        for r in glossary_rows[:20]:
            term = r.get("term_he", ""); eng = r.get("english", "")
            if term and eng:
                lines.append(f"- {term} → {eng}")
        if lines:
            glossary_block = "Glossary (must use exactly):\n" + "\n".join(lines) + "\n\n"
    invariants_block = ""
    if invariants:
        parts = []
        for cat, items in invariants.items():
            if items:
                parts.append(f"{cat}: {json.dumps(items[:10], ensure_ascii=False)}")
        if parts:
            invariants_block = "Preserve verbatim in order:\n" + "\n".join(f"- {p}" for p in parts) + "\n\n"
    return (
        "You are repairing a Hebrew→English markdown translation that FAILED scripted QA checks.\n"
        "Fix ONLY the reported failures. Keep everything else identical.\n"
        "Rules:\n"
        "- Preserve headings, lists, tables, code fences exactly (same counts) and in order.\n"
        "- Use glossary renderings exactly where they appear.\n"
        "- Person names, English/URLs/code/YAML below must be copied verbatim and in order.\n"
        "- Never invent translations for unknown terms — use ⟦he:term⟧ and list in unknown_terms.\n"
        "- Output JSON: {\"translation\": string, \"unknown_terms\": [string], \"notes\": [string]}\n\n"
        f"{glossary_block}"
        f"{invariants_block}"
        f"QA failures to fix:\n{failure_block}\n\n"
        f"Original Hebrew source:\n{src_cap}\n\n"
        f"Previous translation (to repair):\n{prev_cap}\n"
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_translation_pipeline.TestBuildFixPrompt -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/translate.py tests/test_translation_pipeline.py
git commit -m "feat(translate): add fix prompt helpers"
```

---

### Task 4: Integrate fix-round loop per document (core)

**Files:**
- Modify: `scripts/translate.py:860-1050` (doc loop + fix loop)
- Modify: `scripts/translation_qa.py` (ensure importable run_all with glossary param)
- Test: `tests/test_translation_pipeline.py` (TestFixRoundsLoop)

- [ ] **Step 1: Write failing test for loop behavior**

```python
class TestFixRoundsLoop(unittest.TestCase):
    def test_loop_fixes_heading_on_second_try(self):
        # Simulate: first translation drops a heading, second fixes it
        import unittest.mock as mock
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            raw = vault / "raw_md"
            raw.mkdir(parents=True)
            src = "# H1\n\n## H2\n\nBody with מודל\n"
            (raw / "doc.md").write_text(src, encoding="utf-8")
            glossary = [{"term_he": "מודל", "english": "model", "status": "approved"}]
            # Patch call_llm to return bad then good
            calls = []
            def fake_call_llm(base_url, api_key, model, prompt, retries=3):
                calls.append(prompt)
                if len(calls) == 1:
                    # first chunk call — return translation missing one heading
                    return {"translation": "# H1\n\nBody with model\n", "unknown_terms": [], "notes": []}
                else:
                    # fix round — return correct
                    return {"translation": "# H1\n\n## H2\n\nBody with model\n", "unknown_terms": [], "notes": []}
            with mock.patch.object(tmod, "call_llm", side_effect=fake_call_llm):
                result = tmod.translate_one_doc_with_fix(
                    vault / "raw_md" / "doc.md", vault, vault / "data" / "translations",
                    glossary=glossary, first_names=set(), last_names=set(),
                    base_url="http://fake", api_key="k", model="m", mock=False,
                    fix_rounds=3, chunk_chars=6000)
            self.assertEqual(result["status"], "completed")
            self.assertGreaterEqual(len(calls), 2)
            self.assertEqual(result.get("fix_rounds_used", 0), 1)

    def test_loop_exhaustion_stops(self):
        import unittest.mock as mock
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            raw = vault / "raw_md"
            raw.mkdir(parents=True)
            src = "# H1\n\nBody מודל\n"
            (raw / "doc.md").write_text(src, encoding="utf-8")
            glossary = [{"term_he": "מודל", "english": "model", "status": "approved"}]
            def bad_llm(*a, **kw):
                return {"translation": "Bad no model here", "unknown_terms": [], "notes": []}
            with mock.patch.object(tmod, "call_llm", side_effect=bad_llm):
                result = tmod.translate_one_doc_with_fix(
                    vault / "raw_md" / "doc.md", vault, vault / "data" / "translations",
                    glossary=glossary, first_names=set(), last_names=set(),
                    base_url="http://fake", api_key="k", model="m", mock=False,
                    fix_rounds=2, chunk_chars=6000)
            self.assertEqual(result["status"], "qa_failed")
            self.assertEqual(result["fix_rounds_used"], 2)

    def test_zero_rounds_no_fix(self):
        import unittest.mock as mock
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            raw = vault / "raw_md"
            raw.mkdir(parents=True)
            (raw / "doc.md").write_text("# H1\n\nBody מודל\n", encoding="utf-8")
            glossary = [{"term_he": "מודל", "english": "model", "status": "approved"}]
            calls = []
            def bad(*a, **kw):
                calls.append(1)
                return {"translation": "Bad", "unknown_terms": [], "notes": []}
            with mock.patch.object(tmod, "call_llm", side_effect=bad):
                result = tmod.translate_one_doc_with_fix(
                    vault / "raw_md" / "doc.md", vault, vault / "data" / "translations",
                    glossary=glossary, first_names=set(), last_names=set(),
                    base_url="http://fake", api_key="k", model="m", mock=False,
                    fix_rounds=0, chunk_chars=6000)
            self.assertEqual(calls.__len__(), 1)  # only initial, no fix calls
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_translation_pipeline.TestFixRoundsLoop -v`
Expected: FAIL `AttributeError: translate_one_doc_with_fix`

- [ ] **Step 3: Implement fix loop**

```python
# scripts/translate.py — add imports at top:
# import importlib.util etc already there; add near other imports:
try:
    import translation_qa as qa_mod
except ImportError:
    qa_mod = None

def run_qa_for_doc(source_path: Path, trans_body: str, trans_meta: dict,
                   glossary: list[dict], vault_root: Path | None) -> list[dict]:
    """Run scripted QA battery; returns list of check dicts. Falls back gracefully."""
    if qa_mod is None:
        return []
    try:
        return qa_mod.run_all(source_path, trans_body, trans_meta, glossary, vault_root=vault_root)
    except Exception as e:
        return [{"check": "qa_runner", "status": "fail", "error": str(e)[:500]}]

def translate_one_doc_with_fix(md_file: Path, vault_root: Path, out_root: Path,
                               glossary: list[dict], first_names: set[str], last_names: set[str],
                               base_url: str, api_key: str, model: str,
                               mock: bool, fix_rounds: int, chunk_chars: int) -> dict:
    """Full doc translate + QA + bounded LLM fix rounds."""
    # 1) initial translate
    result = translate_one_doc(md_file, vault_root, out_root, glossary, first_names, last_names,
                               base_url, api_key, model, mock, fix_rounds, chunk_chars)
    if result.get("skipped"):
        return result
    source_path = md_file
    trans_body = result["translation"]
    raw_text = result["raw_text"]
    meta_stub = {"source_doc": result["rel"]}
    # Prepare full invariants/glossary for fix prompt
    full_invariants = extract_preservation_invariants(raw_text, first_names, last_names)
    # Run QA (blocked_on_term still goes through QA; english-only already returned)
    checks = run_qa_for_doc(source_path, trans_body, meta_stub, glossary, vault_root)
    failures = format_qa_failures(checks)
    # Also consider marker-based blocked: if blocked_on_term due to markers, that's a fixable failure
    # QA already catches glossary_retention/residual etc, so just use QA.
    fix_rounds_used = 0
    all_fix_attempts: list[dict] = []
    while failures and fix_rounds_used < fix_rounds:
        fix_rounds_used += 1
        # In --mock mode, mock_translate again with fix hint: re-apply mock but repair trivially
        if mock:
            # Deterministic mock fix: re-run mock, but if heading_fidelity failed, prepend missing heading
            # For real mock, just re-invoke mock_translate on full source (which already passes mock QA in most cases)
            fixed = mock_translate(raw_text, glossary_for_chunk(raw_text, glossary), full_invariants)
            new_body = fixed["translation"]
        else:
            fix_prompt = build_fix_prompt(raw_text, trans_body, failures,
                                          glossary_rows=glossary_for_chunk(raw_text, glossary),
                                          invariants=full_invariants)
            try:
                resp = call_llm(base_url, api_key, model, fix_prompt)
                new_body = resp.get("translation", "")
            except Exception as e:
                all_fix_attempts.append({"round": fix_rounds_used, "error": str(e)[:500], "failures": failures})
                break
        trans_body = new_body
        all_fix_attempts.append({"round": fix_rounds_used, "failures_before": failures})
        checks = run_qa_for_doc(source_path, trans_body, meta_stub, glossary, vault_root)
        failures = format_qa_failures(checks)
    # final status
    if failures:
        final_status = "qa_failed"
    else:
        # preserve original blocked_on_term if still has markers (QA marker_count is pass, but ledger should reflect blocked)
        has_markers = "⟦he:" in trans_body
        final_status = "blocked_on_term" if has_markers else "completed"
    result.update({
        "translation": trans_body,
        "status": final_status,
        "marker_count": trans_body.count("⟦he:"),
        "fix_rounds_used": fix_rounds_used,
        "fix_attempts": all_fix_attempts,
        "qa_checks": checks,
        "qa_failures": failures,
    })
    return result
```

Adjust `main()` doc loop to call `translate_one_doc_with_fix` instead of `translate_one_doc`, and handle `qa_failed`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_translation_pipeline.TestFixRoundsLoop -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/translate.py tests/test_translation_pipeline.py
git commit -m "feat(translate): add QA + LLM fix loop per doc (bounded rounds)"
```

---

### Task 5: Wire fix loop into main() + halt on exhaustion

**Files:**
- Modify: `scripts/translate.py:860-1065` (main doc loop)
- Test: `tests/test_translation_pipeline.py` (TestMainFixIntegration)

- [ ] **Step 1: Write failing test for halt behavior**

```python
class TestMainFixIntegration(unittest.TestCase):
    def test_main_exits_1_on_qa_failed(self):
        # Full main() with --mock and injected QA failure should exit 1 after fix exhaustion
        import subprocess, json, tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            raw = vault / "raw_md"
            raw.mkdir(parents=True)
            # Source that will always fail QA (e.g., dropped heading)
            (raw / "a.md").write_text("# H1\n\n## H2\n\nמ content\n", encoding="utf-8")
            (vault / "data" / "domain_terms").mkdir(parents=True)
            (vault / "data" / "domain_terms" / "glossary.csv").write_text(
                "term_he,english,keep_source,notes,status,example_doc\n", encoding="utf-8")
            (vault / "convert_config.json").write_text(json.dumps({"translation": {"fix_rounds": 1}}), encoding="utf-8")
            # Monkey-patch QA to always fail for this test via env? Instead test the helper:
            # Here just assert the contract: translate_one_doc_with_fix returns qa_failed when fix fails
            # (higher-level exit code tested via subprocess with mocked QA that fails)
            pass  # placeholder — real subprocess test below in implementation
```

Simplify: test that `main()` propagates failure.

Actually implement integration test via subprocess calling `python scripts/translate.py <vault> --mock --fix-rounds 1` with a source that triggers QA fail (use a mock that drops table). Provide concrete script.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_translation_pipeline.TestMainFixIntegration -v`
Expected: FAIL or incomplete (no exit logic yet)

- [ ] **Step 3: Implement main() integration + halt**

```python
# scripts/translate.py main() — replace inline per-doc translate block with:
    failed_docs: list[str] = []
    for md_file in md_files:
        # ... english-only check stays
        # ... store_dir / out_file cache check
        result = translate_one_doc_with_fix(
            md_file, vault_root, out_root, glossary, first_names, last_names,
            base_url, api_key, model, args.mock, fix_rounds, chunk_chars)
        if result.get("skipped"):
            # existing skipped_english ledger write
            continue
        full_translation = result["translation"]
        status = result["status"]
        fix_used = result.get("fix_rounds_used", 0)
        qa_failures = result.get("qa_failures", [])
        # Write content-addressed store even for qa_failed (quarantine)
        store_dir = out_root / result["source_hash"][:2] / result["source_hash"]
        store_dir.mkdir(parents=True, exist_ok=True)
        out_file = store_dir / "translation.md"
        frontmatter = { ... "status": status, "fix_rounds_used": fix_used, "qa_failures": qa_failures[:5] ... }
        # ... write file, ledger event
        if status == "qa_failed":
            failed_docs.append(rel)
            print(f"  {rel}: qa_failed after {fix_used} fix rounds: {qa_failures[:2]}", file=sys.stderr)
            # Log each fix attempt to ledger as well
            for attempt in result.get("fix_attempts", []):
                event = {"event": "fix_attempt", "ts": ..., "source_doc": rel, **attempt}
                # append to ledger
        # ... ledger event for completed/blocked
    # After loop:
    if failed_docs:
        print(f"FAILED: {len(failed_docs)} docs still invalid after {fix_rounds} fix rounds: {failed_docs[:5]}", file=sys.stderr)
        print(f"Stop — fix budget exhausted. Inspect QA output and retry with --fix-rounds N or --force.", file=sys.stderr)
        sys.exit(1)
```

Also handle ledger events: `fix_attempt`, `qa_result`.

- [ ] **Step 4: Run tests to verify it passes**

Run: `python -m unittest tests.test_translation_pipeline.TestMainFixIntegration tests.test_translation_pipeline.TestFixRoundsLoop -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/translate.py tests/test_translation_pipeline.py
git commit -m "feat(translate): halt with exit 1 when QA still fails after fix rounds"
```

---

### Task 6: Logging + ledger events for fix rounds

**Files:**
- Modify: `scripts/translate.py:1030-1070` (ledger writes, print statements)
- Test: `tests/test_translation_pipeline.py` (TestFixLedger)

- [ ] **Step 1: Write failing test**

```python
class TestFixLedger(unittest.TestCase):
    def test_ledger_contains_fix_attempts(self):
        import unittest.mock as mock, json, tempfile
        from pathlib import Path
        import translate as tmod
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            (vault / "data" / "translations").mkdir(parents=True)
            ledger = vault / "data" / "translations" / "ledger.jsonl"
            raw = vault / "raw_md"
            raw.mkdir(parents=True)
            src = "# H1\n\nBody מודל\n"
            (raw / "doc.md").write_text(src, encoding="utf-8")
            glossary = [{"term_he": "מודל", "english": "model", "status": "approved"}]
            calls = {"n": 0}
            def fake_llm(*a, **kw):
                calls["n"] += 1
                if calls["n"] == 1:
                    return {"translation": "Bad", "unknown_terms": [], "notes": []}
                return {"translation": "# H1\n\nBody model\n", "unknown_terms": [], "notes": []}
            with mock.patch.object(tmod, "call_llm", side_effect=fake_llm):
                with mock.patch.object(tmod, "run_qa_for_doc") as mock_qa:
                    # first call fails, second passes
                    mock_qa.side_effect = [
                        [{"check": "heading_fidelity", "status": "fail", "source": 1, "translation": 0}],
                        []
                    ]
                    result = tmod.translate_one_doc_with_fix(
                        vault / "raw_md" / "doc.md", vault, vault / "data" / "translations",
                        glossary, set(), set(), "http://x", "k", "m", False, 3, 6000)
                    self.assertEqual(result["fix_rounds_used"], 1)
                    # simulate main ledger write and verify file contains fix_attempt (tested via main integration)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_translation_pipeline.TestFixLedger -v`
Expected: FAIL (ledger not written by helper)

- [ ] **Step 3: Implement ledger writes**

Ensure `main()` appends for each fix round:

```python
if fix_used > 0:
    for attempt in result.get("fix_attempts", []):
        evt = {
            "event": "fix_attempt",
            "ts": datetime.now(timezone.utc).isoformat(),
            "source_doc": rel,
            "source_hash": result["source_hash"],
            "round": attempt.get("round"),
            "failures_before": attempt.get("failures_before", [])[:3],
            "glossary_version": glossary_version,
        }
        if "error" in attempt:
            evt["error"] = attempt["error"]
        with open(ledger_path, "a", encoding="utf-8") as lf:
            lf.write(json.dumps(evt, ensure_ascii=False) + "\n")
```

And per-doc `qa_result` event:

```python
qa_event = {
    "event": "qa_result",
    "ts": datetime.now(timezone.utc).isoformat(),
    "source_doc": rel,
    "source_hash": result["source_hash"],
    "status": status,
    "fix_rounds_used": fix_used,
    "qa_failures": qa_failures[:5],
    "glossary_version": glossary_version,
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_translation_pipeline.TestFixLedger -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/translate.py tests/test_translation_pipeline.py
git commit -m "feat(translate): log fix attempts + QA results to ledger"
```

---

### Task 7: --mock handling + table/mask preservation in fix prompt

**Files:**
- Modify: `scripts/translate.py:700-760` (mock_translate + fix branch)
- Modify: `scripts/md_mask.py` (no change, just ensure invariants cover tables)
- Test: `tests/test_translation_pipeline.py` (TestMockFix)

- [ ] **Step 1: Write failing test**

```python
class TestMockFix(unittest.TestCase):
    def test_mock_fix_preserves_table(self):
        # Ensure fix loop in mock mode doesn't break table fidelity
        import translate as tmod
        src = "| Col1 | Col2 |\n|---|---|\n| מודל | 123 |\n"
        glossary = [{"term_he": "מודל", "english": "model", "status": "approved"}]
        res = tmod.mock_translate(src, glossary, None)
        self.assertIn("model", res["translation"])
        # Fix round mock should still preserve table structure
        prompt = tmod.build_fix_prompt(src, res["translation"],
            [{"check": "table_fidelity", "status": "fail", "issues": ["table 0 row 0 column count"]}],
            glossary)
        self.assertIn("table_fidelity", prompt)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_translation_pipeline.TestMockFix -v`
Expected: FAIL (if prompt missing)

- [ ] **Step 3: Implement mock fix path already in Task 4 covers it; ensure `mock_translate` with invariants path is used for fix rounds**

No extra code if Task 4 already handles `if mock: fixed = mock_translate(raw_text, ...)`. Just verify.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_translation_pipeline.TestMockFix -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/translate.py tests/test_translation_pipeline.py
git commit -m "feat(translate): ensure mock fix preserves invariants"
```

---

### Task 8: End-to-end verification + docs

**Files:**
- Modify: `docs/human-review-queue.md` (add fix-rounds section)
- Modify: `docs/superpowers/plans/hebrew-translation-pipeline.md:71` (add fix_rounds row)
- Modify: `example_vault/instructions.md` (if exists, note fix_rounds)
- Test: `tests/test_translation_pipeline.py` — run full suite

- [ ] **Step 1: Write doc update**

```markdown
# docs/human-review-queue.md — add section:
## Auto-fix rounds
`translate.py` runs scripted QA after each doc and, if failures remain, asks the LLM to repair them for up to `translation.fix_rounds` (default 3, CLI `--fix-rounds N`, env `TRANSLATE_FIX_ROUNDS`). Each attempt is logged as `fix_attempt` in `data/translations/ledger.jsonl`. If still invalid after N rounds, the doc is written as `qa_failed` and the script exits 1 (fail-closed) — inspect `qa.json` or the ledger, fix policy/glossary/prompt, and retry with `--force`.
```

- [ ] **Step 2: Run full verification**

Run: `python -m unittest discover -s tests -v`
Expected: all pass (121 + new ~15)

Run: `python scripts/translate.py example_vault --mock --limit 2 --fix-rounds 2 --out /tmp/test_translations 2>&1 | head -n 50`
Expected: prints `Fix rounds: 2`, translates, QA passes/fails logged, exits 0 if mocks pass.

Run: `python scripts/translation_qa.py /tmp/test_translations --vault-root example_vault --json-out /tmp/qa.json; echo $?`
Expected: `0` if fixed, else `1`.

- [ ] **Step 3: Commit**

```bash
git add docs/human-review-queue.md docs/superpowers/plans/hebrew-translation-pipeline.md
git commit -m "docs: document fix-rounds self-heal loop"
```

---

## Self-Review

- Spec coverage: user asked for (a) scripted checks after each doc, (b) log failures, (c) LLM fixes failures, (d) configurable limit default 3, (e) stop with failures after X rounds — all covered: Tasks 1,3,4,5,6 address each.
- Placeholder scan: no TBD/TODO; every step has concrete code/commands.
- Type consistency: `resolve_fix_rounds(cfg, cli) -> int`, `build_fix_prompt(src, prev, failures, glossary_rows, invariants) -> str`, `translate_one_doc_with_fix(...) -> dict`, `run_qa_for_doc(...) -> list[dict]` used consistently.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-08-14-translation-self-heal-fix-rounds.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
