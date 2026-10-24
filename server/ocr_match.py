"""OCRで読んだレシートの文字を、食材表のidに当てはめる。

ブラウザ（Tesseract.js）で画像から文字を読み、その文字列だけをサーバーに送る。
当てはめはコードで行う（表記ゆれは receipt_aliases とあいまい一致で吸収する）。
"""
import re
import unicodedata
from datetime import date
from difflib import SequenceMatcher

FUZZY_THRESHOLD = 0.75

# 食材ではない行（合計・支払いなど）
NON_ITEM_WORDS = [
    "合計", "小計", "税", "対象", "お釣", "おつり", "釣銭", "預", "現金", "クレジット", "カード",
    "点数", "領収", "レシート", "電話", "tel", "登録番号", "値引", "割引", "ポイント", "残高",
    "責", "no.", "様", "店", "ありがとう", "毎度", "営業", "支払", "交通", "電子マネー", "paypay",
]
# 食材そのものではない加工品・調味料（「麻婆豆腐の素」を豆腐と取り違えないように）
NOT_INGREDIENT_WORDS = ["の素", "のもと", "麻婆", "マーボー", "たれ", "タレ", "ソース", "ドレッシング", "ふりかけ",
                        "スープ", "ルウ", "ルー", "ドリンク", "ジュース", "菓子", "スナック"]
PRICE_RE = re.compile(r"[¥\\]?\s*\d[\d,]*\s*(円|\*|※|外|内|軽)?\s*$")
DATE_RE = re.compile(r"(20\d{2})\s*[年/\-.]\s*(\d{1,2})\s*[月/\-.]\s*(\d{1,2})")


def normalize(s):
    s = unicodedata.normalize("NFKC", s).lower()
    s = re.sub(r"\s+", "", s)
    # カタカナ → ひらがな（「豚コマ」と「豚こま」を同じに扱う）
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in s)


def _terms(ingredients):
    out = []
    for ing in ingredients.values():
        for t in [ing["name"], *ing["receipt_aliases"]]:
            n = normalize(t)
            if n:
                out.append((n, ing["id"]))
    out.sort(key=lambda x: -len(x[0]))  # 長い表記を先に当てる
    return out


def _fuzzy_contains(term, line):
    if len(term) < 3 or len(line) < len(term) - 1:
        return 0.0
    best = 0.0
    for size in (len(term) - 1, len(term), len(term) + 1):
        for i in range(0, max(1, len(line) - size + 1)):
            r = SequenceMatcher(None, term, line[i:i + size]).ratio()
            best = max(best, r)
    return best


def _clean_raw(line, strip_price=True):
    raw = unicodedata.normalize("NFKC", line).strip()
    if strip_price:
        raw = PRICE_RE.sub("", raw).strip()
    raw = re.sub(r"(?<=[^\x00-\x7f])\s+(?=[^\x00-\x7f])", "", raw)  # 「国 産 豚」→「国産豚」
    raw = re.sub(r"[\s_。、・|~\-]+$", "", raw)  # OCRが行末に付けるごみ
    raw = re.sub(r"^[\s*＊※]+", "", raw)  # 行頭の軽減税率の印
    return re.sub(r"\s{2,}", " ", raw)


def extract_date(text):
    for m in DATE_RE.finditer(unicodedata.normalize("NFKC", text)):
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            continue
    return None


def _is_item_line(line):
    norm = normalize(line)
    if len(norm) < 2:
        return False
    return not (any(w in norm for w in NON_ITEM_WORDS) or DATE_RE.search(unicodedata.normalize("NFKC", line)))


def _match_line(line, terms):
    norm = normalize(line)
    if any(normalize(w) in norm for w in NOT_INGREDIENT_WORDS):
        return None
    hit = next((ing_id for term, ing_id in terms if term in norm), None)
    if hit is None:
        score, cand = max(((_fuzzy_contains(term, norm), ing_id) for term, ing_id in terms), default=(0, None))
        if score >= FUZZY_THRESHOLD:
            hit = cand
    return hit


def match_lines(lines, ingredients):
    """Claude が文字起こしした品名の行を照合する。当たらない行はすべて対象外にする。"""
    terms = _terms(ingredients)
    items, ignored = [], []
    for line in lines:
        if not _is_item_line(line):
            continue
        hit = _match_line(line, terms)
        raw = _clean_raw(line, strip_price=False)  # Claude の行には値段がない（「1/2」などを消さない）
        if hit:
            items.append({"ingredient_id": hit, "raw": raw})
        elif raw:
            ignored.append(raw)
    return {"items": items, "ignored": ignored}


def match_text(text, ingredients):
    """端末のOCR（予備）で読んだ文字全体を照合する。値段のない行は品目ではないとみなす。"""
    terms = _terms(ingredients)
    items, ignored = [], []
    for line in text.splitlines():
        if not _is_item_line(line):
            continue
        hit = _match_line(line, terms)
        raw = _clean_raw(line)
        if hit:
            items.append({"ingredient_id": hit, "raw": raw})
        elif PRICE_RE.search(unicodedata.normalize("NFKC", line)) and re.search(r"[ぁ-んァ-ヶ一-龠a-zA-Z]{2,}", raw):
            ignored.append(raw)  # 値段のついた品目らしい行だが、食材表にない
    return {"purchased_at": extract_date(text), "items": items, "ignored": ignored}
