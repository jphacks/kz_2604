"""APIサーバー（冷蔵コーチ）。Python標準ライブラリで動く（Webプッシュに pywebpush、レシートに google-genai）。

    python -m server.app            # http://localhost:8000 （同じWi-Fiの端末からは http://<PCのIP>:8000）

- データはすべて端末ごとのID（ヘッダー X-Device-Id）で分ける。IDがないリクエストは 400
- 時刻は端末ごとの時計で進む。ふだんは実際の時刻。開発者メニュー（/api/debug/advance・reset）で、その端末だけ進められる
- 問いかけ・締め切り・確認・再通知はすべてサーバーが処理する（15秒ごとに確認）
- 端末ごとに「今日の状態」（today_state）を持つ：unanswered / skip_pending / done / cook / none
"""
import csv
import io
import json
import mimetypes
import os
import re
import socket
import ssl
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import logic
from .ocr_match import NOT_FOOD, lines_from_ocr_text, match_lines, normalize, search
from .push import Push
from .receipt import ReceiptError, read_receipt
from .receipt import engine as receipt_engine

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
STATE_FILE = ROOT / "var" / "state.json"
STATE_VERSION = 4
JST = timezone(timedelta(hours=9))
DEMO_START = datetime(2026, 10, 26, 19, 0, tzinfo=JST)  # 「デモを初期化」で合わせる時刻（月曜、買い物から帰ったところ）
RECEIPT_DATE_MAX_AGE_DAYS = 14  # レシートの日付がこれより古い・未来なら、読み違いとみなして今日にする
DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

LOCK = threading.RLock()
SCHEDULER_SECONDS = 15
PUSH = Push(ROOT / "var")


def load_env():
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


def load_json(rel):
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


FOODS = {f["id"]: f for f in load_json("data/foods.json")}
CATEGORIES = {c["id"]: c for c in load_json("data/categories.json")}
DISHES = load_json("data/dishes.json")
PANTRY = load_json("data/excluded.json")  # 食材表から取り除いた「冷蔵・冷凍しないもの」（照合で見分けるのに使う）


def food_source(food):
    return food.get("source") or CATEGORIES.get(food["category"], {}).get("source", "")


class ApiError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------- 状態と時計 ----------------

def new_today(d):
    return {
        "date": d.isoformat(),
        "state": None,          # None の間は食材から決める（unanswered か none）。答えたら固定する
        "answer": None,         # skip / cook
        "answered_by": None,    # user / timeout
        "ask_sent": False,
        "rescue": None,         # 救済行動
        "note": None,           # 「もやしは冷凍に向かないので…」
        "remind_at": None,
        "remind_sent": False,
        "reask_at": None,
        "reask_count": 0,
        "freeze_pending": False,  # 確認の通知を送って、返事を待っている
        "cook_recorded": False,
        "no_deadline": False,   # 開発者メニューでリセットした日は、締め切りで「今日は無理」にしない
    }


def new_device(today):
    return {
        "inventory": [],
        "next_inv": 1,
        "settings": dict(logic.DEFAULT_SETTINGS),
        "today": new_today(today),
        "pending_cook_record": None,  # 「あとで記録する」を選んだ日。翌日のホームで1回だけ聞く
        "push_subscriptions": [],
        "learned": {"names": {}, "jans": {}},
        "clock_offset": 0,  # 秒。0 なら実際の時刻。開発者メニューのデモ操作で、この端末だけずらす
    }


def initial_state():
    return {"version": STATE_VERSION, "devices": {}, "records": []}


def drop_unknown_foods(st):
    """食材表から取り除いた品目（tools/exclude.py）を指す在庫は、読み込むときに消す。"""
    for dev in st["devices"].values():
        dev["inventory"] = [inv for inv in dev["inventory"] if inv["food_id"] in FOODS]
        if dev["today"].get("rescue") and dev["today"]["rescue"]["food_id"] not in FOODS:
            dev["today"] = new_today(logic.parse_date(dev["today"]["date"]))
    return st


def migrate_v3(st):
    """v3（全端末で1つの時計）→ v4（端末ごとの時計）。在庫などはそのまま残し、時計は実際の時刻に戻す。
    v3 では読み違えたレシートの日付（2020年など）がそのまま入っていたので、明らかに古いものは今日にする。"""
    today = datetime.now(JST).date()
    for dev in st["devices"].values():
        dev["clock_offset"] = 0
        for inv in dev["inventory"]:
            if (today - logic.parse_date(inv["purchased_at"])).days > 365:
                inv["purchased_at"] = today.isoformat()
    st.pop("clock_offset", None)
    st["version"] = STATE_VERSION
    return st


def read_state():
    if STATE_FILE.exists():
        st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if st.get("version") == 3:
            st = migrate_v3(st)
        if st.get("version") == STATE_VERSION:
            return drop_unknown_foods(st)
        # 前の形式（食材表が変わる前）のファイルは使わない（新しく始める）
    st = initial_state()
    write_state(st)
    return st


