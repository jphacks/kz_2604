"""判定ロジック（冷蔵コーチ）。

日数・期限・どの食材を救うか・どの料理を提案するかはすべてここで決める。LLMには判断させない。
食材は data/foods.json（分類ごとの目安を展開済み）。印を付ける mark_was_soon 以外は純粋関数。
"""
from datetime import date, timedelta
from urllib.parse import quote

# --- 決定事項（CLAUDE.md 第16節を参照） ---
GRACE_DAYS = 0          # 残り日数がこれ以下なら soon（目安の最終日。デモの 10/27 は通知なし、10/28 に問いかけ）
DEFAULT_SETTINGS = {"notify": True, "ask_time": "18:00", "deadline_time": "20:00", "remind_time": "21:30"}
REASK_MINUTES = 60      # 確認で「あとで」のあとの再通知（1回だけ）
MAX_REASK = 1
LAST_REASK = "23:59"    # 再通知がこれを過ぎるなら送らない
HOME_DISHES = 2         # ホームの料理の提案
LIST_DISHES = 3         # 冷蔵庫の中身・画面6の料理の提案
FREEZER_DAYS = 14       # 農林水産省「2〜3週間」の短い方
FREEZER_SOURCE = "農林水産省 消費者の部屋"
COOKPAD_SEARCH = "https://cookpad.com/jp/search/"
# 救済行動の同順位の決め方：日持ちの短い分類（ひき肉・鶏肉、魚）を先に
RESCUE_ORDER = ["ground_poultry", "fish", "raw_sausage", "shellfish", "sliced_meat", "processed_meat", "bacon", "cooked"]


def parse_date(s):
    return date.fromisoformat(s[:10])


def limit_date(inv, food):
    """冷蔵での推定の期限。パッケージの期限が入力されていればそれを優先する。
    fridge_days が空（パッケージの期限を優先する分類）や track_expiry: false の食材は None。"""
    if inv.get("expiry_override"):
        return parse_date(inv["expiry_override"])
    if not food.get("track_expiry", True) or food.get("fridge_days") is None:
        return None
    return parse_date(inv["purchased_at"]) + timedelta(days=food["fridge_days"])


def days_left(inv, food, today):
    lim = limit_date(inv, food)
    return None if lim is None else (lim - today).days


def is_soon(left):
    return left is not None and 0 <= left <= GRACE_DAYS


def freezer_state(inv, food, today):
    frozen_on = parse_date(inv.get("frozen_at") or inv.get("updated_at") or inv["purchased_at"])
    days = food.get("freezer_days") or FREEZER_DAYS
    until = frozen_on + timedelta(days=days)
    return frozen_on, (today - frozen_on).days, until, (until - today).days


def when_label(left):
    if left < 0:
        return "状態を確かめてください"
    return {0: "今日まで", 1: "明日まで"}.get(left, f"あと{left}日")


def _order(foods):
    return {fid: i for i, fid in enumerate(foods)}


def soon_items(inventory, foods, today, exclude_ids=()):
    order = _order(foods)
    items = [inv for inv in inventory
             if inv["status"] == "stocked" and inv["id"] not in exclude_ids
             and is_soon(days_left(inv, foods[inv["food_id"]], today))]
    items.sort(key=lambda inv: (days_left(inv, foods[inv["food_id"]], today), inv["purchased_at"], order[inv["food_id"]]))
    return items


def soon_names(inventory, foods, today):
    names = []
    for inv in soon_items(inventory, foods, today):
        n = foods[inv["food_id"]]["name"]
        if n not in names:
            names.append(n)
    return names


def should_ask(inventory, foods, today):
    """問いかけを出す条件（食材の側）：冷蔵の在庫に soon が1つ以上。"""
    return bool(soon_items(inventory, foods, today))


def mark_was_soon(inventory, foods, today):
    """一度でも soon になった在庫に印を付ける（「守れた」の判定に使う）。"""
    for inv in soon_items(inventory, foods, today):
        inv["was_soon"] = True


