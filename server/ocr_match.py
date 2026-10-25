"""レシートの品名を食材表のidに当てはめる（LLMには照合させない）。

順番（CLAUDE.md 第10節）：
1. 学習した対応（JANコード → 表記）に当たれば、それを使う
2. 正規化：NFKC → カタカナをひらがなに → 空白・記号を除く（keywords にも同じ処理）
3. キーワード照合：品名に keywords が含まれていれば当てる。長いキーワードから先に
4. 共通の除外語（の素・たれ・ソース・ドレッシング）を含む行は、track_expiry: true の生鮮食材には当てない
5. 当たらない行は unmatched に入れ、候補（candidates）を付ける
"""
import re
import unicodedata
from datetime import date

EXCLUDE_WORDS = ["の素", "たれ", "ソース", "ドレッシング"]
MAX_CANDIDATES = 3

# 予備の経路（端末のOCR）で、品名ではない行を落とすための語
NON_ITEM_WORDS = [
    "合計", "小計", "税", "対象", "お釣", "おつり", "釣銭", "預", "現金", "クレジット", "カード",
    "点数", "領収", "レシート", "電話", "tel", "登録番号", "値引", "割引", "ポイント", "残高",
    "責", "no.", "様", "店", "ありがとう", "毎度", "営業", "支払", "交通", "電子マネー", "paypay",
]
PRICE_RE = re.compile(r"[¥\\]?\s*\d[\d,]*\s*(円|\*|※|外|内|軽)?\s*$")
DATE_RE = re.compile(r"(20\d{2})\s*[年/\-.]\s*(\d{1,2})\s*[月/\-.]\s*(\d{1,2})")
JAN_RE = re.compile(r"\b(\d{13}|\d{8})\b")


def normalize(s):
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in s)  # 「豚コマ」と「豚こま」を同じに
    return re.sub(r"[\s\W_]+", "", s)  # 空白と記号（* や ・ など）を除く。長音「ー」は文字として残る


def clean_raw(line):
    """画面に出すレシート表記。行頭の軽減税率の印とOCRのごみを落とす。"""
    raw = unicodedata.normalize("NFKC", line or "").strip()
    raw = re.sub(r"(?<=[^\x00-\x7f])\s+(?=[^\x00-\x7f])", "", raw)  # 「国 産 豚」→「国産豚」
    raw = re.sub(r"[\s_。、・|~\-]+$", "", raw)
    raw = re.sub(r"^[\s*＊※]+", "", raw)
    return re.sub(r"\s{2,}", " ", raw)


def _keywords(ingredients):
    out = []
    for ing in ingredients.values():
        for kw in ing["keywords"]:
            n = normalize(kw)
            if n:
                out.append((n, ing["id"]))
    out.sort(key=lambda x: -len(x[0]))  # 長いキーワードから先に（「麻婆豆腐」を「豆腐」より先に）
    return out


def _keyword_match(norm, ingredients, keywords):
    excluded = any(normalize(w) in norm for w in EXCLUDE_WORDS)
    for kw, ing_id in keywords:
        if kw in norm and not (excluded and ingredients[ing_id]["track_expiry"]):
            return ing_id
    return None


def _common_len(a, b):
    """いちばん長い共通の部分文字列の長さ"""
    best = 0
    for i in range(len(a)):
        for j in range(i + best + 1, len(a) + 1):
            if a[i:j] in b:
                best = j - i
            else:
                break
    return best


def candidates(norm, ingredients):
    """当たらなかった行の候補。キーワードと2文字以上重なる食材を、重なりの多い順に。"""
    scored = []
    for ing in ingredients.values():
        best = 0.0
        for kw in [ing["name"], *ing["keywords"]]:
            k = normalize(kw)
            n = _common_len(k, norm)
            if n >= 2:
                best = max(best, n / len(k))
        if best >= 0.5:
            scored.append((-best, ing["id"]))
    scored.sort()
    return [ing_id for _, ing_id in scored[:MAX_CANDIDATES]]


def match_lines(lines, ingredients, learned=None):
    """lines: [{"name": 品名, "jan": JANコード or None}]（LLMの文字起こし）
    learned: {"names": {正規化した品名: id}, "jans": {JAN: id}}（端末ごとに学習した対応）"""
    learned = learned or {}
    names, jans = learned.get("names", {}), learned.get("jans", {})
    keywords = _keywords(ingredients)
    matched, unmatched = [], []
    for i, line in enumerate(lines, 1):
        raw = clean_raw(line.get("name"))
        norm = normalize(raw)
        if not norm:
            continue
        jan = line.get("jan") or None
        line_id = f"l{i}"
        ing_id, by = None, None
        if jan and jans.get(jan) in ingredients:
            ing_id, by = jans[jan], "jan"
        elif names.get(norm) in ingredients:
            ing_id, by = names[norm], "name"
        else:
            ing_id = _keyword_match(norm, ingredients, keywords)
            by = "keyword" if ing_id else None
        if ing_id:
            matched.append({"line_id": line_id, "ingredient_id": ing_id, "raw": raw, "jan": jan, "by": by})
        else:
            unmatched.append({"line_id": line_id, "raw": raw, "jan": jan, "candidates": candidates(norm, ingredients)})
    return {"matched": matched, "unmatched": unmatched}


def extract_date(text):
    for m in DATE_RE.finditer(unicodedata.normalize("NFKC", text or "")):
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            continue
    return None


def lines_from_ocr_text(text):
    """予備の経路：端末のOCRで読んだ文字全体から、品名らしい行（値段がつき、合計などではない行）を取り出す。"""
    lines, last_jan = [], None
    for line in (text or "").splitlines():
        nfkc = unicodedata.normalize("NFKC", line)
        m = JAN_RE.search(nfkc)
        if m and len(normalize(JAN_RE.sub("", nfkc))) < 4:  # JANコードだけの行は次の品名に付ける
            last_jan = m.group(1)
            continue
        norm = normalize(line)
        if len(norm) < 2 or any(w in norm for w in NON_ITEM_WORDS) or DATE_RE.search(nfkc):
            continue
        if not PRICE_RE.search(nfkc):
            continue
        name = PRICE_RE.sub("", nfkc).strip()
        if re.search(r"[ぁ-んァ-ヶ一-龠a-zA-Z]{2,}", name):
            lines.append({"name": name, "jan": last_jan})
        last_jan = None
    return {"purchased_at": extract_date(text), "lines": lines}