def write_state(st):
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def now(dev):
    """その端末の時刻。ふだんは実際の時刻（clock_offset が 0）。"""
    return datetime.now(JST) + timedelta(seconds=dev.get("clock_offset", 0))


def at_time(d, hhmm):
    h, m = map(int, hhmm.split(":"))
    return datetime(d.year, d.month, d.day, h, m, tzinfo=JST)


def emit(st, dev_id, message):
    """通知を1件積む。状態を書き込んだあとで、その端末の購読にだけプッシュする。"""
    st.setdefault("_events", []).append({"device_id": dev_id, **message})


def record(st, dev_id, type_, **fields):
    """試用の記録（実証用）。type：ask_sent / answered / remind_sent / remind_answered / rescue"""
    st["records"].append({"device_id": dev_id, "type": type_, "at": now(st["devices"].get(dev_id, {})).isoformat(), **fields})


def notify_enabled(dev):
    return bool(dev["settings"].get("notify", True) and dev["push_subscriptions"])


def ensure_today(dev, today):
    if dev["today"]["date"] != today.isoformat():
        dev["today"] = new_today(today)


def today_state(dev, today):
    t = dev["today"]
    if t["state"]:
        return t["state"]
    return "unanswered" if logic.should_ask(dev["inventory"], FOODS, today) else "none"


# ---------------- 「今日は無理」と確認 ----------------

def apply_skip(st, dev, d, by):
    """「今日は無理」（または締め切りまで返事なし）。救済行動を1つ決めて skip_pending にする。
    救済行動にできる食材がなければ、行動は出さず done（確認の通知も送らない）。"""
    t = dev["today"]
    action, note = logic.choose_rescue(dev["inventory"], FOODS, d)
    t.update({"answer": "skip", "answered_by": by, "rescue": action, "note": note})
    if action:
        t["state"] = "skip_pending"
        remind = at_time(d, dev["settings"]["remind_time"])
        t["remind_at"] = remind.isoformat() if now(dev) < remind else None
    else:
        t["state"] = "done"
        t["remind_at"] = None
    return action, note


def remind_message(dev):
    a = dev["today"]["rescue"]
    return {"kind": "remind", "date": dev["today"]["date"],
            "title": f"{a['text']}（{a['seconds']}秒）", "body": "入れたら、ボタンを押すだけでOK。", "url": "/?open=remind"}


def finish_rescue(st, dev, status):
    """救済行動の対象の在庫を frozen / used / gone にして、今日はおしまいにする。"""
    t = dev["today"]
    inv = find_inv(dev, t["rescue"]["inventory_id"])
    set_status(dev, inv, status)
    t.update({"state": "done", "result": status, "remind_at": None, "reask_at": None, "freeze_pending": False})
    return inv


def tick_device(st, dev_id, dev, t):
    today = t.date()
    ensure_today(dev, today)
    logic.mark_was_soon(dev["inventory"], FOODS, today)
    td, s = dev["today"], dev["settings"]
    state = today_state(dev, today)

    # 問いかけ（早めの食材があって、まだ答えていない日だけ）
    if state == "unanswered" and not td["ask_sent"] and at_time(today, s["ask_time"]) <= t < at_time(today, s["deadline_time"]):
        td["ask_sent"] = True
        if notify_enabled(dev):
            names = "・".join(logic.soon_names(dev["inventory"], FOODS, today))
            emit(st, dev_id, {"kind": "ask", "date": today.isoformat(), "title": "今日、作れそう？",
                              "body": f"{names}を、そろそろ使いたい日です。返事しなくても大丈夫。", "url": "/?open=ask"})
            record(st, dev_id, "ask_sent", date=today.isoformat())

    # 締め切りまでに返事がなければ「今日は無理」
    if state == "unanswered" and not td.get("no_deadline") and t >= at_time(today, s["deadline_time"]):
        apply_skip(st, dev, today, "timeout")

    # 確認（skip_pending の日だけ）と、「あとで」の1時間後の再通知（1回だけ）
    if td["state"] == "skip_pending":
        due_remind = td["remind_at"] and not td["remind_sent"] and t >= datetime.fromisoformat(td["remind_at"])
        due_reask = td["reask_at"] and t >= datetime.fromisoformat(td["reask_at"])
        if due_remind or due_reask:
            if due_remind:
                td["remind_sent"] = True
            td["reask_at"] = None
            td["freeze_pending"] = True
            if notify_enabled(dev):
                emit(st, dev_id, remind_message(dev))
                record(st, dev_id, "remind_sent", date=td["date"], again=bool(due_reask))


def tick(st):
    for dev_id, dev in st["devices"].items():
        tick_device(st, dev_id, dev, now(dev))


