"""Verify retrieval, graph integrity and incremental rebuilds, without touching real notes."""
import json
from pathlib import Path
import tempfile
import unittest

from knowledge.index import build, search


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "vault").mkdir()
        self.manifest = {"observed_on": "2026-09-07", "sources": [
            {"id": "source", "title": "Source", "url": "https://example.com/source", "status": "queued"}
        ]}
        self.write_manifest()

    def write_manifest(self):
        (self.root / "sources.json").write_text(json.dumps(self.manifest), encoding="utf-8")

    def test_cjk_and_english_retrieval(self):
        (self.root / "vault" / "skills.md").write_text("# 技能自进化\nEvaluation with independent evidence.", encoding="utf-8")
        (self.root / "vault" / "travel.md").write_text("# Travel\nShipping lanes.", encoding="utf-8")
        build(self.root)
        self.assertEqual([r["id"] for r in search("自进化", self.root)], ["skills"])
        self.assertEqual([r["id"] for r in search("independent evidence", self.root)], ["skills"])
        self.assertEqual(search("unrelated", self.root), [])

    def test_incremental_changes_remove_stale_content(self):
        note = self.root / "vault" / "note.md"
        note.write_text("# Draft\nOldtoken", encoding="utf-8")
        build(self.root)
        note.write_text("# Revised\nNewtoken", encoding="utf-8")
        build(self.root)
        self.assertFalse(search("Oldtoken", self.root))
        self.assertEqual(search("Newtoken", self.root)[0]["title"], "Revised")
        self.assertEqual(note.read_text(encoding="utf-8"), "# Revised\nNewtoken")

    def test_invalid_graph_does_not_replace_valid_index(self):
        note = self.root / "vault" / "note.md"
        note.write_text("# Valid\nKnownword", encoding="utf-8")
        build(self.root)
        note.write_text("# Invalid\n[[missing]]", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Broken wiki"):
            build(self.root)
        self.assertEqual(search("Knownword", self.root)[0]["title"], "Valid")

    def test_duplicate_source_url_is_rejected(self):
        self.manifest["sources"].append(self.manifest["sources"][0] | {"id": "duplicate"})
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "Duplicate source"):
            build(self.root)


if __name__ == "__main__":
    unittest.main()
