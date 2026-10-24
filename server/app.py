"""APIサーバー。Python標準ライブラリで動く（Webプッシュだけ pywebpush を使う）。

    python -m server.app            # http://localhost:8000 （同じWi-Fiの端末からは http://<PCのIP>:8000）

時刻はサーバーが持つ「デモ用の時計」で進む（実時間と同じ速さで進み、
/api/debug/advance で先に進められる）。タイマー（締め切り・再問いかけ）は
すべてここで処理する。
"""
import json
import mimetypes
import os
import socket
import ssl
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import logic
from .ocr_match import match_lines, match_text
from .push import Push
from .receipt import ReceiptError, read_receipt
from .receipt import engine as receipt_engine

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
STATE_FILE = ROOT / "var" / "state.json"
JST = timezone(timedelta(hours=9))
DEMO_START = datetime(2026, 10, 26, 19, 0, tzinfo=JST)  # 月曜、買い物から帰ったところ

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


# ---------------- 状態と時計 ----------------

def initial_state():
    return {
        "clock_offset": (DEMO_START - datetime.now(JST)).total_seconds(),
        "inventory": [],
        "next_inv": 1,
        "asks": {},           # 日付 -> 問いかけ
        "freeze_checks": {},  # 日付 -> 「冷凍した？」
        "push_subscriptions": [],
    }


def read_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    st = initial_state()
    write_state(st)
    return st


def write_state(st):
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def now(st):
    return datetime.now(JST) + timedelta(seconds=st["clock_offset"])


def at(d, hour):
    return datetime(d.year, d.month, d.day, hour, 0, tzinfo=JST)


def emit(st, title, body):
    """通知を1件積む。状態を書き込んだあとで全端末にプッシュする。"""
    st.setdefault("_events", []).append({"title": title, "body": body})


def make_ask(st, d, asked_at):
    reason = logic.should_ask(st["inventory"], INGREDIENTS, d)["reason_items"]
    emit(st, "今日、作れそう？",
         f"{'と'.join(reason)}をそろそろ使いたい日です" if reason else "答えるのはボタン1つだけです")
    st["asks"][d.isoformat()] = {
        "date": d.isoformat(),
        "asked_at": asked_at.isoformat(),
        "deadline": at(d, logic.DEADLINE_HOUR).isoformat(),
        "answer": None,
        "answered_by": None,
    }


def remind_time(st, d):
    """冷凍のリマインドの時刻。その日の21:30。遅く答えた日は、答えてから30分はあける。"""
    h, m = logic.REMIND_TIME
    base = datetime(d.year, d.month, d.day, h, m, tzinfo=JST)
    return max(base, now(st) + timedelta(minutes=logic.REMIND_MIN_GAP_MINUTES))


def apply_skip(st, d):
    """「無理」（または返事なし）を受けて救済行動を決め、冷凍の行動なら夜のリマインドを予約する。"""
    key = d.isoformat()
    ask = st["asks"][key]
    action, next_tip = logic.choose_action(st["inventory"], INGREDIENTS, d)
    ask["action"] = action
    ask["next_tip"] = next_tip
    ask["acked"] = False  # 画面4で「わかった」を押すまで、開いたら画面4を出す
    st["freeze_checks"].pop(key, None)
    remind_at = None
    if action and action["kind"] == "freeze":
        remind_at = remind_time(st, d)
        st["freeze_checks"][key] = {
            "date": key,
            "inventory_id": action["inventory_id"],
            "status": "scheduled",  # remind_at になったら pending にして通知
            "remind_at": remind_at.isoformat(),
            "reask_at": None,
            "reask_count": 0,
        }
    ask["remind_at"] = remind_at.isoformat() if remind_at else None
    return action, next_tip