def find_inv(dev, inv_id):
    for inv in dev["inventory"]:
        if inv["id"] == inv_id:
            return inv
    raise ApiError(404, f"在庫 {inv_id} がありません")


def set_status(dev, inv, status):
    inv["status"] = status
    inv["updated_at"] = now(dev).isoformat()
    if status == "frozen":
        inv["frozen_at"] = now(dev).date().isoformat()


def weekly(st, dev):
    return logic.weekly_card(dev["inventory"], FOODS, now(dev).date())


def frozen_until(dev, food_id):
    days = FOODS[food_id].get("freezer_days") or logic.FREEZER_DAYS
    return (now(dev).date() + timedelta(days=days)).isoformat()


# ---------------- API ----------------

def check_purchase_date(read_date, today):
    """レシートから読んだ購入日を確かめる。読めない・未来・RECEIPT_DATE_MAX_AGE_DAYS より古いときは、
    読み違いとみなして今日にする（古い日付のままだと、買ったばかりの肉が「状態を確認してください」になる）。"""
    if not read_date:
        return today.isoformat(), "レシートの日付が読めなかったので、今日の日付にしました。違っていたら変えてください。"
    try:
        d = logic.parse_date(read_date)
    except ValueError:
        return today.isoformat(), "レシートの日付が読めなかったので、今日の日付にしました。違っていたら変えてください。"
    if 0 <= (today - d).days <= RECEIPT_DATE_MAX_AGE_DAYS:
        return d.isoformat(), None
    return today.isoformat(), (f"レシートの日付が {d.year}/{d.month}/{d.day} と読めましたが、読み違いかもしれないので"
                               "今日の日付にしました。違っていたら変えてください。")


def api_receipts(ctx, body):
    st, dev = ctx["st"], ctx["dev"]
    today = now(dev).date()
    if body.get("use_fixture"):
        read, engine, extra = load_json("fixtures/receipt_demo.json"), "fixture", {}
    elif body.get("ocr_text") is not None:
        read = lines_from_ocr_text(body["ocr_text"])  # 予備：端末の Tesseract.js で読んだ文字
        if not read["lines"]:
            raise ApiError(422, "レシートの品目を読み取れませんでした")
        engine, extra = "tesseract", {"transcript": body["ocr_text"]}
    else:
        images = body.get("images") or (
            [{"data": body["image_base64"], "media_type": body.get("media_type", "image/jpeg")}]
            if body.get("image_base64") else [])
        if not images:
            raise ApiError(400, "image_base64 か images がありません")
        try:
            read = read_receipt(images)  # 主：LLM で文字起こし（照合はコード）
        except ReceiptError as e:
            raise ApiError(502, str(e))
        engine = "llm"
        extra = {"provider": read["engine"], "model": read["model"],
                 "transcript": "\n".join(l["name"] + (f"  [{l['jan']}]" if l["jan"] else "") for l in read["lines"])}
    purchased_at, date_note = check_purchase_date(read.get("purchased_at"), today)
    result = match_lines(read["lines"], FOODS, dev["learned"], PANTRY)
    for m in result["matched"]:
        food = FOODS[m["food_id"]]
        left = logic.days_left({"purchased_at": purchased_at}, food, today)
        m.update({"name": food["name"], "soon": logic.is_soon(left), **logic.state_fields(left, food)})
    for u in result["unmatched"]:
        u["candidates"] = [{"id": c, "name": FOODS[c]["name"]} for c in u["candidates"]]
    return {"purchased_at": purchased_at, "date_note": date_note, "read_date": read.get("purchased_at"),
            **result, "engine": engine, **extra}


def api_preview(ctx, body):
    """画面2で選んだ食材・購入日について「早めに」かどうかを返す。"""
    today = now(ctx["dev"]).date()
    out = []
    for fid in body.get("food_ids", []):
        food = FOODS.get(fid)
        if not food:
            raise ApiError(400, f"食材表にないid: {fid}")
        left = logic.days_left({"purchased_at": body["purchased_at"]}, food, today)
        out.append({"food_id": fid, "name": food["name"], "soon": logic.is_soon(left), **logic.state_fields(left, food)})
    return {"items": out}


def api_confirm(ctx, body):
    """チェックした品目を在庫に登録し、自分で選んだ行（learn: true）の対応を覚える（「食材ではない」も）。"""
    st, dev = ctx["st"], ctx["dev"]
    added = []
    for it in body.get("items", []):
        fid = it.get("food_id")
        target = NOT_FOOD if it.get("not_food") else fid
        if not it.get("not_food") and fid not in FOODS:
            raise ApiError(400, f"食材表にないid: {fid}")
        if it.get("learn"):
            if it.get("raw") and normalize(it["raw"]):
                dev["learned"]["names"][normalize(it["raw"])] = target
            if it.get("jan"):
                dev["learned"]["jans"][it["jan"]] = target
        if it.get("not_food"):
            continue
        inv = {"id": f"inv_{dev['next_inv']:03d}", "food_id": fid, "raw": it.get("raw", ""),
               "purchased_at": body["purchased_at"], "frozen_at": None,
               "expiry_override": it.get("expiry_override") or None,
               "status": "stocked", "was_soon": False, "updated_at": now(dev).isoformat()}
        dev["next_inv"] += 1
        dev["inventory"].append(inv)
        added.append(inv["id"])
    logic.mark_was_soon(dev["inventory"], FOODS, now(dev).date())
    return {"added": added}


