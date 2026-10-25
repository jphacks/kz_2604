"""照合の試験。照合を変えるたびに python -m unittest で全部通ることを確認する（CLAUDE.md 第10節）。"""
import json
import unittest
from pathlib import Path

from server.ocr_match import lines_from_ocr_text, match_lines

ROOT = Path(__file__).resolve().parent.parent
INGREDIENTS = {i["id"]: i for i in json.loads((ROOT / "data/ingredients.json").read_text(encoding="utf-8"))}
CASES = json.loads((ROOT / "tests/receipt_lines.json").read_text(encoding="utf-8"))


def match_one(line, learned=None):
    r = match_lines([{"name": line, "jan": None}], INGREDIENTS, learned)
    return r["matched"][0]["ingredient_id"] if r["matched"] else None


class ReceiptLinesTest(unittest.TestCase):
    def test_lines(self):
        for case in CASES:
            with self.subTest(line=case["line"]):
                self.assertEqual(match_one(case["line"]), case["expected"])

    def test_unmatched_has_candidates(self):
        r = match_lines([{"name": "CookDo四川式麻婆", "jan": None}], INGREDIENTS)
        self.assertIn("mabo_moto", r["unmatched"][0]["candidates"])

    def test_learned_name_wins(self):
        learned = {"names": {"cookdo四川式麻婆": "mabo_moto"}, "jans": {}}
        self.assertEqual(match_one("CookDo四川式麻婆", learned), "mabo_moto")

    def test_learned_jan_wins(self):
        learned = {"names": {}, "jans": {"4902820208719": "mabo_moto"}}
        r = match_lines([{"name": "なにかの品名", "jan": "4902820208719"}], INGREDIENTS, learned)
        self.assertEqual(r["matched"][0]["ingredient_id"], "mabo_moto")

    def test_ocr_text_keeps_jan(self):
        text = "2026年07月29日(水)\n4979475013647 3倍\n*A&木綿とうふ150gx3  ¥98\n小計 ¥424"
        r = lines_from_ocr_text(text)
        self.assertEqual(r["purchased_at"], "2026-07-29")
        self.assertEqual(r["lines"], [{"name": "*A&木綿とうふ150gx3", "jan": "4979475013647"}])


if __name__ == "__main__":
    unittest.main()