def tick(st):
    """時刻に関わる処理。リクエストのたびと、時刻を進めたときに呼ぶ。"""
    t = now(st)
    today = t.date()
    key = today.isoformat()

    # 18時の問いかけ（早めに使いたい食材がある日だけ）
    if key not in st["asks"] and logic.ASK_HOUR <= t.hour < logic.DEADLINE_HOUR:
        if logic.should_ask(st["inventory"], INGREDIENTS, today)["should_ask"]:
            make_ask(st, today, at(today, logic.ASK_HOUR))

    # 返事の締め切りを過ぎたら skip 扱い
    for d, ask in st["asks"].items():
        if ask["answer"] is None and t >= datetime.fromisoformat(ask["deadline"]):
            ask["answer"] = "skip"
            ask["answered_by"] = "timeout"
            action, _ = apply_skip(st, datetime.fromisoformat(ask["deadline"]).date())
            if action:
                emit(st, "今日はこれだけ", action["text"])

    # 「無理」の日の夜（21:30）の冷凍のリマインドと、「まだ」の1時間後の再通知
    for fc in st["freeze_checks"].values():
        due = (fc["status"] == "scheduled" and t >= datetime.fromisoformat(fc["remind_at"])) or \
              (fc["status"] == "later" and t >= datetime.fromisoformat(fc["reask_at"]))
        if due:
            again = fc["status"] == "later"
            fc["status"] = "pending"
            fc["reask_at"] = None
            view = freeze_check_view(st, fc)
            emit(st, f"{view['name']}、冷凍した？",
                 "もう一度だけ聞かせてください" if again else "答えるのはボタン1つだけです")


def find_inv(st, inv_id):
    for inv in st["inventory"]:
        if inv["id"] == inv_id:
            return inv
    raise ApiError(404, f"在庫 {inv_id} がありません")


def set_status(st, inv, status):
    inv["status"] = status
    inv["updated_at"] = now(st).isoformat()


def current_ask(st):
    """いま画面に出すべきもの（画面3/4/5）を返す。"""
    for d in sorted(st["asks"], reverse=True):
        ask = st["asks"][d]
        if ask["answer"] is None:
            return {"screen": "ask", "ask": ask}
        fc = st["freeze_checks"].get(d)
        if ask["answer"] == "skip":
            if fc and fc["status"] == "pending":
                return {"screen": "freeze_check", "ask": ask, "freeze_check": freeze_check_view(st, fc)}
            if not ask.get("acked", True):
                return {"screen": "action", "ask": ask}
    return {"screen": None}


def freeze_check_view(st, fc):
    inv = next((i for i in st["inventory"] if i["id"] == fc["inventory_id"]), None)
    ask = st["asks"][fc["date"]]
    kind = (ask.get("action") or {}).get("kind")
    name = INGREDIENTS[inv["ingredient_id"]]["name"] if inv else ""
    return {**fc, "name": name, "kind": kind, "question": "冷凍した？",
            "can_reask": fc.get("reask_count", 0) < logic.MAX_REASK}


# ---------------- API ----------------

class ApiError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def preview_items(items, purchased_at, today):
    out = []
    for it in items:
        ing = INGREDIENTS.get(it["ingredient_id"])
        if not ing:
            raise ApiError(400, f"食材表にないid: {it['ingredient_id']}")
        inv = {"purchased_at": purchased_at, "expiry_override": it.get("expiry_override") or None}
        d = logic.describe(inv, ing, today)
        out.append({**it, "name": ing["name"], "freshness": d["freshness"],
                    "freshness_label": d["freshness_label"], "days_left": d["days_left"],
                    "limit_date": d["limit_date"], "source": ing["source"]})
    return out