def api_foods_search(ctx, body):
    return {"foods": search(body.get("q", ""), FOODS, limit=10)}


def bell(dev, state):
    s, td = dev["settings"], dev["today"]
    on = notify_enabled(dev)
    if state == "unanswered":
        label = f"今日は{s['ask_time']}に聞きます"
    elif state == "skip_pending":
        if td["reask_at"]:
            label = f"{td['reask_at'][11:16]}にもう一度だけ聞きます"
        elif td["remind_at"] and not td["remind_sent"]:
            label = f"{td['remind_at'][11:16]}に1回だけ聞きます"
        else:
            label = "今日の通知はおしまい"
    elif state in ("done", "cook"):
        label = "今日の通知はおしまい"
    else:
        label = "今日は通知なし"
    if not on and state in ("unanswered", "skip_pending") and label != "今日の通知はおしまい":
        label = "通知はオフ（開いたときに聞きます）"  # 許可していないのに「18:00に聞きます」と出さない
    return {"label": label, "notify_enabled": on, "quiet": label in ("今日は通知なし", "今日の通知はおしまい")}


def api_home(ctx, body):
    st, dev = ctx["st"], ctx["dev"]
    t = now(dev)
    today = t.date()
    td = dev["today"]
    state = today_state(dev, today)
    card = {"items": logic.soon_names(dev["inventory"], FOODS, today)}
    if td["rescue"]:
        card["action"] = td["rescue"]
        card["until"] = frozen_until(dev, td["rescue"]["food_id"])
    card.update({"note": td["note"], "result": td.get("result"), "cook_recorded": td["cook_recorded"],
                 "deadline": dev["settings"]["deadline_time"], "remind_time": dev["settings"]["remind_time"]})
    pending = dev["pending_cook_record"]
    return {
        "now": t.isoformat(),
        "today_state": state,
        "card": card,
        "bell": bell(dev, state),
        "near": logic.near_items(dev["inventory"], FOODS, today, 3),
        "dishes": [] if state in ("skip_pending", "done")
                  else logic.suggestions(dev["inventory"], FOODS, DISHES, today, logic.HOME_DISHES),
        "weekly": weekly(st, dev),
        "yesterday_cook": bool(pending and pending < today.isoformat()),
        "freeze_pending": td["freeze_pending"],
    }


def api_inventory(ctx, body):
    return logic.fridge_view(ctx["dev"]["inventory"], FOODS, now(ctx["dev"]).date())


def api_inventory_remove(ctx, body):
    reason = body.get("reason")
    if reason not in ("used", "gone"):
        raise ApiError(400, "reason は used か gone")
    for inv_id in body.get("inventory_ids", []):
        set_status(ctx["st"], find_inv(ctx["dev"], inv_id), reason)
    return {"ok": True}


def api_suggestions(ctx, body):
    limit = int(body.get("limit") or logic.LIST_DISHES)
    return {"dishes": logic.suggestions(ctx["dev"]["inventory"], FOODS, DISHES, now(ctx["dev"]).date(), limit)}


def api_answer(ctx, body):
    st, dev, dev_id = ctx["st"], ctx["dev"], ctx["dev_id"]
    today = now(dev).date()
    answer = body.get("answer")
    if answer not in ("skip", "cook"):
        raise ApiError(400, "answer は skip か cook")
    td = dev["today"]
    if td["answer"] and td["answered_by"] == "user":
        raise ApiError(409, "今日はもう答えています")  # その日に答えたら、その日はもう聞かない
    record(st, dev_id, "answered", date=today.isoformat(), value=answer,
           taps=int(body.get("taps") or 1), via=body.get("via") or "screen")
    if answer == "skip":
        action, note = apply_skip(st, dev, today, "user")
        return {"action": action, "not_freezable_note": note, "remind_at": td["remind_at"]}
    td.update({"answer": "cook", "answered_by": "user", "state": "cook"})
    return {"items": logic.cook_items(dev["inventory"], FOODS, today)}


