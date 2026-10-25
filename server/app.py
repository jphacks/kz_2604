"""APIサーバー（冷蔵コーチ）。Python標準ライブラリで動く（Webプッシュに pywebpush、レシートに google-genai）。

    python -m server.app            # http://localhost:8000 （同じWi-Fiの端末からは http://<PCのIP>:8000）

- データはすべて端末ごとのID（ヘッダー X-Device-Id）で分ける。IDがないリクエストは 400
- 時刻はサーバーが持つ「デモ用の時計」で進む（実時間と同じ速さで進み、/api/debug/advance で先に進められる）
- 問いかけ・締め切り・冷凍のリマインド・再通知はすべてサーバーが処理する（15秒ごとに確認）
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
from urllib.parse import urlparse

from . import logic
from .ocr_match import lines_from_ocr_text, match_lines, normalize
from .push import Push
from .receipt import ReceiptError, read_receipt
from .receipt import engine as receipt_engine

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
STATE_FILE = ROOT / "var" / "state.json"
JST = timezone(timedelta(hours=9))
DEMO_START = datetime(2026, 10, 26, 19, 0, tzinfo=JST)  # 月曜、買い物から帰ったところ
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


INGREDIENTS = {ing["id"]: ing for ing in load_json("data/ingredients.json")}
DISHES = load_json("data/dishes.json")


class ApiError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------- 状態と時計 ----------------

def new_device():
    return {
        "inventory": [],
        "next_inv": 1,
        "asks": {},           # 日付 -> 問いかけ
        "freeze_checks": {},  # 日付 -> 冷凍のリマインド
        "settings": {"ask_time": logic.DEFAULT_ASK_TIME, "remind_time": logic.DEFAULT_REMIND_TIME},
        "push_subscriptions": [],
        "learned": {"names": {}, "jans": {}},
    }


def initial_state():
    return {"clock_offset": (DEMO_START - datetime.now(JST)).total_seconds(), "devices": {}, "records": []}


def read_state():
    if STATE_FILE.exists():
        st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if "devices" in st:
            return st
        # 端末ごとのIDより前の形式のファイルは使わない（新しく始める）
    st = initial_state()
    write_state(st)
    return st


def write_state(st):
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def now(st):
    return datetime.now(JST) + timedelta(seconds=st["clock_offset"])


def at_time(d, hhmm):
    h, m = map(int, hhmm.split(":"))
    return datetime(d.year, d.month, d.day, h, m, tzinfo=JST)


def emit(st, dev_id, message):
    """通知を1件積む。状態を書き込んだあとで、その端末の購読にだけプッシュする。"""
    st.setdefault("_events", []).append({"device_id": dev_id, **message})


def record(st, dev_id, type_, **fields):
    """試用の記録（実証用）。type：ask_sent / answered / remind_sent / remind_answered"""
    st["records"].append({"device_id": dev_id, "type": type_, "at": now(st).isoformat(), **fields})


# ---------------- 問いかけと冷凍のリマインド ----------------

def make_ask(st, dev_id, dev, d, asked_at, send=True):
    reason = logic.should_ask(dev["inventory"], INGREDIENTS, d)["reason_items"]
    dev["asks"][d.isoformat()] = {
        "date": d.isoformat(),
        "asked_at": asked_at.isoformat(),
        # 返事の締め切りはリマインドの時刻。返事がなければ「無理」として、そのままリマインドを送る
        # （リマインドの文だけで何をすればいいか分かるので、通知は1日3回までに収まる）
        "deadline": at_time(d, dev["settings"]["remind_time"]).isoformat(),
        "items": reason,
        "answer": None,
        "answered_by": None,
    }
    if send:
        names = "・".join(reason)
        emit(st, dev_id, {"kind": "ask", "date": d.isoformat(), "title": "今日、作れそう？",
                          "body": f"{names}を、そろそろ使いたい日です。返事しなくても大丈夫。", "url": "/?open=ask"})
        record(st, dev_id, "ask_sent", date=d.isoformat())


def apply_skip(st, dev, d):
    """「無理」（または返事なし）を受けて救済行動を1つ決め、冷凍を指示した日だけリマインドを予約する。"""
    key = d.isoformat()
    ask = dev["asks"][key]
    action = logic.choose_action(dev["inventory"], INGREDIENTS, d)
    ask["action"] = action
    ask["acked"] = False  # 画面4で「わかった」を押すまで、開いたら画面4を出す
    dev["freeze_checks"].pop(key, None)
    remind_at = None
    if action and action["kind"] == "freeze":
        remind_at = max(at_time(d, dev["settings"]["remind_time"]), now(st))
        dev["freeze_checks"][key] = {
            "date": key,
            "inventory_id": action["inventory_id"],
            "status": "scheduled",  # remind_at になったら pending にして通知
            "remind_at": remind_at.isoformat(),
            "reask_at": None,
            "reask_count": 0,
        }
    ask["remind_at"] = remind_at.isoformat() if remind_at else None
    return action


def remind_message(dev, fc):
    view = freeze_check_view(dev, fc)
    return {"kind": "remind", "date": fc["date"], "title": f"{view['name']}、冷凍庫に入れた？",
            "body": f"まだなら、{view['how']}だけ（{view['seconds']}秒）。", "url": "/?open=remind"}


def tick_device(st, dev_id, dev, t):
    today = t.date()
    key = today.isoformat()
    logic.mark_was_soon(dev["inventory"], INGREDIENTS, today)

    # 問いかけ（早めの食材がある日だけ、端末ごとの時刻に）
    ask_at = at_time(today, dev["settings"]["ask_time"])
    remind_at = at_time(today, dev["settings"]["remind_time"])
    if key not in dev["asks"] and ask_at <= t < remind_at:
        if logic.should_ask(dev["inventory"], INGREDIENTS, today)["should_ask"]:
            make_ask(st, dev_id, dev, today, ask_at)

    # 返事の締め切り（リマインドの時刻）を過ぎたら skip
    for d, ask in dev["asks"].items():
        if ask["answer"] is None and t >= datetime.fromisoformat(ask["deadline"]):
            ask["answer"] = "skip"
            ask["answered_by"] = "timeout"
            apply_skip(st, dev, logic.parse_date(d))

    # 冷凍のリマインドと、「あとで」の1時間後の再通知（1回だけ）
    for fc in dev["freeze_checks"].values():
        due = (fc["status"] == "scheduled" and t >= datetime.fromisoformat(fc["remind_at"])) or \
              (fc["status"] == "later" and t >= datetime.fromisoformat(fc["reask_at"]))
        if due:
            fc["status"] = "pending"
            fc["reask_at"] = None
            fc["sent_at"] = t.isoformat()
            emit(st, dev_id, remind_message(dev, fc))
            record(st, dev_id, "remind_sent", date=fc["date"], again=fc.get("reask_count", 0) > 0)


def tick(st):
    t = now(st)
    for dev_id, dev in st["devices"].items():
        tick_device(st, dev_id, dev, t)


def find_inv(dev, inv_id):
    for inv in dev["inventory"]:
        if inv["id"] == inv_id:
            return inv
    raise ApiError(404, f"在庫 {inv_id} がありません")


def set_status(st, inv, status):
    inv["status"] = status
    inv["updated_at"] = now(st).isoformat()
    if status == "frozen":
        inv["frozen_at"] = now(st).date().isoformat()


def freeze_check_view(dev, fc):
    inv = next((i for i in dev["inventory"] if i["id"] == fc["inventory_id"]), None)
    ing = INGREDIENTS[inv["ingredient_id"]] if inv else {}
    # 「袋のまま冷凍庫へ」→「袋のまま入れる」（「まだなら、袋のまま入れるだけ」の形にする）
    how = (ing.get("freeze_action") or "冷凍庫へ").replace("冷凍庫へ", "入れる")
    return {**fc, "name": ing.get("name", ""), "how": how,
            "seconds": ing.get("freeze_seconds") or 30,
            "can_reask": fc.get("reask_count", 0) < logic.MAX_REASK}


def current_screen(st, dev):
    """今日の分で、いま画面に出すべきもの（4：救済行動、5b：冷凍庫に入れた？）。"""
    key = now(st).date().isoformat()
    ask = dev["asks"].get(key)
    fc = dev["freeze_checks"].get(key)
    if fc and fc["status"] == "pending":
        return {"screen": "freeze_check", "today_ask": ask, "freeze_check": freeze_check_view(dev, fc)}
    if ask and ask["answer"] == "skip" and not ask.get("acked", True):
        return {"screen": "action", "today_ask": ask}
    return {"screen": None, "today_ask": ask}


def weekly(st, dev):
    return logic.weekly_card(dev["inventory"], INGREDIENTS, now(st).date())


# ---------------- API ----------------

def preview_item(ingredient_id, purchased_at, today, expiry_override=None):
    ing = INGREDIENTS.get(ingredient_id)
    if not ing:
        raise ApiError(400, f"食材表にないid: {ingredient_id}")
    d = logic.describe({"purchased_at": purchased_at, "expiry_override": expiry_override}, ing, today)
    return {"ingredient_id": ingredient_id, "name": ing["name"], "freshness": d["freshness"],
            "freshness_label": d["freshness_label"], "days_left": d["days_left"], "limit_date": d["limit_date"]}


def api_receipts(ctx, body):
    st, dev = ctx["st"], ctx["dev"]
    today = now(st).date()
    extra = {}
    if body.get("use_fixture"):
        read = load_json("fixtures/receipt_demo.json")
        engine, extra = "fixture", {}
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
                 "transcript": "\n".join(f"{l['name']}" + (f"  [{l['jan']}]" if l["jan"] else "") for l in read["lines"])}
    purchased_at = read.get("purchased_at") or today.isoformat()
    result = match_lines(read["lines"], INGREDIENTS, dev["learned"])
    for m in result["matched"]:
        m.update(preview_item(m["ingredient_id"], purchased_at, today))
    for u in result["unmatched"]:
        u["candidates"] = [{"id": c, "name": INGREDIENTS[c]["name"]} for c in u["candidates"]]
    return {"purchased_at": purchased_at, **result, "engine": engine, **extra}


def api_preview(ctx, body):
    today = now(ctx["st"]).date()
    return {"items": [preview_item(it["ingredient_id"], body["purchased_at"], today, it.get("expiry_override"))
                      for it in body.get("items", [])]}


def api_confirm(ctx, body):
    """チェックした品目を在庫に登録し、自分で選んだ行（learn: true）の対応を覚える。"""
    st, dev = ctx["st"], ctx["dev"]
    added = []
    for it in body.get("items", []):
        if it["ingredient_id"] not in INGREDIENTS:
            raise ApiError(400, f"食材表にないid: {it['ingredient_id']}")
        if it.get("learn"):
            if it.get("raw") and normalize(it["raw"]):
                dev["learned"]["names"][normalize(it["raw"])] = it["ingredient_id"]
            if it.get("jan"):
                dev["learned"]["jans"][it["jan"]] = it["ingredient_id"]
        inv = {
            "id": f"inv_{dev['next_inv']:03d}",
            "ingredient_id": it["ingredient_id"],
            "raw": it.get("raw", ""),
            "purchased_at": body["purchased_at"],
            "frozen_at": None,
            "expiry_override": it.get("expiry_override") or None,
            "status": "stocked",
            "was_soon": False,
            "updated_at": now(st).isoformat(),
        }
        dev["next_inv"] += 1
        dev["inventory"].append(inv)
        added.append(inv["id"])
    logic.mark_was_soon(dev["inventory"], INGREDIENTS, now(st).date())
    return {"added": added}


def api_home(ctx, body):
    st, dev = ctx["st"], ctx["dev"]
    t = now(st)
    today = t.date()
    ask_today = dev["asks"].get(today.isoformat())
    q = logic.should_ask(dev["inventory"], INGREDIENTS, today)
    answered = bool(ask_today and ask_today["answer"])
    items = ask_today["items"] if ask_today and ask_today.get("items") else q["reason_items"]
    return {
        "now": t.isoformat(),
        "ask": {"show": q["should_ask"] and not answered, "items": items, "answered": answered,
                "date": today.isoformat()},
        "notify_today": {"will_notify": q["should_ask"] and not answered, "time": dev["settings"]["ask_time"]},
        "weekly": weekly(st, dev),
        **current_screen(st, dev),
    }


def api_inventory(ctx, body):
    return logic.fridge_view(ctx["dev"]["inventory"], INGREDIENTS, now(ctx["st"]).date())


def api_suggestions(ctx, body):
    return {"dishes": logic.suggestions(ctx["dev"]["inventory"], INGREDIENTS, DISHES, now(ctx["st"]).date())}


def api_answer(ctx, body):
    st, dev, dev_id = ctx["st"], ctx["dev"], ctx["dev_id"]
    d = logic.parse_date(body.get("date") or now(st).date().isoformat())
    key = d.isoformat()
    answer = body.get("answer")
    if answer not in ("skip", "cook"):
        raise ApiError(400, "answer は skip か cook")
    if key not in dev["asks"]:
        make_ask(st, dev_id, dev, d, now(st), send=False)  # ホームのカードから、通知より先に答えたとき
    ask = dev["asks"][key]
    if ask["answer"] is None or ask["answered_by"] == "timeout":
        record(st, dev_id, "answered", date=key, value=answer, taps=int(body.get("taps") or 1),
               via=body.get("via") or "screen")
    ask["answer"] = answer
    ask["answered_by"] = "user"
    if answer == "skip":
        action = apply_skip(st, dev, d)
        return {"action": action, "remind_at": ask["remind_at"]}
    return {"items": logic.cook_items(dev["inventory"], INGREDIENTS, d)}


def api_action_ack(ctx, body):
    ask = ctx["dev"]["asks"].get(body.get("date") or now(ctx["st"]).date().isoformat())
    if not ask:
        raise ApiError(404, "この日の救済行動はありません")
    ask["acked"] = True
    return {"remind_at": ask.get("remind_at")}


def api_action_already_used(ctx, body):
    """画面4「〇〇はもう使った」。在庫から消して、救済行動を選び直す。"""
    st, dev = ctx["st"], ctx["dev"]
    d = logic.parse_date(body.get("date") or now(st).date().isoformat())
    ask = dev["asks"][d.isoformat()]
    set_status(st, find_inv(dev, ask["action"]["inventory_id"]), "used")
    action = apply_skip(st, dev, d)
    return {"action": action, "remind_at": ask["remind_at"]}


def api_freeze_check(ctx, body):
    st, dev, dev_id = ctx["st"], ctx["dev"], ctx["dev_id"]
    key = body.get("date") or next((k for k, v in sorted(dev["freeze_checks"].items(), reverse=True)
                                    if v["status"] in ("pending", "later", "scheduled")), None)
    fc = dev["freeze_checks"].get(key) if key else None
    if not fc:
        raise ApiError(404, "確認待ちの「冷凍庫に入れた？」はありません")
    answer = body.get("answer")
    if answer not in ("done", "later", "gone"):
        raise ApiError(400, "answer は done / later / gone")
    record(st, dev_id, "remind_answered", date=key, value=answer, taps=int(body.get("taps") or 1),
           via=body.get("via") or "screen")
    inv = find_inv(dev, fc["inventory_id"])
    if answer == "done":
        set_status(st, inv, "frozen")
        fc["status"] = "done"
        return {"weekly": weekly(st, dev), "name": INGREDIENTS[inv["ingredient_id"]]["name"]}
    if answer == "later":
        if fc.get("reask_count", 0) >= logic.MAX_REASK:
            fc["status"] = "dropped"  # 2回目の「あとで」。その日はおしまい
            return {"reask_at": None}
        reask = now(st) + timedelta(minutes=logic.REASK_MINUTES)
        fc["status"] = "later"
        fc["reask_at"] = reask.isoformat()
        fc["reask_count"] = fc.get("reask_count", 0) + 1
        return {"reask_at": reask.isoformat()}
    set_status(st, inv, "gone")
    fc["status"] = "gone"
    return {"weekly": weekly(st, dev)}


def api_use(ctx, body):
    for inv_id in body.get("inventory_ids", []):
        set_status(ctx["st"], find_inv(ctx["dev"], inv_id), "used")
    return {"ok": True}


def api_cook_items(ctx, body):
    return {"items": logic.cook_items(ctx["dev"]["inventory"], INGREDIENTS, now(ctx["st"]).date())}


def api_settings_get(ctx, body):
    return ctx["dev"]["settings"]


def api_settings_put(ctx, body):
    ask_time, remind_time = body.get("ask_time"), body.get("remind_time")
    if not (TIME_RE.match(ask_time or "") and TIME_RE.match(remind_time or "")):
        raise ApiError(400, "時刻は HH:MM の形で指定してください")
    if remind_time <= ask_time:
        raise ApiError(400, "リマインドの時刻は、問いかけの時刻より後にしてください")
    ctx["dev"]["settings"] = {"ask_time": ask_time, "remind_time": remind_time}
    # 今日の問いかけがまだ答えられていなければ、締め切り（リマインドの時刻）も合わせる
    ask = ctx["dev"]["asks"].get(now(ctx["st"]).date().isoformat())
    if ask and ask["answer"] is None:
        ask["deadline"] = at_time(logic.parse_date(ask["date"]), remind_time).isoformat()
    return ctx["dev"]["settings"]


def api_ingredients(ctx, body):
    return {"items": [{"id": i["id"], "name": i["name"]} for i in INGREDIENTS.values()]}


def api_sources(ctx, body):
    """食材表の「目安と出典」。発表や画面での説明に使う。"""
    sources = load_json("data/sources.json")
    items = []
    for ing in INGREDIENTS.values():
        if ing["freezable"]:
            rescue = f"{ing['freeze_action']}（冷凍したら{ing['freezer_period']}以内）"
        else:
            rescue = ing.get("quick_use") or "—"
        items.append({"name": ing["name"], "fridge_days": ing["fridge_days"], "freezer_days": ing.get("freezer_days"),
                      "track_expiry": ing.get("track_expiry", True), "rescue": rescue,
                      "fridge_basis": ing["fridge_basis"], "action_basis": ing.get("action_basis"), "refs": ing["refs"]})
    return {"items": items, "sources": sources}


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
    st, dev, dev_id = ctx["st"], ctx["dev"], ctx["dev_id"]
    t = now(st)
    key = t.date().isoformat()
    dev["asks"].pop(key, None)
    dev["freeze_checks"].pop(key, None)
    if not logic.should_ask(dev["inventory"], INGREDIENTS, t.date())["should_ask"]:
        raise ApiError(409, "今日は早めの食材がないので、問いかけは出ません")
    make_ask(st, dev_id, dev, t.date(), t)
    return {"ask": dev["asks"][key]}


def api_debug_trigger_remind(ctx, body):
    """冷凍のリマインドを今すぐ出す（予約中・「あとで」のものを前倒し）。"""
    st, dev = ctx["st"], ctx["dev"]
    for key in sorted(dev["freeze_checks"], reverse=True):
        fc = dev["freeze_checks"][key]
        if fc["status"] in ("scheduled", "later"):
            if fc["status"] == "later":
                fc["reask_at"] = now(st).isoformat()
            else:
                fc["remind_at"] = now(st).isoformat()
            tick(st)
            return {"freeze_check": freeze_check_view(dev, fc)}
    raise ApiError(404, "予約中のリマインドはありません（先に「無理」と答えてください）")


def api_debug_advance(ctx, body):
    st = ctx["st"]
    if body.get("to"):
        target = datetime.fromisoformat(body["to"])
        if target.tzinfo is None:
            target = target.replace(tzinfo=JST)
        st["clock_offset"] += (target - now(st)).total_seconds()
    else:
        st["clock_offset"] += float(body.get("minutes", 60)) * 60
    return {"now": now(st).isoformat()}


def api_debug_use(ctx, body):
    """デモ用：指定した食材の在庫を「使った」にする（木・金に作った体で）。"""
    ids = set(body.get("ingredient_ids", []))
    for inv in ctx["dev"]["inventory"]:
        if inv["status"] == "stocked" and inv["ingredient_id"] in ids:
            set_status(ctx["st"], inv, "used")
    return {"ok": True}


def api_debug_reset(ctx, body):
    """scope：demo（時計とこの端末の在庫・問いかけ）／records（全端末の試用の記録）／all（両方。初期値）。
    設定・通知の購読・学習した対応は消さない。"""
    st, dev = ctx["st"], ctx["dev"]
    scope = body.get("scope", "all")
    if scope in ("demo", "all"):
        st["clock_offset"] = initial_state()["clock_offset"]
        fresh = new_device()
        for k in ("inventory", "next_inv", "asks", "freeze_checks"):
            dev[k] = fresh[k]
    if scope in ("records", "all"):
        st["records"] = []
    return {"now": now(st).isoformat()}


def _stats(st):
    """全端末の記録を、問いかけ（またはリマインド）とその返事の組にして平均を出す。"""
    rows = []
    sent = {}
    for r in st["records"]:
        k = (r["device_id"], r.get("date"), "ask" if r["type"] in ("ask_sent", "answered") else "remind")
        if r["type"] in ("ask_sent", "remind_sent"):
            sent[k] = r
        elif r["type"] in ("answered", "remind_answered"):
            s = sent.pop(k, None)
            secs = (datetime.fromisoformat(r["at"]) - datetime.fromisoformat(s["at"])).total_seconds() if s else None
            rows.append({"device": r["device_id"][:8], "date": r.get("date"), "kind": k[2],
                         "sent_at": s["at"] if s else None, "answered_at": r["at"], "seconds": secs,
                         "taps": r.get("taps"), "via": r.get("via"), "value": r.get("value")})
    timed = [x["seconds"] for x in rows if x["seconds"] is not None]
    taps = [x["taps"] for x in rows if x["taps"] is not None]
    return {
        "devices": len({r["device_id"] for r in st["records"]}),
        "avg_seconds": round(sum(timed) / len(timed), 1) if timed else None,
        "avg_taps": round(sum(taps) / len(taps), 2) if taps else None,
        "frozen_count": sum(1 for x in rows if x["kind"] == "remind" and x["value"] == "done"),
        "rows": rows,
    }


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
    ("GET", "/api/home"): api_home,
    ("GET", "/api/inventory"): api_inventory,
    ("GET", "/api/suggestions"): api_suggestions,
    ("POST", "/api/inventory/use"): api_use,
    ("GET", "/api/cook-items"): api_cook_items,
    ("POST", "/api/answer"): api_answer,
    ("POST", "/api/action/ack"): api_action_ack,
    ("POST", "/api/action/already-used"): api_action_already_used,
    ("POST", "/api/freeze-check"): api_freeze_check,
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
    ("POST", "/api/debug/use"): api_debug_use,
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
        tick(st)
        result = None
        if fn:
            dev = st["devices"].setdefault(dev_id, new_device())
            result = fn({"st": st, "dev": dev, "dev_id": dev_id}, body or {})
            tick(st)
        events = st.pop("_events", [])
        subs = {e["device_id"]: list(st["devices"].get(e["device_id"], {}).get("push_subscriptions", [])) for e in events}
        write_state(st)
    dispatch(events, subs)
    return result


def scheduler():
    """問いかけ・締め切り・リマインドを、画面が開いていなくても進めて通知する。"""
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
        path = urlparse(self.path).path
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