def api_receipts(st, body):
    today = now(st).date()
    if body.get("use_fixture"):
        result = load_json("fixtures/receipt_demo.json")
        result["engine"] = "fixture"
    elif body.get("ocr_text") is not None:
        # 予備：端末の中の Tesseract.js で読んだ文字
        result = match_text(body["ocr_text"], INGREDIENTS)
        if not result["items"] and not result["ignored"]:
            raise ApiError(422, "レシートの品目を読み取れませんでした")
        result["engine"] = "tesseract"
        result["transcript"] = body["ocr_text"]
    else:
        # 主：Gemini API（または Claude API）で文字起こし → コードで食材表と照合
        images = body.get("images") or (
            [{"data": body["image_base64"], "media_type": body.get("media_type", "image/jpeg")}]
            if body.get("image_base64") else [])
        if not images:
            raise ApiError(400, "image_base64 か images がありません")
        try:
            read = read_receipt(images)
        except ReceiptError as e:
            raise ApiError(502, str(e))
        result = match_lines(read["lines"], INGREDIENTS)
        result["purchased_at"] = read["purchased_at"]
        result["engine"] = read["engine"]
        result["model"] = read["model"]
        result["transcript"] = "\n".join(read["lines"])
    result["purchased_at"] = result.get("purchased_at") or today.isoformat()
    result["items"] = preview_items(result["items"], result["purchased_at"], today)
    return result


def api_preview(st, body):
    today = now(st).date()
    return {"items": preview_items(body.get("items", []), body["purchased_at"], today)}


def api_confirm(st, body):
    added = []
    for it in body.get("items", []):
        if it["ingredient_id"] not in INGREDIENTS:
            raise ApiError(400, f"食材表にないid: {it['ingredient_id']}")
        inv = {
            "id": f"inv_{st['next_inv']:03d}",
            "ingredient_id": it["ingredient_id"],
            "raw": it.get("raw", ""),
            "purchased_at": body["purchased_at"],
            "expiry_override": it.get("expiry_override") or None,
            "status": "stocked",
            "updated_at": now(st).isoformat(),
        }
        st["next_inv"] += 1
        st["inventory"].append(inv)
        added.append(inv["id"])
    return {"added": added}


def api_inventory(st, body):
    return logic.fridge_view(st["inventory"], INGREDIENTS, now(st).date())


def api_suggestions(st, body):
    return {"dishes": logic.suggestions(st["inventory"], INGREDIENTS, DISHES, now(st).date())}


def api_today(st, body):
    return logic.should_ask(st["inventory"], INGREDIENTS, now(st).date())


def api_answer(st, body):
    d = datetime.fromisoformat(body.get("date") or now(st).date().isoformat()).date()
    key = d.isoformat()
    if key not in st["asks"]:
        make_ask(st, d, now(st))
    ask = st["asks"][key]
    answer = body.get("answer")
    if answer not in ("skip", "cook"):
        raise ApiError(400, "answer は skip か cook")
    ask["answer"] = answer
    ask["answered_by"] = "user"
    if answer == "skip":
        action, next_tip = apply_skip(st, d)
        return {"action": action, "next_tip": next_tip, "remind_at": ask["remind_at"]}
    return {"items": logic.cook_items(st["inventory"], INGREDIENTS, d)}


def api_action_ack(st, body):
    ask = st["asks"].get(body["date"])
    if not ask:
        raise ApiError(404, "この日の救済行動はありません")
    ask["acked"] = True
    return {"remind_at": ask.get("remind_at")}


def api_action_already_used(st, body):
    """画面4「〇〇はもう使った」。在庫から消して、次の行動を選び直す。"""
    d = datetime.fromisoformat(body["date"]).date()
    ask = st["asks"][d.isoformat()]
    used = ask["action"]["inventory_id"]
    set_status(st, find_inv(st, used), "used")
    action, next_tip = apply_skip(st, d)
    return {"action": action, "next_tip": next_tip, "remind_at": ask["remind_at"]}


def api_freeze_check(st, body):
    d = body.get("date") or next((k for k, v in st["freeze_checks"].items() if v["status"] == "pending"), None)
    fc = st["freeze_checks"].get(d) if d else None
    if not fc:
        raise ApiError(404, "確認待ちの「冷凍した？」はありません")
    answer = body.get("answer")
    inv = find_inv(st, fc["inventory_id"])
    if answer == "done":
        kind = (st["asks"][d].get("action") or {}).get("kind")
        set_status(st, inv, "frozen" if kind == "freeze" else "used")
        fc["status"] = "done"
        return {"ok": True}
    if answer == "later":
        if fc.get("reask_count", 0) >= logic.MAX_REASK:
            fc["status"] = "dropped"  # 上限まで聞いたので、今日はもう聞かない
            return {"reask_at": None}
        reask = now(st) + timedelta(minutes=logic.REASK_MINUTES)
        fc["status"] = "later"
        fc["reask_at"] = reask.isoformat()
        fc["reask_count"] = fc.get("reask_count", 0) + 1
        return {"reask_at": reask.isoformat()}
    if answer == "gone":
        set_status(st, inv, "gone")
        fc["status"] = "gone"
        return {"ok": True}
    raise ApiError(400, "answer は done / later / gone")