def api_rescue(ctx, body):
    """画面4・ホームのカード：done_now（入れた・今やった）／later（あとでやる）／already_used（もう使った）"""
    st, dev, dev_id = ctx["st"], ctx["dev"], ctx["dev_id"]
    td = dev["today"]
    if not td["rescue"] or td["state"] not in ("skip_pending",):
        raise ApiError(409, "今日の救済行動はありません")
    result = body.get("result")
    if result not in ("done_now", "later", "already_used"):
        raise ApiError(400, "result は done_now / later / already_used")
    record(st, dev_id, "rescue", date=td["date"], value=result, taps=1, via=body.get("via") or "screen")
    if result == "done_now":
        inv = finish_rescue(st, dev, "frozen")
        return {"until": frozen_until(dev, inv["food_id"]), "weekly": weekly(st, dev), "name": FOODS[inv["food_id"]]["name"]}
    if result == "already_used":
        finish_rescue(st, dev, "used")
        return {"weekly": weekly(st, dev)}
    return {"remind_at": td["remind_at"]}  # skip_pending のまま、確認の時刻に1回だけ聞く


def api_freeze_check(ctx, body):
    st, dev, dev_id = ctx["st"], ctx["dev"], ctx["dev_id"]
    td = dev["today"]
    if td["state"] != "skip_pending" or not td["rescue"]:
        raise ApiError(409, "確認待ちの「冷凍庫に入れた？」はありません")
    answer = body.get("answer")
    if answer not in ("done", "later", "gone"):
        raise ApiError(400, "answer は done / later / gone")
    record(st, dev_id, "remind_answered", date=td["date"], value=answer,
           taps=int(body.get("taps") or 1), via=body.get("via") or "screen")
    if answer == "done":
        inv = finish_rescue(st, dev, "frozen")
        return {"until": frozen_until(dev, inv["food_id"]), "weekly": weekly(st, dev), "name": FOODS[inv["food_id"]]["name"]}
    if answer == "gone":
        finish_rescue(st, dev, "gone")
        return {"weekly": weekly(st, dev)}
    td["freeze_pending"] = False
    reask = now(dev) + timedelta(minutes=logic.REASK_MINUTES)
    last = at_time(now(dev).date(), logic.LAST_REASK)
    if td["reask_count"] >= logic.MAX_REASK or reask > last:
        return {"reask_at": None}  # 2回目、または23:59を過ぎる。今日はもう聞かない（ホームのカードからは押せる）
    td["reask_at"] = reask.isoformat()
    td["reask_count"] += 1
    return {"reask_at": td["reask_at"]}


def api_cook_items(ctx, body):
    return {"items": logic.cook_items(ctx["dev"]["inventory"], FOODS, now(ctx["dev"]).date())}


def api_cook_record(ctx, body):
    """使った食材を記録（used にする）。翌日の「昨日、使った食材は？」から来たときも同じ。"""
    st, dev = ctx["st"], ctx["dev"]
    for inv_id in body.get("inventory_ids", []):
        set_status(dev, find_inv(dev, inv_id), "used")
    if body.get("yesterday"):
        dev["pending_cook_record"] = None
    else:
        dev["today"]["cook_recorded"] = True
    return {"weekly": weekly(st, dev)}


def api_cook_record_later(ctx, body):
    dev = ctx["dev"]
    dev["pending_cook_record"] = dev["today"]["date"]  # 翌日のホームで1回だけ聞く
    return {"ok": True}


def api_cook_record_skip(ctx, body):
    ctx["dev"]["pending_cook_record"] = None
    return {"ok": True}


def api_settings_get(ctx, body):
    return ctx["dev"]["settings"]


def api_settings_put(ctx, body):
    s = {k: body.get(k) for k in ("ask_time", "deadline_time", "remind_time")}
    if not all(TIME_RE.match(v or "") for v in s.values()):
        raise ApiError(400, "時刻は HH:MM の形で指定してください")
    if not (s["ask_time"] < s["deadline_time"] < s["remind_time"]):
        raise ApiError(400, "問いかけ → 返事の締め切り → できたかの確認 の順になるようにしてください")
    s["notify"] = bool(body.get("notify", True))
    dev = ctx["dev"]
    dev["settings"] = s
    td = dev["today"]
    if td["state"] == "skip_pending" and not td["remind_sent"]:  # 今日の確認の時刻も合わせる
        remind = at_time(logic.parse_date(td["date"]), s["remind_time"])
        td["remind_at"] = remind.isoformat() if now(ctx["dev"]) < remind else None
    return s


def api_ingredients(ctx, body):
    """料理表の食材など、よく使う品目（core）。手入力は /api/foods/search を使う。"""
    return {"items": [{"id": f["id"], "name": f["name"]} for f in FOODS.values() if f.get("core")]}


def api_sources(ctx, body):
    """目安と出典：分類ごとの冷蔵の目安と、出典の一覧。"""
    cats = [{"id": c["id"], "label": c["label"], "fridge_days": c["fridge_days"], "freezable": c["freezable"],
             "track_expiry": c["track_expiry"], "source": c.get("source", ""),
             "examples": [f["name"] for f in FOODS.values() if f["category"] == c["id"]][:4]}
            for c in CATEGORIES.values()]
    return {"categories": cats, "freezer_days": logic.FREEZER_DAYS, "sources": load_json("data/sources.json")}