def choose_rescue(inventory, foods, today):
    """救済行動は1つだけ。soon の在庫のうち freeze_action を持つ（肉・魚介類）もので、残り日数が最も少ない1品。
    同順位は RESCUE_ORDER の分類順 → 購入日が古い順 → 食材表の順。
    冷凍に向かない soon の食材は「明日の料理の提案に回す」と1行だけ添える。"""
    order = _order(foods)
    items = soon_items(inventory, foods, today)
    candidates = [inv for inv in items if foods[inv["food_id"]].get("freeze_action")]
    candidates.sort(key=lambda inv: (
        days_left(inv, foods[inv["food_id"]], today),
        RESCUE_ORDER.index(foods[inv["food_id"]]["category"]) if foods[inv["food_id"]]["category"] in RESCUE_ORDER else 99,
        inv["purchased_at"], order[inv["food_id"]]))
    action = None
    if candidates:
        inv = candidates[0]
        food = foods[inv["food_id"]]
        how = food["freeze_action"]
        action = {
            "inventory_id": inv["id"],
            "food_id": food["id"],
            "name": food["name"],
            "how": how,                                         # 「袋のまま冷凍庫へ」
            "how_short": how.replace("冷凍庫へ", "入れる"),     # 「袋のまま入れる」
            "text": f"{food['name']}を、{how}",
            "note": f"切らなくて大丈夫。{'袋' if how.startswith('袋') else 'パック'}ごと入れるだけです。",
            "seconds": food.get("freeze_seconds") or 30,
            "freezer_note": f"冷凍の目安：{(food.get('freezer_days') or FREEZER_DAYS) // 7}週間",
            "source": FREEZER_SOURCE,
        }
    rest = [foods[inv["food_id"]] for inv in items if not action or inv["id"] != action["inventory_id"]]
    names = list(dict.fromkeys(f["name"] for f in rest))
    note = None
    if names:
        if all(not f.get("freezable") for f in rest):
            note = f"{'・'.join(names)}は冷凍に向かないので、明日の料理の提案に回しておきます。"
        else:
            note = f"{'・'.join(names)}は、明日の料理の提案に回しておきます。"
    return action, note


def cook_items(inventory, foods, today):
    """画面6：在庫（冷蔵・冷凍）を購入日の古い順に。早めの食材は最初からチェック。"""
    items = [inv for inv in inventory if inv["status"] in ("stocked", "frozen")]
    items.sort(key=lambda inv: (inv["purchased_at"], inv["id"]))
    out = []
    for inv in items:
        food = foods[inv["food_id"]]
        soon = inv["status"] == "stocked" and is_soon(days_left(inv, food, today))
        out.append({"inventory_id": inv["id"], "name": food["name"], "purchased_at": inv["purchased_at"],
                    "frozen": inv["status"] == "frozen", "soon": soon, "checked": soon})
    return out


# ---------------- 期限が近い食材（ホーム）・冷蔵庫の中身（画面0b） ----------------

def near_items(inventory, foods, today, limit=3):
    """期限の目安が近い順に（冷蔵は購入日から、冷凍は冷凍した日から）。目安のない食材は出さない。"""
    rows = []
    for inv in inventory:
        food = foods[inv["food_id"]]
        if inv["status"] == "stocked":
            left = days_left(inv, food, today)
            if left is None:
                continue
            rows.append((left, inv["purchased_at"], {"name": food["name"], "when": when_label(left), "soon": is_soon(left)}))
        elif inv["status"] == "frozen":
            left = freezer_state(inv, food, today)[3]
            rows.append((left, inv["purchased_at"], {"name": f"{food['name']}（冷凍）", "when": when_label(left), "soon": is_soon(left)}))
    rows.sort(key=lambda r: r[:2])
    return [r[2] for r in rows[:limit]]


def fridge_view(inventory, foods, today):
    fridge, freezer = [], []
    for inv in inventory:
        food = foods[inv["food_id"]]
        if inv["status"] == "stocked":
            left = days_left(inv, food, today)
            fridge.append({"inventory_id": inv["id"], "name": food["name"], "purchased_at": inv["purchased_at"],
                           "days_left": left, "soon": is_soon(left), "past": left is not None and left < 0,
                           "when": when_label(left) if left is not None else None})
        elif inv["status"] == "frozen":
            frozen_on, frozen_days, until, left = freezer_state(inv, food, today)
            freezer.append({"inventory_id": inv["id"], "name": food["name"], "frozen_at": frozen_on.isoformat(),
                            "frozen_days": frozen_days, "until": until.isoformat(), "soon": is_soon(left),
                            "days_left": left})
    big = 10 ** 6
    fridge.sort(key=lambda x: (x["days_left"] if x["days_left"] is not None else big, x["purchased_at"]))
    freezer.sort(key=lambda x: x["days_left"])
    return {"fridge": fridge, "freezer": freezer, "freezer_source": FREEZER_SOURCE,
            "freezer_note": f"冷凍の目安は、冷凍した日から{FREEZER_DAYS // 7}週間です。"}


