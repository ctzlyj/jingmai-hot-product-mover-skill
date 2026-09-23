import json
import sys
import unittest
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))
from vision_compliance_audit import (
    DEFAULT_PRIMARY_MODEL,
    DEFAULT_REVIEW_MODEL,
    _extract_json,
    build_parser,
    is_review_needed,
    load_done,
)


class VisionComplianceAuditTests(unittest.TestCase):
    def test_skill_documents_two_tier_policy(self):
        skill_text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("references/vision-compliance-audit.md", skill_text)
        self.assertIn(DEFAULT_PRIMARY_MODEL, skill_text)
        self.assertIn(DEFAULT_REVIEW_MODEL, skill_text)
        self.assertTrue((SKILL_ROOT / "references" / "vision-compliance-audit.md").is_file())

    def test_defaults_use_fast_primary_and_stronger_review(self):
        self.assertEqual(DEFAULT_PRIMARY_MODEL, "gemini-2.5-flash-lite")
        self.assertEqual(DEFAULT_REVIEW_MODEL, "gemini-2.5-flash")
        parser = build_parser()
        args = parser.parse_args(["--list", "x.txt", "--out", "p.jsonl", "--review-out", "r.jsonl", "--summary", "s.json"])
        self.assertEqual(args.primary_model, DEFAULT_PRIMARY_MODEL)
        self.assertEqual(args.review_model, DEFAULT_REVIEW_MODEL)

    def test_review_needed_for_flagged_failed_and_unparseable(self):
        self.assertTrue(is_review_needed({"ok": True, "parsed": {"violations": [{"term": "TOP"}]}}))
        self.assertTrue(is_review_needed({"ok": False, "error": "HTTP 500"}))
        self.assertTrue(is_review_needed({"ok": True, "parsed": None}))
        self.assertTrue(is_review_needed({"ok": True, "parsed": {"violations": None}}))
        self.assertFalse(is_review_needed({"ok": True, "parsed": {"violations": []}}))

    def test_json_extraction_tolerates_code_fences(self):
        parsed = _extract_json('```json\n{"violations": []}\n```')
        self.assertEqual(parsed, {"violations": []})
        self.assertIsNone(_extract_json("no json here"))

    def test_resume_skips_only_successful_records(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "primary.jsonl"
            out.write_text(
                json.dumps({"path": "a.png", "ok": True, "parsed": {"violations": []}}) + "\n" +
                json.dumps({"path": "b.png", "ok": False, "error": "URLError"}) + "\n",
                encoding="utf-8")
            done = load_done(out)
        self.assertEqual(sorted(done), ["a.png"])


if __name__ == "__main__":
    unittest.main()