def api_config(ctx, body):
    return {
        "receipt_engine": receipt_engine(),  # "gemini" / "claude" / None（キーなし）
        "push_available": PUSH.available,
        "vapid_public_key": PUSH.public_key,
        "push_registered": len(ctx["dev"]["push_subscriptions"]),
    }


def api_push_subscribe(ctx, body):
    sub = body.get("subscription") or {}
    if not sub.get("endpoint") or not sub.get("keys"):
        raise ApiError(400, "subscription がありません")
    subs = [s for s in ctx["dev"]["push_subscriptions"] if s["endpoint"] != sub["endpoint"]]
    subs.append({"endpoint": sub["endpoint"], "keys": sub["keys"]})
    ctx["dev"]["push_subscriptions"] = subs
    return {"registered": len(subs)}


def api_push_unsubscribe(ctx, body):
    dev = ctx["dev"]
    dev["push_subscriptions"] = [s for s in dev["push_subscriptions"] if s["endpoint"] != body.get("endpoint")]
    return {"registered": len(dev["push_subscriptions"])}


def api_push_test(ctx, body):
    if not PUSH.available:
        raise ApiError(503, "サーバーに pywebpush が入っていません（pip install -r requirements.txt）")
    emit(ctx["st"], ctx["dev_id"], {"kind": "test", "title": "通知のテスト",
                                     "body": "問いかけはこのように届きます。", "url": "/"})
    return {"registered": len(ctx["dev"]["push_subscriptions"])}


# ---------------- 開発者メニュー用（X-Debug-Password が必要） ----------------

def api_debug_ping(ctx, body):
    return {"ok": True, "device_id": ctx["dev_id"]}


def api_debug_trigger_ask(ctx, body):
    """この端末に問いかけを今すぐ出す（今日の返事を消して、通知を送る）。"""
    st, dev, dev_id = ctx["st"], ctx["dev"], ctx["dev_id"]
    today = now(dev).date()
    dev["today"] = new_today(today)
    if not logic.should_ask(dev["inventory"], FOODS, today):
        raise ApiError(409, "今日は早めの食材がないので、問いかけは出ません")
    dev["today"]["ask_sent"] = True
    dev["today"]["no_deadline"] = at_time(today, dev["settings"]["deadline_time"]) <= now(dev)
    names = "・".join(logic.soon_names(dev["inventory"], FOODS, today))
    emit(st, dev_id, {"kind": "ask", "date": today.isoformat(), "title": "今日、作れそう？",
                      "body": f"{names}を、そろそろ使いたい日です。返事しなくても大丈夫。", "url": "/?open=ask"})
    record(st, dev_id, "ask_sent", date=today.isoformat())
    return {"today_state": today_state(dev, today)}


def api_debug_trigger_remind(ctx, body):
    """この端末に確認を今すぐ出す（skip_pending の日だけ）。"""
    st, dev = ctx["st"], ctx["dev"]
    td = dev["today"]
    if td["state"] != "skip_pending":
        raise ApiError(409, "確認を送れるのは「あとでやる」の日だけです（先に「今日は無理」→「あとでやる」）")
    if td["reask_at"]:
        td["reask_at"] = now(dev).isoformat()
    else:
        td["remind_at"], td["remind_sent"] = now(dev).isoformat(), False
    tick(st)
    return {"freeze_pending": td["freeze_pending"]}


def api_debug_advance(ctx, body):
    """この端末の時計だけを進める（ほかの端末は実際の時刻のまま）。"""
    dev = ctx["dev"]
    if body.get("to"):
        target = datetime.fromisoformat(body["to"])
        if target.tzinfo is None:
            target = target.replace(tzinfo=JST)
        dev["clock_offset"] = dev.get("clock_offset", 0) + (target - now(dev)).total_seconds()
    else:
        dev["clock_offset"] = dev.get("clock_offset", 0) + float(body.get("minutes", 60)) * 60
    return {"now": now(dev).isoformat()}


def api_debug_real_time(ctx, body):
    """この端末の時計を実際の時刻に戻す（デモのあとに使う）。在庫は残す。"""
    dev = ctx["dev"]
    dev["clock_offset"] = 0
    dev["today"] = new_today(now(dev).date())
    return {"now": now(dev).isoformat()}


def api_debug_reset_today(ctx, body):
    """この端末の今日の返事・確認の予定・再通知を消す（記録は残す）。"""
    st, dev = ctx["st"], ctx["dev"]
    today = now(dev).date()
    dev["today"] = new_today(today)
    dev["today"]["ask_sent"] = True  # リセットしただけで問いかけの通知が飛ばないように
    dev["today"]["no_deadline"] = True  # 締め切りのあとでリセットしても「今日、作れそう？」に戻す
    return {"today_state": today_state(dev, today), "message": "リセットしました（『今日、作れそう？』が戻ります）"}


