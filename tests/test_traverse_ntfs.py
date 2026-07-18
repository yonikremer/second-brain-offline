# tests/test_traverse_ntfs.py
import importlib.util
from pathlib import Path
import shutil
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("traverse_ntfs", ROOT / "scripts" / "traverse_ntfs.py")
traverse_ntfs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(traverse_ntfs)


class TestTraverseNTFS(unittest.TestCase):
    def setUp(self):
        # Create temp dirs for source and destination
        self.src_dir_obj = tempfile.TemporaryDirectory()
        self.dest_dir_obj = tempfile.TemporaryDirectory()
        self.src_dir = Path(self.src_dir_obj.name)
        self.dest_dir = Path(self.dest_dir_obj.name)

    def tearDown(self):
        self.src_dir_obj.cleanup()
        self.dest_dir_obj.cleanup()

    def test_clean_extensions(self):
        exts = ["pdf", ".DOCX", "  .one  "]
        result = traverse_ntfs.clean_extensions(exts)
        self.assertEqual(result, {".pdf", ".docx", ".one"})

    def test_traverse_and_copy_basic(self):
        # Create test files in source
        (self.src_dir / "file1.pdf").write_text("pdf content")
        (self.src_dir / "file2.docx").write_text("docx content")
        (self.src_dir / "file3.zip").write_text("zip content")  # blacklisted
        (self.src_dir / "file4.csv").write_text("csv content")  # blacklisted
        (self.src_dir / "file5.txt").write_text("txt content")  # unknown

        whitelist = traverse_ntfs.DEFAULT_WHITELIST
        blacklist = traverse_ntfs.DEFAULT_BLACKLIST

        copied, blacklisted, unknown = traverse_ntfs.traverse_and_copy(
            source=self.src_dir,
            dest=self.dest_dir,
            whitelist=whitelist,
            blacklist=blacklist,
            preserve_structure=False,
            dry_run=False
        )

        self.assertEqual(copied, 2)
        self.assertEqual(blacklisted, 2)
        self.assertEqual(unknown[".txt"], 1)

        # Check copied files
        self.assertTrue((self.dest_dir / "file1.pdf").exists())
        self.assertTrue((self.dest_dir / "file2.docx").exists())
        self.assertFalse((self.dest_dir / "file3.zip").exists())
        self.assertFalse((self.dest_dir / "file4.csv").exists())
        self.assertFalse((self.dest_dir / "file5.txt").exists())

    def test_traverse_flat_collision(self):
        # Create subfolders with same name files
        sub1 = self.src_dir / "sub1"
        sub2 = self.src_dir / "sub2"
        sub1.mkdir()
        sub2.mkdir()

        (sub1 / "doc.pdf").write_text("pdf 1")
        (sub2 / "doc.pdf").write_text("pdf 2")

        whitelist = {".pdf"}
        blacklist = set()

        copied, _, _ = traverse_ntfs.traverse_and_copy(
            source=self.src_dir,
            dest=self.dest_dir,
            whitelist=whitelist,
            blacklist=blacklist,
            preserve_structure=False,
            dry_run=False
        )

        self.assertEqual(copied, 2)
        self.assertTrue((self.dest_dir / "doc.pdf").exists())
        self.assertTrue((self.dest_dir / "doc_1.pdf").exists())

        # Verify content
        c1 = (self.dest_dir / "doc.pdf").read_text()
        c2 = (self.dest_dir / "doc_1.pdf").read_text()
        self.assertEqual({c1, c2}, {"pdf 1", "pdf 2"})

    def test_traverse_preserve_structure(self):
        # Create subfolders
        sub = self.src_dir / "sub" / "nested"
        sub.mkdir(parents=True)
        (sub / "doc.pdf").write_text("pdf nested")

        whitelist = {".pdf"}
        blacklist = set()

        copied, _, _ = traverse_ntfs.traverse_and_copy(
            source=self.src_dir,
            dest=self.dest_dir,
            whitelist=whitelist,
            blacklist=blacklist,
            preserve_structure=True,
            dry_run=False
        )

        self.assertEqual(copied, 1)
        self.assertTrue((self.dest_dir / "sub" / "nested" / "doc.pdf").exists())
        self.assertEqual((self.dest_dir / "sub" / "nested" / "doc.pdf").read_text(), "pdf nested")


if __name__ == "__main__":
    unittest.main()
