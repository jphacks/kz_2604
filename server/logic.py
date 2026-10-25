"""判定ロジック。

日数・期限・どの食材を救うか・どの料理を提案するかはすべてここで決める。LLMには判断させない。
関数は、印を付ける mark_was_soon 以外は純粋関数（状態を書き換えない）にしてある。
"""
from datetime import date, timedelta
from urllib.parse import quote

# --- 決定事項（CLAUDE.md 第15節を参照） ---
GRACE_DAYS = 0         # 残り日数がこれ以下なら soon（目安の最終日だけ。デモの 10/27 は通知なし、10/28 に問いかけ）
NORMAL_MAX_DAYS = 2    # 残り日数がこれ以下なら normal、超えたら ok
DEFAULT_ASK_TIME = "18:00"
DEFAULT_REMIND_TIME = "21:30"
REASK_MINUTES = 60     # 「あとで」のあとの再通知
MAX_REASK = 1          # 「あとで」での再通知は1回だけ
MAX_SUGGESTIONS = 3    # 料理の提案の最大件数
COOKPAD_SEARCH = "https://cookpad.com/jp/search/"

STATUS_LABEL = {"soon": "早めに", "normal": "ふつう", "ok": "余裕あり", "past": "状態を確かめてください",
                "none": "期限なし"}


def parse_date(s):
    return date.fromisoformat(s[:10])


def tracked(ing):
    return ing.get("track_expiry", True) and ing.get("fridge_days") is not None


def limit_date(inv, ing):
    """冷蔵での推定の期限。パッケージの期限が入力されていればそれを優先する。期限を判定しない食材は None。"""
    if inv.get("expiry_override"):
        return parse_date(inv["expiry_override"])
    if not tracked(ing):
        return None
    return parse_date(inv["purchased_at"]) + timedelta(days=ing["fridge_days"])


def days_left(inv, ing, today):
    lim = limit_date(inv, ing)
    return None if lim is None else (lim - today).days


def freshness(left):
    if left is None:
        return "none"
    if left < 0:
        return "past"
    if left <= GRACE_DAYS:
        return "soon"
    if left <= NORMAL_MAX_DAYS:
        return "normal"
    return "ok"


def describe(inv, ing, today):
    left = days_left(inv, ing, today)
    st = freshness(left)
    lim = limit_date(inv, ing)
    return {
        **inv,
        "name": ing["name"],
        "days_left": left,
        "freshness": st,
        "freshness_label": STATUS_LABEL[st],
        "limit_date": lim.isoformat() if lim else None,
        "source": ing["source"],
    }


def urgency_key(inv, ing, today, order):
    """同順位の決め方：残り日数 → 消費期限の食品を先 → 購入日が古い順 → 食材表の順。"""
    left = days_left(inv, ing, today)
    return (
        left if left is not None else 10 ** 6,
        0 if ing.get("expiry_type") == "consumption" else 1,
        inv["purchased_at"],
        order[ing["id"]],
    )


def soon_items(inventory, ingredients, today, exclude_ids=()):
    order = {ing_id: i for i, ing_id in enumerate(ingredients)}
    items = [
        inv for inv in inventory
        if inv["status"] == "stocked"
        and inv["id"] not in exclude_ids
        and freshness(days_left(inv, ingredients[inv["ingredient_id"]], today)) == "soon"
    ]
    items.sort(key=lambda inv: urgency_key(inv, ingredients[inv["ingredient_id"]], today, order))
    return items


def should_ask(inventory, ingredients, today):
    """問いかけを出す条件：冷蔵の soon の在庫が1つ以上。ホームのカードと通知で同じ条件。"""
    names = []
    for inv in soon_items(inventory, ingredients, today):
        name = ingredients[inv["ingredient_id"]]["name"]
        if name not in names:
            names.append(name)
    return {"should_ask": bool(names), "reason_items": names}


def mark_was_soon(inventory, ingredients, today):
    """一度でも soon になった在庫に印を付ける（「守れた」の判定に使う）。"""
    for inv in soon_items(inventory, ingredients, today):
        inv["was_soon"] = True


def choose_action(inventory, ingredients, today, exclude_ids=()):
    """救済行動は1つだけ。soon のうち冷凍できる最も急ぐ1品を冷凍。なければ最も急ぐ1品の quick_use。"""
    items = soon_items(inventory, ingredients, today, exclude_ids)
    pick = next((inv for inv in items if ingredients[inv["ingredient_id"]]["freezable"]), None)
    kind = "freeze"
    if pick is None:
        pick = next((inv for inv in items if ingredients[inv["ingredient_id"]].get("quick_use")), None)
        kind = "quick_use"
    if pick is None:
        return None
    ing = ingredients[pick["ingredient_id"]]
    verb = ing["freeze_action"] if kind == "freeze" else ing["quick_use"]
    return {
        "inventory_id": pick["id"],
        "ingredient_id": ing["id"],
        "name": ing["name"],
        "kind": kind,
        # 条件の言葉（「残っていたら」）は付けない。代わりに画面に「○○はもう使った」を置く
        "text": f"{ing['name']}を、{verb}",
        "seconds": ing["freeze_seconds"] if kind == "freeze" else ing.get("quick_use_seconds"),
        "note": ing.get("action_note"),
        "storage": f"冷凍したら{ing['freezer_period']}以内に使い切る（農林水産省）"
                   if kind == "freeze" and ing.get("freezer_period") else None,
        "source": ing["source"],
    }