def api_debug_reset(ctx, body):
    """scope：all（初期値。この端末の時計・在庫・今日の状態と、全端末の試用の記録）／demo／records。
    設定・通知の購読・学習した対応は消さない。"""
    st, dev = ctx["st"], ctx["dev"]
    scope = body.get("scope", "all")
    if scope in ("demo", "all"):
        fresh = new_device(DEMO_START.date())
        dev["clock_offset"] = (DEMO_START - datetime.now(JST)).total_seconds()  # この端末だけ 10/26 19:00 に
        for k in ("inventory", "next_inv", "today", "pending_cook_record"):
            dev[k] = fresh[k]
    if scope in ("records", "all"):
        st["records"] = []
    return {"now": now(dev).isoformat()}


def _stats(st):
    """全端末の記録を、通知（問いかけ・確認）とその返事の組にして平均を出す。"""
    rows, sent = [], {}
    for r in st["records"]:
        kind = "ask" if r["type"] in ("ask_sent", "answered") else "remind" if r["type"] in ("remind_sent", "remind_answered") else "rescue"
        k = (r["device_id"], r.get("date"), kind)
        if r["type"] in ("ask_sent", "remind_sent"):
            sent[k] = r
            continue
        s = sent.pop(k, None)
        secs = (datetime.fromisoformat(r["at"]) - datetime.fromisoformat(s["at"])).total_seconds() if s else None
        rows.append({"device": r["device_id"][:8], "date": r.get("date"), "kind": kind,
                     "sent_at": s["at"] if s else None, "answered_at": r["at"], "seconds": secs,
                     "taps": r.get("taps"), "via": r.get("via"), "value": r.get("value")})
    timed = [x["seconds"] for x in rows if x["seconds"] is not None]
    taps = [x["taps"] for x in rows if x["taps"] is not None]
    frozen = sum(1 for x in rows if (x["kind"] == "remind" and x["value"] == "done") or (x["kind"] == "rescue" and x["value"] == "done_now"))
    return {"devices": len({r["device_id"] for r in st["records"]}),
            "avg_seconds": round(sum(timed) / len(timed), 1) if timed else None,
            "avg_taps": round(sum(taps) / len(taps), 2) if taps else None,
            "frozen_count": frozen, "rows": rows}


def api_debug_stats(ctx, body):
    return _stats(ctx["st"])


def api_debug_stats_csv(ctx, body):
    buf = io.StringIO()
    w = csv.writer(buf)
    cols = ["device", "date", "kind", "sent_at", "answered_at", "seconds", "taps", "via", "value"]
    w.writerow(cols)
    for row in _stats(ctx["st"])["rows"]:
        w.writerow([row[c] for c in cols])
    return ("text/csv; charset=utf-8", ("﻿" + buf.getvalue()).encode("utf-8"))  # Excel で文字化けしないよう BOM 付き


ROUTES = {
    ("POST", "/api/receipts"): api_receipts,
    ("POST", "/api/inventory/preview"): api_preview,
    ("POST", "/api/inventory/confirm"): api_confirm,
    ("GET", "/api/foods/search"): api_foods_search,
    ("GET", "/api/home"): api_home,
    ("GET", "/api/inventory"): api_inventory,
    ("POST", "/api/inventory/remove"): api_inventory_remove,
    ("GET", "/api/suggestions"): api_suggestions,
    ("POST", "/api/answer"): api_answer,
    ("POST", "/api/rescue"): api_rescue,
    ("POST", "/api/freeze-check"): api_freeze_check,
    ("GET", "/api/cook-items"): api_cook_items,
    ("POST", "/api/cook-record"): api_cook_record,
    ("POST", "/api/cook-record/later"): api_cook_record_later,
    ("POST", "/api/cook-record/skip"): api_cook_record_skip,
    ("GET", "/api/settings"): api_settings_get,
    ("PUT", "/api/settings"): api_settings_put,
    ("GET", "/api/ingredients"): api_ingredients,
    ("GET", "/api/sources"): api_sources,
    ("GET", "/api/config"): api_config,
    ("POST", "/api/push/subscribe"): api_push_subscribe,
    ("POST", "/api/push/unsubscribe"): api_push_unsubscribe,
    ("POST", "/api/push/test"): api_push_test,
}
DEBUG_ROUTES = {
    ("GET", "/api/debug/ping"): api_debug_ping,
    ("GET", "/api/debug/stats"): api_debug_stats,
    ("GET", "/api/debug/stats.csv"): api_debug_stats_csv,
    ("POST", "/api/debug/trigger-ask"): api_debug_trigger_ask,
    ("POST", "/api/debug/trigger-remind"): api_debug_trigger_remind,
    ("POST", "/api/debug/advance"): api_debug_advance,
    ("POST", "/api/debug/real-time"): api_debug_real_time,
    ("POST", "/api/debug/reset-today"): api_debug_reset_today,
    ("POST", "/api/debug/reset"): api_debug_reset,
}