# ---------------- 料理の提案（いつも出す） ----------------

def suggestions(inventory, foods, dishes, today, limit=LIST_DISHES):
    """対象：uses の食材がすべて手元にある料理（stocked か frozen。track_expiry: false の食材もあると数える）。
    並べ方：①使う食材のうち一番期限が近いものの残り日数が少ない順（冷凍は冷凍の目安で数える）
            ②soon の食材を多く使う順 ③調理時間が短い順。
    uses の "a|b" は「どれか1つがあればよい」（豆腐なら木綿・絹ごしでもよい）。"""
    big = 10 ** 6
    have = {}  # food_id -> {"left": 残り日数 or None, "soon": bool, "frozen": bool}
    for inv in inventory:
        food = foods[inv["food_id"]]
        if inv["status"] == "stocked":
            left = days_left(inv, food, today)
            if left is not None and left < 0:
                continue  # 目安を過ぎたものを使う料理は勧めない
            cur = {"left": left, "soon": is_soon(left), "frozen": False}
        elif inv["status"] == "frozen":
            left = freezer_state(inv, food, today)[3]
            cur = {"left": left, "soon": is_soon(left), "frozen": True}
        else:
            continue
        prev = have.get(food["id"])
        if prev is None or (cur["left"] if cur["left"] is not None else big) < (prev["left"] if prev["left"] is not None else big):
            have[food["id"]] = cur

    picked = []
    for i, dish in enumerate(dishes):
        chosen = []
        for use in dish["uses"]:
            opts = [u for u in use.split("|") if u in have]
            if not opts:
                break
            chosen.append(min(opts, key=lambda u: have[u]["left"] if have[u]["left"] is not None else big))
        else:
            nearest = min((have[u]["left"] for u in chosen if have[u]["left"] is not None), default=big)
            n_soon = sum(1 for u in chosen if have[u]["soon"])
            picked.append((nearest, -n_soon, dish["time_minutes"], i, dish, chosen))
    picked.sort(key=lambda x: x[:4])

    def label(u):
        return foods[u]["name"] + ("（冷凍）" if have[u]["frozen"] else "")

    return [{
        "id": d["id"],
        "name": d["name"],
        "time_minutes": d["time_minutes"],
        "uses": [label(u) for u in sorted(chosen, key=lambda u: have[u]["left"] if have[u]["left"] is not None else big)],
        "soon_uses": [label(u) for u in chosen if have[u]["soon"]],
        "url": COOKPAD_SEARCH + quote(d["search_query"]),
    } for *_, d, chosen in picked[:limit]]


# ---------------- 今週のカード ----------------

def week_range(today):
    start = today - timedelta(days=today.weekday())  # 月曜はじまり
    return start, start + timedelta(days=6)


def weekly_card(inventory, foods, today):
    """守れた品：今週、was_soon の在庫が frozen か used になった品数。
    間に合わなかった品：今週、目安日数を過ぎても used/frozen/gone にならなかった品数（「捨てた」とは書かない）。"""
    start, end = week_range(today)
    saved = missed = 0
    for inv in inventory:
        food = foods[inv["food_id"]]
        if inv["status"] in ("frozen", "used") and inv.get("was_soon"):
            changed = parse_date(inv.get("frozen_at") or inv.get("updated_at") or inv["purchased_at"])
            if start <= changed <= end:
                saved += 1
        elif inv["status"] in ("stocked", "wasted"):
            lim = limit_date(inv, food)
            if lim is not None and lim < today and start <= lim + timedelta(days=1) <= end:
                missed += 1
    return {"week_start": start.isoformat(), "saved_count": saved, "missed_count": missed,
            "has_records": bool(saved or missed)}