def api_use(st, body):
    for inv_id in body.get("inventory_ids", []):
        set_status(st, find_inv(st, inv_id), "used")
    return {"ok": True}


def api_cook_items(st, body):
    return {"items": logic.cook_items(st["inventory"], INGREDIENTS, now(st).date())}


def api_weekly(st, body):
    return logic.weekly_card(st["inventory"], INGREDIENTS, st["asks"], st["freeze_checks"], now(st).date())


def api_ingredients(st, body):
    return {"items": [{"id": i["id"], "name": i["name"]} for i in INGREDIENTS.values()]}


def api_sources(st, body):
    """食材表の「目安と出典」。発表や画面での説明に使う。"""
    sources = load_json("data/sources.json")
    items = []
    for ing in INGREDIENTS.values():
        if ing["freezable"]:
            rescue = f"{ing['freeze_action']}（冷凍したら{ing['freezer_period']}以内）"
        else:
            rescue = ing["quick_use"] or "—"
        items.append({
            "name": ing["name"],
            "fridge_days": ing["fridge_days"],
            "expiry_type": ing["expiry_type"],
            "rescue": rescue,
            "fridge_basis": ing["fridge_basis"],
            "action_basis": ing.get("action_basis"),
            "refs": ing["refs"],
        })
    return {"items": items, "sources": sources}


def api_state(st, body):
    return {"now": now(st).isoformat(), **current_ask(st)}


def api_debug_trigger_ask(st, body):
    t = now(st)
    key = t.date().isoformat()
    st["asks"].pop(key, None)
    st["freeze_checks"].pop(key, None)
    make_ask(st, t.date(), t)
    return {"ask": st["asks"][key], **logic.should_ask(st["inventory"], INGREDIENTS, t.date())}


def api_debug_trigger_remind(st, body):
    """冷凍のリマインドを今すぐ出す（予約中・「まだ」のものを前倒し）。"""
    for d in sorted(st["freeze_checks"], reverse=True):
        fc = st["freeze_checks"][d]
        if fc["status"] in ("scheduled", "later"):
            fc["status"] = "scheduled"
            fc["remind_at"] = now(st).isoformat()
            tick(st)
            return {"freeze_check": freeze_check_view(st, fc)}
    raise ApiError(404, "予約中の冷凍のリマインドはありません（先に「無理」と答えてください）")


def api_debug_advance(st, body):
    if body.get("to"):
        target = datetime.fromisoformat(body["to"])
        if target.tzinfo is None:
            target = target.replace(tzinfo=JST)
        st["clock_offset"] += (target - now(st)).total_seconds()
    else:
        st["clock_offset"] += float(body.get("minutes", 60)) * 60
    return {"now": now(st).isoformat()}


def api_debug_use(st, body):
    """デモ用：指定した食材の在庫を「使った」にする（木・金に作った体で）。"""
    ids = set(body.get("ingredient_ids", []))
    for inv in st["inventory"]:
        if inv["status"] == "stocked" and inv["ingredient_id"] in ids:
            set_status(st, inv, "used")
    return {"ok": True}


def api_config(st, body):
    return {
        "receipt_engine": receipt_engine(),  # "gemini" / "claude" / None（キーなし）
        "demo": os.environ.get("DEMO_MODE", "1") != "0",
        "push_available": PUSH.available,
        "vapid_public_key": PUSH.public_key,
        "push_devices": len(st.get("push_subscriptions", [])),
    }


