"""判定ロジック。

日数・期限・どの食材を救うか・どの料理を提案するかはすべてここで決める。LLMには判断させない。
関数はすべて純粋関数（状態を書き換えない）にしてある。
"""
from datetime import date, timedelta
from urllib.parse import quote

# --- 決定事項（CLAUDE.md 13. を参照） ---
GRACE_DAYS = 1        # 残り日数がこれ以下なら soon
NORMAL_MAX_DAYS = 3   # 残り日数がこれ以下なら normal、超えたら ok
ASK_HOUR = 18         # 問いかけの時刻
DEADLINE_HOUR = 21    # 返事の締め切り。過ぎたら skip
REMIND_TIME = (21, 30)  # 「無理」の日の冷凍のリマインド
REMIND_MIN_GAP_MINUTES = 30  # 遅く「無理」と答えた日は、答えてから最低これだけあけてリマインド
REASK_MINUTES = 60    # 「まだ」のあとの再通知
MAX_REASK = 2         # 「まだ」での再通知の上限回数
MAX_SUGGESTIONS = 3   # 料理の提案の最大件数
FREEZER_USE_DAYS = 14  # 冷凍したものの目安（農林水産省「2〜3週間」の短い方）
COOKPAD_SEARCH = "https://cookpad.com/jp/search/"

STATUS_LABEL = {"soon": "早めに", "normal": "ふつう", "ok": "余裕あり", "past": "状態を確かめてください"}


def parse_date(s):
    return date.fromisoformat(s)


def limit_date(inv, ing):
    """推定の期限。パッケージの期限が入力されていればそれを優先する。"""
    if inv.get("expiry_override"):
        return parse_date(inv["expiry_override"])
    return parse_date(inv["purchased_at"]) + timedelta(days=ing["fridge_days"])


def days_left(inv, ing, today):
    return (limit_date(inv, ing) - today).days


def freshness(left):
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
    return {
        **inv,
        "name": ing["name"],
        "days_left": left,
        "freshness": st,
        "freshness_label": STATUS_LABEL[st],
        "limit_date": limit_date(inv, ing).isoformat(),
        "source": ing["source"],
    }