def dispatch(events, subs_by_device):
    """積まれた通知を別スレッドで、それぞれの端末の購読に送る（送信待ちでリクエストを止めない）。"""
    if not events or not PUSH.available:
        return

    def run():
        dead = {}
        for ev in events:
            subs = subs_by_device.get(ev["device_id"], [])
            if subs:
                dead.setdefault(ev["device_id"], set()).update(PUSH.send(subs, ev))
        if any(dead.values()):
            with LOCK:
                st = read_state()
                for dev_id, eps in dead.items():
                    dev = st["devices"].get(dev_id)
                    if dev:
                        dev["push_subscriptions"] = [s for s in dev["push_subscriptions"] if s["endpoint"] not in eps]
                write_state(st)

    threading.Thread(target=run, daemon=True).start()


def run_locked(fn=None, body=None, dev_id=None):
    with LOCK:
        st = read_state()
        if fn:
            st["devices"].setdefault(dev_id, new_device(datetime.now(JST).date()))
        tick(st)
        result = None
        if fn:
            result = fn({"st": st, "dev": st["devices"][dev_id], "dev_id": dev_id}, body or {})
            tick(st)
        events = st.pop("_events", [])
        subs = {e["device_id"]: list(st["devices"].get(e["device_id"], {}).get("push_subscriptions", [])) for e in events}
        write_state(st)
    dispatch(events, subs)
    return result


def scheduler():
    """問いかけ・締め切り・確認・再通知を、画面が開いていなくても進めて通知する。"""
    while True:
        time.sleep(SCHEDULER_SECONDS)
        try:
            run_locked()
        except Exception as e:
            print(f"scheduler error: {e}")


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload, ctype="application/json; charset=utf-8"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method):
        url = urlparse(self.path)
        path = url.path
        if path.startswith("/api/"):
            fn = ROUTES.get((method, path))
            if fn is None and (method, path) in DEBUG_ROUTES:
                password = os.environ.get("DEBUG_PASSWORD")
                if not password:
                    return self._send(403, {"error": "サーバーの .env に DEBUG_PASSWORD が設定されていません"})
                if self.headers.get("X-Debug-Password") != password:
                    return self._send(401, {"error": "開発者メニューのパスワードが違います"})
                fn = DEBUG_ROUTES[(method, path)]
            if not fn:
                return self._send(404, {"error": "not found"})
            dev_id = self.headers.get("X-Device-Id", "")
            if not DEVICE_ID_RE.match(dev_id):
                return self._send(400, {"error": "ヘッダー X-Device-Id（端末ごとのID）がありません"})
            try:
                if method == "GET":
                    body = {k: v[0] for k, v in parse_qs(url.query).items()}
                else:
                    length = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                result = run_locked(fn, body, dev_id)
                if isinstance(result, tuple):  # (content-type, bytes)
                    return self._send(200, result[1], result[0])
                return self._send(200, result)
            except ApiError as e:
                return self._send(e.code, {"error": e.message})
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                return self._send(400, {"error": f"リクエストが正しくありません: {e}"})
        if method != "GET":
            return self._send(405, {"error": "method not allowed"})
        file = (WEB / (path.lstrip("/") or "index.html")).resolve()
        if not file.is_relative_to(WEB) or not file.is_file():
            file = WEB / "index.html"
        ctype = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(200, file.read_bytes(), ctype)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def log_message(self, fmt, *args):
        print(f"{self.command} {self.path} -> {args[1] if len(args) > 1 else ''}")


def lan_addresses():
    addrs = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            addrs.add(s.getsockname()[0])
    except OSError:
        pass
    try:
        addrs |= {a for a in socket.gethostbyname_ex(socket.gethostname())[2] if not a.startswith("127.")}
    except OSError:
        pass
    return sorted(addrs)


def main():
    load_env()
    mimetypes.add_type("application/javascript", ".js")
    mimetypes.add_type("application/manifest+json", ".webmanifest")
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)

    scheme = "http"
    cert, key = os.environ.get("SSL_CERT"), os.environ.get("SSL_KEY")
    if cert and key:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(ROOT / cert, ROOT / key)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        scheme = "https"

    threading.Thread(target=scheduler, daemon=True).start()
    print(f"このPC:          {scheme}://localhost:{port}", flush=True)
    for a in lan_addresses():
        print(f"同じWi-Fiの端末: {scheme}://{a}:{port}", flush=True)
    print(f"Webプッシュ:     {'使える' if PUSH.available else '使えない（pip install -r requirements.txt）'}", flush=True)
    print(f"開発者メニュー:  {'使える' if os.environ.get('DEBUG_PASSWORD') else '.env に DEBUG_PASSWORD を入れると使える'}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