def api_push_subscribe(st, body):
    sub = body.get("subscription") or {}
    if not sub.get("endpoint") or not sub.get("keys"):
        raise ApiError(400, "subscription がありません")
    subs = [s for s in st.setdefault("push_subscriptions", []) if s["endpoint"] != sub["endpoint"]]
    subs.append({"endpoint": sub["endpoint"], "keys": sub["keys"]})
    st["push_subscriptions"] = subs
    return {"devices": len(subs)}


def api_push_unsubscribe(st, body):
    st["push_subscriptions"] = [s for s in st.get("push_subscriptions", []) if s["endpoint"] != body.get("endpoint")]
    return {"devices": len(st["push_subscriptions"])}


def api_push_test(st, body):
    if not PUSH.available:
        raise ApiError(503, "サーバーに pywebpush が入っていません（pip install -r requirements.txt）")
    emit(st, "通知のテスト", "18時のお知らせはこのように届きます")
    return {"devices": len(st.get("push_subscriptions", []))}


def api_debug_reset(st, body):
    subs = st.get("push_subscriptions", [])
    st.clear()
    st.update(initial_state())
    st["push_subscriptions"] = subs  # 端末の通知登録はリセットしない
    return {"now": now(st).isoformat()}


ROUTES = {
    ("POST", "/api/receipts"): api_receipts,
    ("POST", "/api/inventory/preview"): api_preview,
    ("POST", "/api/inventory/confirm"): api_confirm,
    ("GET", "/api/inventory"): api_inventory,
    ("GET", "/api/suggestions"): api_suggestions,
    ("POST", "/api/inventory/use"): api_use,
    ("GET", "/api/cook-items"): api_cook_items,
    ("GET", "/api/today"): api_today,
    ("POST", "/api/answer"): api_answer,
    ("POST", "/api/action/ack"): api_action_ack,
    ("POST", "/api/action/already-used"): api_action_already_used,
    ("POST", "/api/freeze-check"): api_freeze_check,
    ("GET", "/api/weekly"): api_weekly,
    ("GET", "/api/ingredients"): api_ingredients,
    ("GET", "/api/sources"): api_sources,
    ("GET", "/api/config"): api_config,
    ("POST", "/api/push/subscribe"): api_push_subscribe,
    ("POST", "/api/push/unsubscribe"): api_push_unsubscribe,
    ("POST", "/api/push/test"): api_push_test,
    ("GET", "/api/state"): api_state,
    ("POST", "/api/debug/trigger-ask"): api_debug_trigger_ask,
    ("POST", "/api/debug/advance"): api_debug_advance,
    ("POST", "/api/debug/trigger-remind"): api_debug_trigger_remind,
    ("POST", "/api/debug/use"): api_debug_use,
    ("POST", "/api/debug/reset"): api_debug_reset,
}


def dispatch(events, subs):
    """積まれた通知を別スレッドで送る（送信待ちでリクエストを止めない）。"""
    if not events or not subs or not PUSH.available:
        return

    def run():
        dead = set()
        for ev in events:
            dead |= set(PUSH.send(subs, ev["title"], ev["body"]))
        if dead:
            with LOCK:
                st = read_state()
                st["push_subscriptions"] = [s for s in st.get("push_subscriptions", []) if s["endpoint"] not in dead]
                write_state(st)

    threading.Thread(target=run, daemon=True).start()


def run_locked(fn, body=None):
    with LOCK:
        st = read_state()
        tick(st)
        result = fn(st, body or {}) if fn else None
        tick(st)
        events = st.pop("_events", [])
        subs = list(st.get("push_subscriptions", []))
        write_state(st)
    dispatch(events, subs)
    return result


def scheduler():
    """18時の問いかけ・締め切り・再問いかけを、画面が開いていなくても進めて通知する。"""
    while True:
        time.sleep(SCHEDULER_SECONDS)
        try:
            run_locked(None)
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
            if not fn:
                return self._send(404, {"error": "not found"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                return self._send(200, run_locked(fn, body))
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
    server.serve_forever()


if __name__ == "__main__":
    main()