def urgency_key(inv, ing, today, order):
    """同順位の決め方：残り日数 → 消費期限の食品を先 → 購入日が古い順 → 食材表の順。"""
    return (
        days_left(inv, ing, today),
        0 if ing["expiry_type"] == "consumption" else 1,
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
    items = soon_items(inventory, ingredients, today)
    names = []
    for inv in items:
        name = ingredients[inv["ingredient_id"]]["name"]
        if name not in names:
            names.append(name)
    return {"should_ask": bool(items), "reason_items": names}


def _rescue_text(ing):
    """救済行動の文。必ず条件付きにする（在庫がずれていても嘘にならないように）。"""
    if ing["freezable"] and ing["freeze_action"]:
        return f"{ing['name']}が残っていたら、{ing['freeze_action']}", ing["freeze_seconds"], "freeze"
    if ing["quick_use"]:
        return f"{ing['name']}が残っていたら、{ing['quick_use']}", ing["quick_use_seconds"], "quick_use"
    return None


def choose_action(inventory, ingredients, today, exclude_ids=()):
    """最も急ぐ1品の救済行動と、次に急ぐ1品の「明日の朝の一手」を返す。"""
    action = None
    next_tip = None
    for inv in soon_items(inventory, ingredients, today, exclude_ids):
        ing = ingredients[inv["ingredient_id"]]
        if action is None:
            r = _rescue_text(ing)
            if r is None:
                continue
            text, seconds, kind = r
            action = {
                "inventory_id": inv["id"],
                "ingredient_id": ing["id"],
                "name": ing["name"],
                "kind": kind,
                "text": text,
                "seconds": seconds,
                "note": ing.get("action_note"),
                # 冷凍したあとの目安（農林水産省：ホームフリージングは2〜3週間以内に使い切る）
                "storage": f"冷凍したら{ing['freezer_period']}以内に使い切る（農林水産省）"
                           if kind == "freeze" and ing.get("freezer_period") else None,
                "source": ing["source"],
            }
        elif ing["id"] != action["ingredient_id"] and ing["quick_use"]:
            next_tip = f"{ing['name']}が残っていたら、{ing['quick_use']}"
            break
    return action, next_tip


def cook_items(inventory, ingredients, today):
    stocked = [inv for inv in inventory if inv["status"] == "stocked"]
    stocked.sort(key=lambda inv: (inv["purchased_at"], inv["id"]))
    return [describe(inv, ingredients[inv["ingredient_id"]], today) for inv in stocked]


def is_wasted(inv, ing, today):
    """目安を過ぎても used/frozen/gone にならなかった在庫を wasted とみなす。"""
    if inv["status"] == "wasted":
        return True
    return inv["status"] == "stocked" and days_left(inv, ing, today) < 0


def week_range(today):
    start = today - timedelta(days=today.weekday())  # 月曜はじまり
    return start, start + timedelta(days=6)


def weekly(inventory, ingredients, asks, freeze_checks, today):
    start, end = week_range(today)

    def in_week(d):
        return start <= parse_date(d) <= end

    week_inv = [inv for inv in inventory if in_week(inv["purchased_at"])]
    wasted_inv = [inv for inv in week_inv if is_wasted(inv, ingredients[inv["ingredient_id"]], today)]
    wasted = [ingredients[inv["ingredient_id"]]["name"] for inv in wasted_inv]
    saved = [{"name": ingredients[inv["ingredient_id"]]["name"], "how": inv["status"]}
             for inv in week_inv if inv["status"] in ("frozen", "used")]
    protected = [d for d, a in asks.items()
                 if in_week(d) and a.get("answer") == "skip"
                 and freeze_checks.get(d, {}).get("status") == "done"]

    # 間に合わなかった食材の説明（罪悪感ではなく、次にいつ声をかけるかを伝える）
    wasted_detail = []
    for inv in wasted_inv:
        ing = ingredients[inv["ingredient_id"]]
        if any(w["name"] == ing["name"] for w in wasted_detail):
            continue
        ask_from = max(0, ing["fridge_days"] - GRACE_DAYS)
        when = {0: "買った当日", 1: "買った次の日"}.get(ask_from, f"買って{ask_from}日目")
        short = "日持ちが短い食材です。" if ing["fridge_days"] <= 2 else ""
        wasted_detail.append({
            "name": ing["name"],
            "message": f"{ing['name']}は{short}目安は{ing['fridge_days']}日なので、{when}から声をかけます。",
        })

    return {
        "week_start": start.isoformat(),
        "week_end": (start + timedelta(days=5)).isoformat(),  # 土曜のふり返りなので表示は土曜まで
        "wasted": wasted,
        "wasted_detail": wasted_detail,
        "saved": saved,
        "saved_names": list(dict.fromkeys(s["name"] for s in saved)),
        "protected_days": len(protected),
    }


# ---------------- 冷蔵庫の中身（画面0b） ----------------

def fridge_view(inventory, ingredients, today):
    """冷蔵にあるものを期限の目安が近い順に。冷凍したものは別の欄に。"""
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
            "_key": urgency_key(inv, ing, today, order),  # 同じ日数なら消費期限の食品 → 購入日 → 食材表の順
        })
    fridge.sort(key=lambda x: x.pop("_key"))

    freezer = []
    for inv in inventory:
        if inv["status"] != "frozen":
            continue
        ing = ingredients[inv["ingredient_id"]]
        frozen_on = (inv.get("updated_at") or inv["purchased_at"])[:10]
        freezer.append({
            "inventory_id": inv["id"],
            "name": ing["name"],
            "frozen_at": frozen_on,
            "use_by": (parse_date(frozen_on) + timedelta(days=FREEZER_USE_DAYS)).isoformat(),
        })
    freezer.sort(key=lambda x: x["use_by"])
    return {"fridge": fridge, "freezer": freezer}


# ---------------- 料理の提案（画面0b・6） ----------------

def suggestions(inventory, ingredients, dishes, today, limit=MAX_SUGGESTIONS):
    """使う食材がすべて冷蔵にあり、うち1つ以上が soon の料理を、soon の食材を多く使う順に。"""
    states = {}
    for inv in inventory:
        if inv["status"] != "stocked":
            continue
        st = freshness(days_left(inv, ingredients[inv["ingredient_id"]], today))
        if st == "past":
            continue  # 目安を過ぎたものを使う料理は勧めない
        prev = states.get(inv["ingredient_id"])
        states[inv["ingredient_id"]] = "soon" if "soon" in (prev, st) else st

    picked = []
    for i, dish in enumerate(dishes):
        if not all(u in states for u in dish["uses"]):
            continue
        n_soon = sum(1 for u in dish["uses"] if states[u] == "soon")
        if n_soon == 0:
            continue
        picked.append((-n_soon, dish["time_minutes"], i, dish))
    picked.sort(key=lambda x: x[:3])
    return [{
        "id": d["id"],
        "name": d["name"],
        "time_minutes": d["time_minutes"],
        "uses": [ingredients[u]["name"] for u in d["uses"]],
        "soon_uses": [ingredients[u]["name"] for u in d["uses"] if states[u] == "soon"],
        "url": COOKPAD_SEARCH + quote(d["search_query"]),
    } for *_, d in picked[:limit]]


# ---------------- 今週のカード（ホーム） ----------------

def weekly_card(inventory, ingredients, asks, freeze_checks, today):
    w = weekly(inventory, ingredients, asks, freeze_checks, today)
    return {
        "week_start": w["week_start"],
        "wasted_count": len(w["wasted"]),
        "saved_count": len(w["saved"]),
        "protected_days": w["protected_days"],
    }