def cook_items(inventory, ingredients, today):
    stocked = [inv for inv in inventory if inv["status"] in ("stocked", "frozen")]
    stocked.sort(key=lambda inv: (inv["purchased_at"], inv["id"]))
    return [{**describe(inv, ingredients[inv["ingredient_id"]], today), "frozen": inv["status"] == "frozen"}
            for inv in stocked]


# ---------------- 冷凍庫 ----------------

def freezer_state(inv, ing, today):
    frozen_on = parse_date(inv.get("frozen_at") or inv.get("updated_at") or inv["purchased_at"])
    frozen_days = (today - frozen_on).days
    left = (ing.get("freezer_days") or 14) - frozen_days
    return frozen_on, frozen_days, left


# ---------------- 冷蔵庫の中身（画面0b） ----------------

def fridge_view(inventory, ingredients, today):
    """冷蔵は期限の目安が近い順。冷凍庫は冷凍した日と日数（冷凍の目安が近いと早めに）。"""
    order = {ing_id: i for i, ing_id in enumerate(ingredients)}
    fridge = []
    for inv in inventory:
        if inv["status"] != "stocked":
            continue
        ing = ingredients[inv["ingredient_id"]]
        d = describe(inv, ing, today)
        fridge.append({
            "inventory_id": inv["id"],
            "ingredient_id": ing["id"],
            "name": ing["name"],
            "purchased_at": inv["purchased_at"],
            "state": d["freshness"],
            "state_label": d["freshness_label"],
            "days_left": d["days_left"],
            "_key": urgency_key(inv, ing, today, order),
        })
    fridge.sort(key=lambda x: x.pop("_key"))

    freezer = []
    for inv in inventory:
        if inv["status"] != "frozen":
            continue
        ing = ingredients[inv["ingredient_id"]]
        frozen_on, frozen_days, left = freezer_state(inv, ing, today)
        st = freshness(left)
        freezer.append({
            "inventory_id": inv["id"],
            "name": ing["name"],
            "frozen_at": frozen_on.isoformat(),
            "frozen_days": frozen_days,
            "days_left": left,
            "state": st,
            "state_label": STATUS_LABEL[st],
        })
    freezer.sort(key=lambda x: x["days_left"])
    return {"fridge": fridge, "freezer": freezer}


# ---------------- 料理の提案（画面0b・6） ----------------

def suggestions(inventory, ingredients, dishes, today, limit=MAX_SUGGESTIONS):
    """使う食材がすべて冷蔵か冷凍にあり、うち1つ以上が soon の料理。
    冷凍した食材を使う料理を先に、次に soon を多く使う順（同じなら調理時間が短い順）。
    uses の "naganegi|negi" は「どちらか一方があればよい」。"""
    have = {}  # ingredient_id -> {"state": ..., "frozen": bool}
    for inv in inventory:
        ing = ingredients[inv["ingredient_id"]]
        if inv["status"] == "stocked":
            st = freshness(days_left(inv, ing, today))
            if st == "past":
                continue  # 目安を過ぎたものを使う料理は勧めない
            frozen = False
        elif inv["status"] == "frozen":
            st = freshness(freezer_state(inv, ing, today)[2])
            frozen = True
        else:
            continue
        cur = have.get(ing["id"])
        if cur is None or st == "soon" or (frozen and not cur["frozen"]):
            have[ing["id"]] = {"state": st if cur is None or st == "soon" else cur["state"],
                               "frozen": frozen or (cur or {}).get("frozen", False)}

    picked = []
    for i, dish in enumerate(dishes):
        chosen = []
        for use in dish["uses"]:
            ok = next((u for u in use.split("|") if u in have), None)
            if ok is None:
                break
            chosen.append(ok)
        else:
            n_soon = sum(1 for u in chosen if have[u]["state"] == "soon")
            if n_soon == 0:
                continue
            n_frozen = sum(1 for u in chosen if have[u]["frozen"])
            picked.append((-min(n_frozen, 1), -n_soon, dish["time_minutes"], i, dish, chosen))
    picked.sort(key=lambda x: x[:4])

    def label(u):
        return ingredients[u]["name"] + ("（冷凍）" if have[u]["frozen"] else "")

    return [{
        "id": d["id"],
        "name": d["name"],
        "time_minutes": d["time_minutes"],
        "uses": [label(u) for u in chosen],
        "soon_uses": [label(u) for u in chosen if have[u]["state"] == "soon"],
        "url": COOKPAD_SEARCH + quote(d["search_query"]),
    } for *_, d, chosen in picked[:limit]]


# ---------------- 今週のカード（ホーム・5d） ----------------

def week_range(today):
    start = today - timedelta(days=today.weekday())  # 月曜はじまり
    return start, start + timedelta(days=6)


def weekly_card(inventory, ingredients, today):
    """守れた品：今週、was_soon の在庫が frozen か used になった品数。
    間に合わなかった品：今週、目安日数を過ぎても used/frozen/gone にならなかった品数（「捨てた」とは書かない）。"""
    start, end = week_range(today)

    def in_week(d):
        return d is not None and start <= d <= end

    saved = missed = 0
    for inv in inventory:
        ing = ingredients[inv["ingredient_id"]]
        if inv["status"] in ("frozen", "used") and inv.get("was_soon"):
            changed = parse_date(inv.get("frozen_at") or inv.get("updated_at") or inv["purchased_at"])
            if in_week(changed):
                saved += 1
        elif inv["status"] in ("stocked", "wasted") and tracked(ing):
            lim = limit_date(inv, ing)
            if lim is not None and lim < today and in_week(lim + timedelta(days=1)):
                missed += 1
    return {"week_start": start.isoformat(), "saved_count": saved, "missed_count": missed,
            "has_records": bool(saved or missed)}
