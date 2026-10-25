"""照合の試験。照合を変えるたびに python -m unittest で全部通ることを確認する（CLAUDE.md 第10節）。"""
import json
import unittest
from pathlib import Path

from server.ocr_match import NOT_FOOD, lines_from_ocr_text, match_lines, search

ROOT = Path(__file__).resolve().parent.parent
FOODS = {f["id"]: f for f in json.loads((ROOT / "data/foods.json").read_text(encoding="utf-8"))}
CASES = json.loads((ROOT / "tests/receipt_lines.json").read_text(encoding="utf-8"))
PANTRY = json.loads((ROOT / "data/excluded.json").read_text(encoding="utf-8"))


def match_one(line, learned=None):
    r = match_lines([{"name": line, "jan": None}], FOODS, learned, PANTRY)
    return r["matched"][0]["food_id"] if r["matched"] else None


class ReceiptLinesTest(unittest.TestCase):
    def test_lines(self):
        """tests/receipt_lines.json の17件（仕様の一覧）"""
        for case in CASES:
            with self.subTest(line=case["line"]):
                self.assertEqual(match_one(case["line"]), case["expected"])

    def test_pantry_items_are_not_registered(self):
        """食材表から取り除いた「冷蔵・冷凍しないもの」は pantry に入り、別の食材に取り違えない"""
        lines = ["サラダ油", "ごま油", "ツナ缶 3缶", "無洗米5kg"]
        r = match_lines([{"name": n, "jan": None} for n in lines], FOODS, None, PANTRY)
        self.assertEqual(([m["raw"] for m in r["matched"]], [p["raw"] for p in r["pantry"]]), ([], ["サラダ油", "ごま油", "ツナ缶 3缶", "無洗米5kg"]))

    def test_one_char_keyword_only_whole_name(self):
        self.assertEqual(match_one("卵"), "egg")
        self.assertNotEqual(match_one("酢豚"), match_one("酢"))

    def test_unmatched_has_candidates(self):
        r = match_lines([{"name": "CookDo四川式麻婆", "jan": None}], FOODS)
        self.assertIn("mabo_moto", r["unmatched"][0]["candidates"])

    def test_learned_name_wins(self):
        learned = {"names": {"cookdo四川式麻婆": "mabo_moto"}, "jans": {}}
        self.assertEqual(match_one("CookDo四川式麻婆", learned), "mabo_moto")

    def test_learned_jan_wins(self):
        learned = {"names": {}, "jans": {"4902820208719": "mabo_moto"}}
        r = match_lines([{"name": "なにかの品名", "jan": "4902820208719"}], FOODS, learned)
        self.assertEqual(r["matched"][0]["food_id"], "mabo_moto")

    def test_learned_not_food(self):
        learned = {"names": {"ぼっくすてぃっしゅ": NOT_FOOD}, "jans": {}}
        r = match_lines([{"name": "ボックスティッシュ", "jan": None}], FOODS, learned)
        self.assertEqual((r["matched"], r["unmatched"], len(r["not_food"])), ([], [], 1))

    def test_search_kana_and_kanji(self):
        self.assertEqual(search("ぶた", FOODS)[0]["id"], "pork_koma")
        self.assertIn("food_0653", [f["id"] for f in search("もめん", FOODS)])
        self.assertIn("food_0653", [f["id"] for f in search("木綿", FOODS)])

    def test_ocr_text_keeps_jan(self):
        text = "2026年07月29日(水)\n4979475013647 3倍\n*A&木綿とうふ150gx3  ¥98\n小計 ¥424"
        r = lines_from_ocr_text(text)
        self.assertEqual(r["purchased_at"], "2026-07-29")
        self.assertEqual(r["lines"], [{"name": "*A&木綿とうふ150gx3", "jan": "4979475013647"}])


if __name__ == "__main__":
    unittest.main()
