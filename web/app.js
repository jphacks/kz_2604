// 画面（冷蔵コーチ）。判定ロジックは持たない。表示する内容はすべてAPIから受け取る。
"use strict";

const app = document.getElementById("app");

let view = "home";
let home = null;             // GET /api/home の結果
let ingredients = [];
let draft = null;            // 画面2の編集中データ
let sentImages = [];         // APIに送った直前の画像（画面2で確認できる）
let lastAction = null;       // 画面4の内容
let freezeResult = null;     // 5c・5d の内容
let cookItems = [];
let cookSelected = new Set();
let cookFromAsk = false;
let openedFrom = null;       // 通知から開いたとき {kind}。タップ数を数えるのに使う
let renderSeq = 0;           // 古い非同期描画が新しい画面を上書きしないように
let config = {};

// ---------------- 端末ごとのID・保存 ----------------

function store(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    if (value === null) localStorage.removeItem(key); else localStorage.setItem(key, value);
  } catch (e) { return null; }
}

function newId() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
  // http で開いたスマホなど、randomUUID が使えないとき
  const b = crypto.getRandomValues(new Uint8Array(16));
  b[6] = (b[6] & 0x0f) | 0x40; b[8] = (b[8] & 0x3f) | 0x80;
  const h = [...b].map(x => x.toString(16).padStart(2, "0")).join("");
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

const DEVICE_ID = store("reizo.device_id") || (() => { const id = newId(); store("reizo.device_id", id); return id; })();
let devMenuOpen = store("reizo.dev_menu") === "1";

// ---------------- 共通 ----------------

async function api(method, path, body) {
  const headers = { "X-Device-Id": DEVICE_ID };
  if (body) headers["Content-Type"] = "application/json";
  if (path.startsWith("/api/debug/")) headers["X-Debug-Password"] = store("reizo.debug_password") || "";
  const res = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : undefined });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

const WEEK = ["日", "月", "火", "水", "木", "金", "土"];
function ymd(iso) { return iso.slice(0, 10).split("-").map(Number); }
function weekday(iso) { const [y, m, d] = ymd(iso); return WEEK[new Date(Date.UTC(y, m - 1, d)).getUTCDay()]; }
function fmtDay(iso) { const [, m, d] = ymd(iso); return `${m}/${d}（${weekday(iso)}）`; }
function fmtDayLong(iso) { const [, m, d] = ymd(iso); return `${m}月${d}日（${weekday(iso)}）`; }
function fmtShort(iso) { const [, m, d] = ymd(iso); return `${m}/${d}`; }
function fmtTime(iso) { return iso.split("T")[1].slice(0, 5); }
function fmtSeconds(s) { return !s ? "" : s < 60 ? `${s}秒` : `${Math.round(s / 60)}分`; }
function stateTag(state, label) { return `<span class="tag ${esc(state)}">${esc(label)}</span>`; }
function names(list) { return list.join("・"); }
// 「おつかれさま。\n今日はこれだけ。」のように改行で区切った見出し
function title(text) { return `<h1>${text.split("\n").map(l => `<span class="br">${esc(l)}</span>`).join("")}</h1>`; }

const ICON = {
  camera: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 8h3l2-3h6l2 3h3v11H4z"/><circle cx="12" cy="13" r="3.5"/></svg>`,
  fridge: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="2.5" width="14" height="19" rx="2.5"/><path d="M5 10h14M9 6v2M9 13v3"/></svg>`,
  snow: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M12 2v20M3.3 7l17.4 10M3.3 17L20.7 7"/><path d="M9 4l3 2 3-2M9 20l3-2 3 2M4 10.5l3.4-.3L6 7M20 13.5l-3.4.3L18 17M4 13.5l3.4.3L6 17M20 10.5l-3.4-.3L18 7"/></svg>`,
  pan: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="10" cy="13" r="6.5"/><path d="M16.5 13H22M8 3.5c0 1 1 1.5 1 2.5M11.5 3.5c0 1 1 1.5 1 2.5"/></svg>`,
  bell: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M6 16V11a6 6 0 0 1 12 0v5l1.5 2h-15z"/><path d="M10 20a2 2 0 0 0 4 0"/></svg>`,
  bellOff: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M6 16V11a6 6 0 0 1 9.5-4.9M18 11v5l1.5 2H8"/><path d="M10 20a2 2 0 0 0 4 0M3 3l18 18"/></svg>`,
  check: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>`,
  out: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/></svg>`,
};

function show(v) {
  view = v;
  render();
  window.scrollTo(0, 0);
}

// 通知から開いたときは、通知のタップを1回と数える
function taps() { return openedFrom ? 2 : 1; }
function via(fallback) { return openedFrom ? "screen" : fallback; }

async function loadHome() {
  home = await api("GET", "/api/home");
  return home;
}

// ホームにいるときだけ、サーバー側で出すべき画面（4・5b）に切り替える
async function poll() {
  if (view !== "home") return;
  const prev = home && home.screen;
  try { await loadHome(); } catch (e) { return; }
  if (home.screen === "action" && prev !== "action") {
    lastAction = { action: home.today_ask.action, remind_at: home.today_ask.remind_at };
    show("action");
  } else if (home.screen === "freeze_check" && prev !== "freeze_check") {
    show("freeze");
  }
}

// ---------------- 料理の提案（画面0b・6で共通） ----------------

function dishesHtml(dishes, heading) {
  return `
    <h2>${esc(heading)}</h2>
    ${dishes.length ? dishes.map(d => `
      <div class="dish">
        <div class="dish-head"><b>${esc(d.name)}</b><span class="time">約${d.time_minutes}分</span></div>
        <div class="uses">${d.uses.map(u => `<span class="${d.soon_uses.includes(u) ? "soon" : ""}">${esc(u)}</span>`).join("")}</div>
        <a class="cookpad" href="${esc(d.url)}" target="_blank" rel="noopener">クックパッドで探す${ICON.out}</a>
      </div>`).join("")
      : `<p class="sub">早めに使いたい食材で作れる料理は、いまはありません。</p>`}
    <p class="note" style="text-align:left">時間はアプリの目安です。リンク先はクックパッドの検索結果です。</p>`;
}

function weeklyHtml(w) {
  if (!w.has_records) return `<div class="wk"><span class="k">今週</span><span class="quiet">守れた食材が、ここに出ます</span></div>`;
  return `<div class="wk"><span class="k">今週</span><span>守れた <b>${w.saved_count}</b>品</span>${
    w.missed_count ? `<span class="sep">／</span><span class="quiet">間に合わなかった ${w.missed_count}品</span>` : ""}</div>`;
}

// ---------------- 画面0 ホーム ----------------

async function renderHome(seq) {
  await loadHome();
  if (seq !== renderSeq) return;
  // 答えを待っている画面（4：救済行動、5b：冷凍庫に入れた？）があれば、そちらを出す
  if (home.screen === "action") {
    lastAction = { action: home.today_ask.action, remind_at: home.today_ask.remind_at };
    return show("action");
  }
  if (home.screen === "freeze_check") return show("freeze");
  const h = home;
  const notify = h.notify_today.will_notify
    ? `${ICON.bell}<span>今日は${esc(h.notify_today.time)}に聞きます</span>`
    : `${ICON.bellOff}<span>今日は通知なし</span>`;
  const card = h.ask.show
    ? `<div class="ask-card">
         <div class="ask-items"><span class="tag soon strong">早めに</span><b>${esc(names(h.ask.items))}</b></div>
         ${title("今日、作れそう？")}
         <p class="sub">押すだけでOK。返事しなくても大丈夫です。</p>
         <div class="row two"><button class="cook" id="home-cook">作る</button><button class="primary" id="home-skip">無理</button></div>
       </div>`
    : h.ask.answered
      ? `<div class="calm-card">${title("今日の返事は\n受け取りました")}<p class="sub">何もしなくて大丈夫です。</p></div>`
      : `<div class="calm-card">${title("今日は、急いで使う\n食材はありません")}<p class="sub">何もしなくて大丈夫です。</p></div>`;
  app.innerHTML = `
    <div class="screen">
      <div class="home-top">
        <div class="appname" id="appname">冷蔵コーチ</div>
        <button class="notify-chip ${h.notify_today.will_notify ? "" : "off"}" id="go-settings">${notify}</button>
      </div>
      <p class="label">${esc(fmtDayLong(h.now))}</p>
      ${card}
      <div class="tiles">
        <button class="tile-sm" id="go-fridge">${ICON.fridge}<span><b>冷蔵庫の中身</b><small>期限の目安が近い順</small></span></button>
        <button class="tile-sm" id="go-receipt">${ICON.camera}<span><b>レシートを撮る</b><small>買い物のあとに1回だけ</small></span></button>
      </div>
      ${weeklyHtml(h.weekly)}
      ${devMenuOpen ? `<div class="card dev" id="dev"></div>` : ""}
    </div>`;
  const askBtn = async answer => {
    const res = await api("POST", "/api/answer", { date: h.ask.date, answer, taps: 1, via: "home" });
    afterAnswer(answer, res);
  };
  const c = document.getElementById("home-cook");
  if (c) c.onclick = () => askBtn("cook");
  const s = document.getElementById("home-skip");
  if (s) s.onclick = () => askBtn("skip");
  document.getElementById("go-settings").onclick = () => show("settings");
  document.getElementById("go-fridge").onclick = () => show("fridge");
  document.getElementById("go-receipt").onclick = () => show("receipt");
  setupLongPress(document.getElementById("appname"));
  if (devMenuOpen) renderDevMenu(seq);
}

async function afterAnswer(answer, res) {
  openedFrom = null;
  if (answer === "skip") {
    lastAction = res;
    show("action");
  } else {
    cookItems = res.items;
    cookSelected = new Set();
    cookFromAsk = true;
    show("cook");
  }
}

// ---------------- 開発者メニュー（アプリ名を3秒長押し） ----------------

function setupLongPress(el) {
  let timer = null;
  const cancel = () => { clearTimeout(timer); timer = null; el.classList.remove("pressing"); };
  el.addEventListener("pointerdown", e => {
    e.preventDefault();
    el.classList.add("pressing");
    timer = setTimeout(() => {
      cancel();
      devMenuOpen = !devMenuOpen;
      store("reizo.dev_menu", devMenuOpen ? "1" : "0");
      if (navigator.vibrate) navigator.vibrate(40);
      render();
    }, 3000);
  });
  ["pointerup", "pointerleave", "pointercancel"].forEach(t => el.addEventListener(t, cancel));
  el.addEventListener("contextmenu", e => e.preventDefault());
}

async function renderDevMenu(seq) {
  const el = document.getElementById("dev");
  if (!el) return;
  let unlocked = false;
  if (store("reizo.debug_password")) {
    try { await api("GET", "/api/debug/ping"); unlocked = true; } catch (e) { /* パスワード違い */ }
  }
  if (seq !== renderSeq) return;
  el.innerHTML = `
    <h2>開発者メニュー</h2>
    <p class="sub">この端末のID：<code>${esc(DEVICE_ID)}</code></p>
    <p class="sub">いまの時刻（デモ用の時計）：${esc(fmtDay(home.now))} ${esc(fmtTime(home.now))}</p>
    ${unlocked ? `
      <div class="dev-buttons">
        <button id="d-stats">記録を見る</button>
        <button id="d-ask">18時にする（通知の代わり）</button>
        <button id="d-remind">リマインドを今すぐ出す</button>
        <button id="d-hour">1時間進める</button>
        <button id="d-morning">次の日の朝へ</button>
        <button id="d-reset">デモを初期化（10/26 19:00）</button>
      </div>
      <p class="error" id="d-err" hidden></p>`
    : `<div class="field"><input type="password" id="d-pw" placeholder="パスワード（.env の DEBUG_PASSWORD）" autocomplete="off">
        <button id="d-pw-save" style="width:auto;margin:0;min-height:44px">保存</button></div>
       <p class="error" id="d-err" hidden></p>`}
    <button class="link muted" id="d-close">閉じる</button>`;
  const err = m => { const p = document.getElementById("d-err"); p.hidden = false; p.textContent = m; };
  const run = fn => async () => { try { await fn(); render(); } catch (e) { err(e.message); } };
  const day = home.now.slice(0, 10);
  const next = new Date(Date.UTC(...ymd(day).map((v, i) => i === 1 ? v - 1 : v)) + 864e5).toISOString().slice(0, 10);
  const bind = (id, fn) => { const b = document.getElementById(id); if (b) b.onclick = fn; };
  bind("d-close", () => { devMenuOpen = false; store("reizo.dev_menu", "0"); render(); });
  bind("d-pw-save", async () => {
    store("reizo.debug_password", document.getElementById("d-pw").value);
    try { await api("GET", "/api/debug/ping"); render(); } catch (e) { store("reizo.debug_password", null); err(e.message); }
  });
  bind("d-stats", () => show("stats"));
  bind("d-ask", run(async () => {
    if (fmtTime(home.now) < "18:00") await api("POST", "/api/debug/advance", { to: `${day}T18:00:00+09:00` });
    await api("POST", "/api/debug/trigger-ask");
  }));
  bind("d-remind", run(() => api("POST", "/api/debug/trigger-remind")));
  bind("d-hour", run(() => api("POST", "/api/debug/advance", { minutes: 60 })));
  bind("d-morning", run(() => api("POST", "/api/debug/advance", { to: `${next}T09:00:00+09:00` })));
  bind("d-reset", run(() => api("POST", "/api/debug/reset", { scope: "demo" })));
}

// ---------------- 試用の記録（開発者メニュー） ----------------

async function renderStats(seq) {
  const s = await api("GET", "/api/debug/stats");
  if (seq !== renderSeq) return;
  const KIND = { ask: "問いかけ", remind: "リマインド" };
  const VIA = { notification: "通知のボタン", screen: "2択画面", home: "ホームのカード" };
  const VAL = { skip: "無理", cook: "作る", done: "入れた", later: "あとで", gone: "残っていなかった" };
  app.innerHTML = `
    <div class="screen">
      <p class="label">開発者メニュー</p>
      ${title("試用の記録")}
      <p class="sub">全端末の記録です。発表では人数をそのまま書き、「実証された」とは言わないでください。</p>
      <div class="stat-grid">
        <div class="stat"><small>端末</small><b>${s.devices}</b></div>
        <div class="stat"><small>通知→行動の平均</small><b>${s.avg_seconds ?? "—"}<i>秒</i></b></div>
        <div class="stat"><small>平均のタップ数</small><b>${s.avg_taps ?? "—"}</b></div>
        <div class="stat"><small>冷凍できた回数</small><b>${s.frozen_count}</b></div>
      </div>
      <div class="table-wrap"><table class="rec">
        <tr><th>端末</th><th>日付</th><th>種類</th><th>秒</th><th>タップ</th><th>どこから</th><th>答え</th></tr>
        ${s.rows.map(r => `<tr><td>${esc(r.device)}</td><td>${esc(r.date ? fmtShort(r.date) : "")}</td><td>${esc(KIND[r.kind])}</td>
          <td>${r.seconds == null ? "—" : Math.round(r.seconds)}</td><td>${r.taps ?? ""}</td><td>${esc(VIA[r.via] || r.via || "")}</td>
          <td>${esc(VAL[r.value] || r.value || "")}</td></tr>`).join("") || `<tr><td colspan="7" class="sub">まだ記録はありません</td></tr>`}
      </table></div>
      <div class="actions">
        <button id="csv">CSVでダウンロード</button>
        <button class="link muted" id="clear">記録を消す（デモの前に）</button>
        <button class="dark" id="back">もどる</button>
      </div>
    </div>`;
  document.getElementById("csv").onclick = async () => {
    const res = await fetch("/api/debug/stats.csv", { headers: { "X-Device-Id": DEVICE_ID, "X-Debug-Password": store("reizo.debug_password") || "" } });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(await res.blob());
    a.download = "reizo-coach-records.csv";
    a.click();
    URL.revokeObjectURL(a.href);
  };
  document.getElementById("clear").onclick = async () => {
    if (!confirm("全端末の試用の記録を消します。よろしいですか？")) return;
    await api("POST", "/api/debug/reset", { scope: "records" });
    render();
  };
  document.getElementById("back").onclick = () => show("home");
}

// ---------------- 画面0b 冷蔵庫の中身 ----------------

async function renderFridge(seq) {
  const [inv, sug] = await Promise.all([api("GET", "/api/inventory"), api("GET", "/api/suggestions")]);
  if (seq !== renderSeq) return;
  const row = (i, sub) => `
    <div class="item">
      <div class="body"><div class="name">${esc(i.name)}</div><div class="raw">${sub}</div></div>
      ${stateTag(i.state, i.state_label)}
      <button class="use" data-id="${esc(i.inventory_id)}">使った</button>
    </div>`;
  app.innerHTML = `
    <div class="screen">
      <p class="label">${esc(fmtDayLong(home.now))}</p>
      ${title("冷蔵庫の中身")}
      <h2>冷蔵（期限の目安が近い順）</h2>
      <div class="list">
        ${inv.fridge.length ? inv.fridge.map(i => row(i, `${esc(fmtShort(i.purchased_at))} に購入`)).join("")
          : `<p class="sub" style="padding:12px 0">冷蔵に登録された食材はありません。</p>`}
      </div>
      <h2>冷凍庫</h2>
      <div class="list">
        ${inv.freezer.length ? inv.freezer.map(i => row(i, `${esc(fmtShort(i.frozen_at))}に冷凍（${i.frozen_days + 1}日目）`)).join("")
          : `<p class="sub" style="padding:12px 0">冷凍した食材はありません。</p>`}
      </div>
      ${dishesHtml(sug.dishes, "作れそうな料理（冷凍を優先・時間は目安）")}
      <div class="actions"><button class="dark" id="back">ホームへ</button></div>
    </div>`;
  app.querySelectorAll(".use").forEach(b => b.onclick = async () => {
    b.disabled = true;
    await api("POST", "/api/inventory/use", { inventory_ids: [b.dataset.id] });
    render();
  });
  document.getElementById("back").onclick = () => show("home");
}

// ---------------- 画面0c 通知の時間 ----------------

const isIOS = /iPhone|iPad|iPod/.test(navigator.userAgent);
const isStandalone = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone;

function b64ToUint8(b64) {
  const s = atob((b64 + "=".repeat((4 - b64.length % 4) % 4)).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(s, c => c.charCodeAt(0));
}

async function currentSubscription() {
  if (!("serviceWorker" in navigator)) return null;
  const reg = await navigator.serviceWorker.getRegistration();
  return reg ? reg.pushManager.getSubscription() : null;
}

async function renderSettings(seq) {
  const [s, sub] = await Promise.all([api("GET", "/api/settings"), currentSubscription().catch(() => null)]);
  if (seq !== renderSeq) return;
  const supported = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
  let permission;
  if (!config.push_available) permission = `<p class="sub">サーバーで通知が使えません（pywebpush が入っていません）。</p>`;
  else if (!window.isSecureContext) permission = `<p class="sub">通知を受け取るには、https:// で始まるアドレスで開いてください。通知がなくても、ホームに同じ「今日、作れそう？」が出ます。</p>`;
  else if (!supported) permission = isIOS && !isStandalone
    ? `<p class="sub">iPhoneでは、共有ボタンから「ホーム画面に追加」し、追加したアイコンから開くと通知を受け取れます（iOS 16.4以降）。</p>`
    : `<p class="sub">このブラウザは通知に対応していません。通知がなくても、ホームに同じ「今日、作れそう？」が出ます。</p>`;
  else if (sub && Notification.permission === "granted")
    permission = `<p>この端末で通知を受け取ります。</p><div class="row"><button id="push-test">テスト通知</button><button id="push-off">止める</button></div>`;
  else permission = `<button class="primary" id="push-on">通知を許可する</button>`;

  app.innerHTML = `
    <div class="screen">
      <p class="label">設定</p>
      ${title("通知の時間")}
      <p class="sub">早めに使いたい食材がある日だけ、決めた時刻に聞きます。通知は1日に多くても3回です。</p>
      <div class="card">
        <label class="time-row"><span><b>問いかけ</b><small>「今日、作れそう？」</small></span>
          <input type="time" step="60" id="ask-time" value="${esc(s.ask_time)}"></label>
        <label class="time-row"><span><b>リマインド</b><small>「冷凍庫に入れた？」（冷凍をお願いした日だけ）</small></span>
          <input type="time" step="60" id="remind-time" value="${esc(s.remind_time)}"></label>
        <p class="error" id="warn" hidden>リマインドは、問いかけより後の時刻にしてください。</p>
      </div>
      <h2>通知</h2>
      <div class="card">${permission}<p class="error" id="push-err" hidden></p></div>
      <div class="actions">
        <button class="primary" id="save">保存する</button>
        <button class="link muted" id="back">もどる</button>
      </div>
    </div>`;
  const ask = document.getElementById("ask-time"), remind = document.getElementById("remind-time");
  const check = () => {
    const bad = !ask.value || !remind.value || remind.value <= ask.value;
    document.getElementById("warn").hidden = !bad;
    document.getElementById("save").disabled = bad;
  };
  ask.oninput = remind.oninput = check;
  check();
  document.getElementById("save").onclick = async () => {
    try {
      await api("PUT", "/api/settings", { ask_time: ask.value, remind_time: remind.value });
      show("home");
    } catch (e) { const w = document.getElementById("warn"); w.hidden = false; w.textContent = e.message; }
  };
  document.getElementById("back").onclick = () => show("home");
  const fail = e => { const p = document.getElementById("push-err"); p.hidden = false; p.textContent = e.message || String(e); };
  const on = document.getElementById("push-on");
  if (on) on.onclick = async () => {
    on.disabled = true;
    try {
      if (await Notification.requestPermission() !== "granted") throw new Error("通知が許可されませんでした。ブラウザの設定から許可してください");
      const reg = await navigator.serviceWorker.register("/sw.js");
      await navigator.serviceWorker.ready;
      const ps = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToUint8(config.vapid_public_key) });
      await api("POST", "/api/push/subscribe", { subscription: ps.toJSON() });
      render();
    } catch (e) { on.disabled = false; fail(e); }
  };
  const test = document.getElementById("push-test");
  if (test) test.onclick = async () => {
    try { await api("POST", "/api/push/subscribe", { subscription: sub.toJSON() }); await api("POST", "/api/push/test"); }
    catch (e) { fail(e); }
  };
  const off = document.getElementById("push-off");
  if (off) off.onclick = async () => {
    try { await api("POST", "/api/push/unsubscribe", { endpoint: sub.endpoint }); await sub.unsubscribe(); render(); }
    catch (e) { fail(e); }
  };
}

// ---------------- 画面1 レシート撮影 ----------------
// 主：LLM（画像を送って文字起こし）→ 予備：端末の中の Tesseract.js → 最後：デモ用の固定データ。
// 食材への当てはめはどの経路でもサーバー側のコード（server/ocr_match.py）。

const TESSERACT_URL = "https://cdn.jsdelivr.net/npm/tesseract.js@5.1.1/dist/tesseract.min.js";
const TESS_LANG_PATH = "https://cdn.jsdelivr.net/npm/@tesseract.js-data/jpn/4.0.0_best_int";
const API_LONG_SIDE = 1568;   // LLM に送る1枚の長辺
const SPLIT_ASPECT = 2.0;     // 縦横比がこれを超える縦長のレシートは上下に分ける

function renderReceipt(error) {
  app.innerHTML = `
    <div class="screen">
      <p class="label">買い物のあと</p>
      ${title("レシートを撮ってください")}
      <p class="sub">撮るのは買い物のあと1回だけ。疲れた日は、何もしなくて大丈夫です。</p>
      ${error ? `<p class="error">${esc(error)}</p>` : ""}
      <div class="camera" id="camera">
        <div class="paper"><b>［店名］</b><span>国産豚コマ切落</span><span>もやし</span><span>たまご10コ</span><span>キャベツ1/2</span><span>絹とうふ</span></div>
        <span class="corner tl"></span><span class="corner tr"></span><span class="corner bl"></span><span class="corner br"></span>
      </div>
      <p class="note">レシート全体が入るように、明るい場所でまっすぐ撮ってください。長いレシートは自動で上下に分けて読みます。</p>
      <input type="file" id="file" accept="image/*" capture="environment" hidden>
      <input type="file" id="pick" accept="image/*" hidden>
      <div class="actions">
        <button class="primary" id="shoot">${ICON.camera}撮影する</button>
        <div class="row">
          <button class="link" id="later">あとで撮る</button>
          <button class="link muted" id="pick-btn">写真から選ぶ</button>
          ${devMenuOpen ? `<button class="link muted" id="fixture">デモ用レシート</button>` : ""}
        </div>
      </div>
    </div>`;
  const file = document.getElementById("file");
  const pick = document.getElementById("pick");
  document.getElementById("shoot").onclick = () => file.click();
  document.getElementById("pick-btn").onclick = () => pick.click();
  document.getElementById("later").onclick = () => show("home");
  const fx = document.getElementById("fixture");
  if (fx) fx.onclick = async () => { sentImages = []; startConfirm(await api("POST", "/api/receipts", { use_fixture: true })); };
  const onFile = async input => {
    if (!input.files[0]) return;
    try { readReceipt(await imageFileToCanvas(input.files[0])); }
    catch (e) { renderReceipt("画像を開けませんでした"); }
  };
  file.onchange = () => onFile(file);
  pick.onchange = () => onFile(pick);
}

function imageFileToCanvas(f) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => {
      const scale = Math.min(1, 4000 / Math.max(img.width, img.height));
      const c = document.createElement("canvas");
      c.width = Math.round(img.width * scale);
      c.height = Math.round(img.height * scale);
      c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
      URL.revokeObjectURL(img.src);
      resolve(c);
    };
    img.onerror = reject;
    img.src = URL.createObjectURL(f);
  });
}

function sub(src, x, y, w, h, scale = 1) {
  const c = document.createElement("canvas");
  c.width = Math.max(1, Math.round(w * scale));
  c.height = Math.max(1, Math.round(h * scale));
  c.getContext("2d").drawImage(src, x, y, w, h, 0, 0, c.width, c.height);
  return c;
}

// レシート（明るい紙）の部分だけを切り出す。左右の余白を落として文字を大きく保つ
function cropToReceipt(src) {
  const s = Math.min(1, 400 / Math.max(src.width, src.height));
  const small = sub(src, 0, 0, src.width, src.height, s);
  const { data, width: w, height: h } = small.getContext("2d").getImageData(0, 0, small.width, small.height);
  const cols = new Float64Array(w), rows = new Float64Array(h);
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
    const i = (y * w + x) * 4;
    const g = data[i] * 0.299 + data[i + 1] * 0.587 + data[i + 2] * 0.114;
    cols[x] += g / h; rows[y] += g / w;
  }
  const bounds = arr => {
    const lo = Math.min(...arr), hi = Math.max(...arr);
    if (hi - lo < 40) return [0, arr.length - 1];  // 紙と背景の差がない（紙が画面いっぱい）なら切らない
    const thr = lo + (hi - lo) * 0.55;
    let a = 0, b = arr.length - 1;
    while (a < b && arr[a] < thr) a++;
    while (b > a && arr[b] < thr) b--;
    return (b - a) < arr.length * 0.3 ? [0, arr.length - 1] : [a, b];
  };
  const [x0, x1] = bounds(cols), [y0, y1] = bounds(rows);
  const mx = (x1 - x0) * 0.03, my = (y1 - y0) * 0.02;
  const X = Math.max(0, (x0 - mx) / s), Y = Math.max(0, (y0 - my) / s);
  const W = Math.min(src.width, (x1 + mx) / s) - X, H = Math.min(src.height, (y1 + my) / s) - Y;
  return sub(src, X, Y, W, H);
}

// 縦長のレシートは少し重ねて上下に分け、1枚ずつ長辺を API_LONG_SIDE 以下にする
function splitForApi(src) {
  const n = src.height / src.width > SPLIT_ASPECT ? Math.ceil(src.height / src.width / 1.6) : 1;
  const overlap = n > 1 ? Math.round(src.height * 0.05) : 0;
  const step = src.height / n;
  const pieces = [];
  for (let i = 0; i < n; i++) {
    const y = Math.max(0, Math.round(i * step - overlap));
    const h = Math.min(src.height - y, Math.round(step + overlap * 2));
    const scale = Math.min(1, API_LONG_SIDE / Math.max(src.width, h));
    pieces.push(sub(src, 0, y, src.width, h, scale));
  }
  return pieces;
}

// 予備のOCR用：白黒にしてコントラストを上げる
function prepareForOcr(src) {
  const scale = Math.min(2, 2000 / Math.max(src.width, src.height));
  const c = sub(src, 0, 0, src.width, src.height, scale);
  const ctx = c.getContext("2d");
  const img = ctx.getImageData(0, 0, c.width, c.height);
  const px = img.data;
  const hist = new Uint32Array(256);
  for (let i = 0; i < px.length; i += 4) {
    const g = (px[i] * 299 + px[i + 1] * 587 + px[i + 2] * 114) / 1000 | 0;
    px[i] = g;
    hist[g]++;
  }
  const total = px.length / 4;
  let lo = 0, hi = 255, acc = 0;
  for (; lo < 255 && (acc += hist[lo]) < total * 0.02; lo++);
  acc = 0;
  for (; hi > 0 && (acc += hist[hi]) < total * 0.02; hi--);
  const range = Math.max(1, hi - lo);
  for (let i = 0; i < px.length; i += 4) {
    const v = Math.max(0, Math.min(255, (px[i] - lo) * 255 / range));
    px[i] = px[i + 1] = px[i + 2] = v;
  }
  ctx.putImageData(img, 0, 0);
  return c;
}

function loadTesseract() {
  if (window.Tesseract) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = TESSERACT_URL;
    s.onload = resolve;
    s.onerror = () => reject(new Error("文字読み取りの部品を読み込めませんでした"));
    document.head.appendChild(s);
  });
}

async function tesseractText(canvas, onProgress) {
  await loadTesseract();
  const worker = await Tesseract.createWorker("jpn", 1, {
    langPath: TESS_LANG_PATH,
    logger: m => { if (m.status === "recognizing text") onProgress(m.progress); },
  });
  await worker.setParameters({ preserve_interword_spaces: "1" });
  const { data } = await worker.recognize(canvas);
  await worker.terminate();
  return data.text;
}

async function readReceipt(canvas) {
  const cropped = cropToReceipt(canvas);
  const pieces = splitForApi(cropped);
  sentImages = pieces.map(p => p.toDataURL("image/jpeg", 0.92));  // 強い圧縮はしない
  app.innerHTML = `
    <div class="screen">
      <p class="label">買い物のあと</p>
      ${title("読み取っています")}
      <div class="shots">${sentImages.map(u => `<img class="shot" src="${u}" alt="送る画像">`).join("")}</div>
      <div class="progress"><div id="bar"></div></div>
      <p class="sub" id="ocr-msg">レシートの文字を読み取っています…</p>
      <div class="actions"><button class="link muted" id="cancel">やめる</button></div>
    </div>`;
  let cancelled = false;
  document.getElementById("cancel").onclick = () => { cancelled = true; show("receipt"); };
  const bar = document.getElementById("bar");
  const msg = document.getElementById("ocr-msg");
  bar.style.width = "30%";

  const reasons = [];
  try {  // 1. LLM（主）
    const result = await api("POST", "/api/receipts", {
      images: sentImages.map(u => ({ data: u.split(",")[1], media_type: "image/jpeg" })),
    });
    if (!cancelled) startConfirm(result);
    return;
  } catch (e) { reasons.push(`AIでの読み取り：${e.message}`); }
  if (cancelled) return;

  try {  // 2. 端末の中の Tesseract.js（予備）
    msg.textContent = "予備の方法（端末の中の文字読み取り）で読んでいます…";
    bar.style.width = "0%";
    const text = await tesseractText(prepareForOcr(cropped), p => { bar.style.width = `${Math.round(p * 100)}%`; });
    if (cancelled) return;
    const result = await api("POST", "/api/receipts", { ocr_text: text });
    startConfirm(result, reasons);
    return;
  } catch (e) { reasons.push(`端末の文字読み取り：${e.message}`); }
  if (cancelled) return;

  startConfirm(await api("POST", "/api/receipts", { use_fixture: true }), reasons);  // 3. デモ用の固定データ
}

// ---------------- 画面2 読み取り結果の確認（チェックして登録） ----------------

const ENGINE_LABEL = { llm: "AIで読み取りました", tesseract: "予備の方法（端末の中の文字読み取り）で読み取りました", fixture: "デモ用の固定データです" };

function startConfirm(result, reasons = []) {
  draft = {
    purchased_at: result.purchased_at,
    engine: result.engine, provider: result.provider, model: result.model, transcript: result.transcript, reasons,
    rows: [
      ...result.matched.map(m => ({ ...m, kind: "matched", checked: true, original: m.ingredient_id, editing: false })),
      ...result.unmatched.map(u => ({ ...u, kind: "unmatched", checked: false, ingredient_id: u.candidates[0]?.id || "" })),
    ],
  };
  show("confirm");
}

// 選んだ食材の「早めに」などはサーバーで計算する
async function refreshTags() {
  const rows = draft.rows.filter(r => r.ingredient_id);
  const { items } = await api("POST", "/api/inventory/preview", {
    purchased_at: draft.purchased_at, items: rows.map(r => ({ ingredient_id: r.ingredient_id })),
  });
  rows.forEach((r, i) => Object.assign(r, { name: items[i].name, freshness: items[i].freshness, freshness_label: items[i].freshness_label }));
  render();
}

function ingredientOptions(row) {
  const cands = (row.candidates || []).map(c => c.id);
  const opt = i => `<option value="${esc(i.id)}" ${i.id === row.ingredient_id ? "selected" : ""}>${esc(i.name)}</option>`;
  const first = ingredients.filter(i => cands.includes(i.id));
  const rest = ingredients.filter(i => !cands.includes(i.id));
  return `${row.ingredient_id ? "" : `<option value="" selected>食材を選ぶ</option>`}
    ${first.length ? `<optgroup label="候補">${first.map(opt).join("")}</optgroup><optgroup label="ほかの食材">${rest.map(opt).join("")}</optgroup>` : rest.map(opt).join("")}`;
}

function checkRow(row, idx) {
  const showSelect = row.checked && (row.kind !== "matched" || row.editing);
  return `
    <div class="check-row ${row.checked ? "on" : ""}" data-idx="${idx}" role="checkbox" aria-checked="${row.checked}" tabindex="0">
      <span class="box">${row.checked ? ICON.check : ""}</span>
      <div class="body">
        <div class="name">${row.kind === "unmatched" && !row.checked ? esc(row.raw) : esc(row.name || "食材を選ぶ")}</div>
        <div class="raw">${row.raw ? `レシート表記：${esc(row.raw)}` : "手で追加"}${row.jan ? `（JAN ${esc(row.jan)}）` : ""}</div>
        ${showSelect ? `<select class="pick" data-idx="${idx}" onclick="event.stopPropagation()">${ingredientOptions(row)}</select>` : ""}
      </div>
      ${row.checked && row.freshness && row.freshness !== "none" ? stateTag(row.freshness, row.freshness_label) : ""}
      ${row.kind === "matched" && row.checked && !row.editing ? `<button class="fix" data-idx="${idx}">直す</button>` : ""}
    </div>`;
}

function renderConfirm() {
  const matched = draft.rows.map((r, i) => [r, i]).filter(([r]) => r.kind !== "unmatched");
  const unmatched = draft.rows.map((r, i) => [r, i]).filter(([r]) => r.kind === "unmatched");
  const n = draft.rows.filter(r => r.checked && r.ingredient_id).length;
  app.innerHTML = `
    <div class="screen">
      <p class="label">${esc(fmtDay(draft.purchased_at))}のレシート</p>
      ${title("登録する食材にチェック")}
      <div class="list checks">${matched.length ? matched.map(([r, i]) => checkRow(r, i)).join("")
        : `<p class="sub" style="padding:12px 0">認識できた食材はありません。下の行から選ぶか、追加してください。</p>`}</div>
      ${unmatched.length ? `
        <h2>認識できなかった行</h2>
        <p class="sub">食材だけチェックしてください。食材でなければ、そのままでOK。</p>
        <div class="list checks">${unmatched.map(([r, i]) => checkRow(r, i)).join("")}</div>` : ""}
      <div class="field" id="add-row" hidden><select id="addsel">${ingredients.map(i => `<option value="${esc(i.id)}">${esc(i.name)}</option>`).join("")}</select>
        <button id="add" style="width:auto;margin:0;min-height:44px">追加</button></div>
      <button class="link" id="add-open">読み取れなかった食材を追加</button>
      <div class="field"><label class="sub" for="pdate">購入日</label><input type="date" id="pdate" value="${esc(draft.purchased_at)}"></div>
      <p class="sub">「早めに」などの目安：USDA FoodKeeper・農林水産省ほか（<a href="#" id="src">目安と出典</a>）。期限は推定で、短めに取っています。</p>
      <details class="ocr">
        <summary>${esc(ENGINE_LABEL[draft.engine] || "")}${draft.model ? `（${esc(draft.model)}）` : ""}</summary>
        ${draft.reasons.length ? `<p>うまくいかなかった方法：${draft.reasons.map(esc).join(" ／ ")}</p>` : ""}
        ${sentImages.length ? `<p>送った画像（${sentImages.length}枚）</p><div class="shots small">${sentImages.map(u => `<img src="${u}" alt="">`).join("")}</div>` : ""}
        ${draft.transcript ? `<p>読み取った文字</p><pre>${esc(draft.transcript)}</pre>` : ""}
      </details>
      <div class="actions">
        <button class="primary" id="ok" ${n ? "" : "disabled"}>${n}品を登録する</button>
        <button class="link muted" id="cancel">やめる</button>
      </div>
    </div>`;

  app.querySelectorAll(".check-row").forEach(el => {
    const toggle = () => {
      const r = draft.rows[Number(el.dataset.idx)];
      r.checked = !r.checked;
      if (r.checked && r.ingredient_id) refreshTags(); else render();
    };
    el.onclick = toggle;
    el.onkeydown = e => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); toggle(); } };
  });
  app.querySelectorAll(".fix").forEach(b => b.onclick = e => { e.stopPropagation(); draft.rows[Number(b.dataset.idx)].editing = true; render(); });
  app.querySelectorAll(".pick").forEach(s => s.onchange = () => { draft.rows[Number(s.dataset.idx)].ingredient_id = s.value; refreshTags(); });
  document.getElementById("pdate").onchange = e => { draft.purchased_at = e.target.value; refreshTags(); };
  document.getElementById("src").onclick = e => { e.preventDefault(); show("sources"); };
  document.getElementById("add-open").onclick = () => { document.getElementById("add-row").hidden = false; document.getElementById("add-open").hidden = true; };
  document.getElementById("add").onclick = () => {
    draft.rows.push({ kind: "manual", checked: true, ingredient_id: document.getElementById("addsel").value, raw: "", jan: null, editing: true });
    refreshTags();
  };
  document.getElementById("cancel").onclick = () => { draft = null; show("home"); };
  document.getElementById("ok").onclick = async () => {
    const items = draft.rows.filter(r => r.checked && r.ingredient_id).map(r => ({
      line_id: r.line_id, ingredient_id: r.ingredient_id, raw: r.raw, jan: r.jan,
      // 自分で選んだ行（認識できなかった行・直した行）は、次から自動で当たるように覚える
      learn: r.kind === "unmatched" || (r.kind === "matched" && r.ingredient_id !== r.original),
    }));
    await api("POST", "/api/inventory/confirm", { purchased_at: draft.purchased_at, items });
    draft = null;
    sentImages = [];
    show("home");
  };
}

// ---------------- 画面3b 今日、作れそう？（通知をタップしたとき） ----------------

function renderAsk() {
  const a = home.ask;
  app.innerHTML = `
    <div class="screen">
      <div class="ask-items"><span class="tag soon strong">早めに</span><b>${esc(names(a.items))}</b></div>
      ${title("今日、作れそう？")}
      <p class="sub">押すだけでOK。決めるのはアプリがやります。</p>
      <div class="big-choice">
        <button class="cook" id="cook">作る</button>
        <button class="primary" id="skip">無理</button>
      </div>
      <p class="note">返事がなければ「無理」として受け取ります。</p>
      <div class="actions"><button class="link muted" id="back">ホームへ</button></div>
    </div>`;
  const send = async answer => afterAnswer(answer, await api("POST", "/api/answer", { date: a.date, answer, taps: taps(), via: via("screen") }));
  document.getElementById("cook").onclick = () => send("cook");
  document.getElementById("skip").onclick = () => send("skip");
  document.getElementById("back").onclick = () => { openedFrom = null; show("home"); };
}

// ---------------- 画面4 救済行動 ----------------

function renderAction() {
  const a = lastAction && lastAction.action;
  const date = home && home.today_ask ? home.today_ask.date : home.ask.date;
  const ack = async () => { await api("POST", "/api/action/ack", { date }); show("home"); };
  if (!a) {
    app.innerHTML = `
      <div class="screen">
        ${title("おつかれさま。\n今日はもう大丈夫。")}
        <p class="sub">いま急いでやることはありません。</p>
        <div class="actions"><button class="dark" id="ok">わかった</button></div>
      </div>`;
    document.getElementById("ok").onclick = ack;
    return;
  }
  app.innerHTML = `
    <div class="screen">
      ${title("おつかれさま。\n今日はこれだけ。")}
      <div class="rescue">
        <div class="top">${a.kind === "freeze" ? ICON.snow : ICON.pan}${a.seconds ? `<span class="pill">${esc(fmtSeconds(a.seconds))}</span>` : ""}</div>
        <p class="text">${esc(a.text)}</p>
        ${a.note ? `<p class="small">${esc(a.note)}</p>` : ""}
        <p class="storage">${a.storage ? `保存の目安：${esc(a.storage)}` : `目安の出典：${esc(a.source)}`}</p>
      </div>
      ${lastAction.remind_at ? `<p class="note">${esc(fmtTime(lastAction.remind_at))}に、できたか1回だけ聞きます。</p>` : ""}
      <div class="actions">
        <button class="dark" id="ok">わかった</button>
        <button class="link" id="used">${esc(a.name)}はもう使った</button>
      </div>
    </div>`;
  document.getElementById("ok").onclick = ack;
  document.getElementById("used").onclick = async () => {
    lastAction = await api("POST", "/api/action/already-used", { date });
    render();
  };
}

// ---------------- 画面5b〜5d 冷凍庫に入れた？ ----------------

function renderFreeze() {
  if (freezeResult) return renderFreezeResult();
  const fc = home.freeze_check;
  app.innerHTML = `
    <div class="screen">
      <p class="label">${esc(fmtTime(home.now))}</p>
      ${title(`${fc.name}、\n冷凍庫に入れた？`)}
      <p class="sub">まだなら、${esc(fc.how)}だけ。${esc(fmtSeconds(fc.seconds))}です。</p>
      <div class="big-choice">
        <button class="primary" id="done">入れた</button>
        <button id="later">あとで</button>
      </div>
      <button class="link" id="gone">もう残っていなかった</button>
    </div>`;
  const send = async answer => {
    const res = await api("POST", "/api/freeze-check", { date: fc.date, answer, taps: taps(), via: via("screen") });
    openedFrom = null;
    freezeResult = { answer, name: fc.name, ...res };
    render();
  };
  document.getElementById("done").onclick = () => send("done");
  document.getElementById("later").onclick = () => send("later");
  document.getElementById("gone").onclick = () => send("gone");
}

function renderFreezeResult() {
  const r = freezeResult;
  let body;
  if (r.answer === "done") {  // 5d
    body = `
      <div class="done-icon">${ICON.check}</div>
      ${title(`${r.name}、\n救えました。`)}
      <p class="sub">今日はもう何もしなくて大丈夫。ゆっくり休んでください。</p>
      ${weeklyHtml(r.weekly)}`;
  } else if (r.answer === "later") {  // 5c
    body = r.reask_at
      ? `${title("了解。")}<p class="lead">${esc(fmtTime(r.reask_at))}に、もう一度だけ聞きます。</p>
         <p class="sub">それで今日はおしまい。返事がなくても大丈夫です。</p>`
      : `${title("了解。")}<p class="lead">今日はここまでにします。</p><p class="sub">返事がなくても大丈夫です。</p>`;
  } else {
    body = `${title("教えてくれて\nありがとう。")}<p class="sub">${esc(r.name)}を在庫から消しました。</p>`;
  }
  app.innerHTML = `<div class="screen">${body}<div class="actions"><button class="dark" id="home">ホームへ</button></div></div>`;
  document.getElementById("home").onclick = () => { freezeResult = null; show("home"); };
}

// ---------------- 画面6 使った食材（「作る」） ----------------

async function renderCook(seq) {
  const sug = await api("GET", "/api/suggestions");
  if (seq !== renderSeq) return;
  const n = cookSelected.size;
  app.innerHTML = `
    <div class="screen">
      <p class="label">${cookFromAsk ? "「作る」を受け取りました" : "使った食材の記録"}</p>
      ${title(cookFromAsk ? "いいね。\n使ったものをタップ" : "使ったものをタップ")}
      <p class="sub">古い順に並んでいます。あとでまとめて押してもOK。</p>
      <div class="chips">
        ${cookItems.length ? cookItems.map(i => `
          <button class="chip ${cookSelected.has(i.id) ? "selected" : ""}" data-id="${esc(i.id)}">
            <span>${esc(i.name)}</span><small>${i.frozen ? "冷凍" : esc(fmtShort(i.purchased_at))}${i.freshness === "soon" ? "・早めに" : ""}</small>
          </button>`).join("") : `<p class="sub">登録された食材はありません。</p>`}
      </div>
      <p class="sub">${n ? `${n}品を在庫から消します` : "選んだ食材を在庫から消します"}</p>
      ${dishesHtml(sug.dishes, "何を作るか迷ったら（時間は目安）")}
      <div class="actions">
        <button class="primary" id="save" ${n ? "" : "disabled"}>記録する</button>
        <button class="link muted" id="back">ホームへ</button>
      </div>
    </div>`;
  app.querySelectorAll(".chip").forEach(b => b.onclick = () => {
    const id = b.dataset.id;
    cookSelected.has(id) ? cookSelected.delete(id) : cookSelected.add(id);
    render();
  });
  document.getElementById("save").onclick = async () => {
    await api("POST", "/api/inventory/use", { inventory_ids: [...cookSelected] });
    cookSelected = new Set();
    show("home");
  };
  document.getElementById("back").onclick = () => show("home");
}

// ---------------- 目安と出典 ----------------

async function renderSources(seq) {
  const s = await api("GET", "/api/sources");
  if (seq !== renderSeq) return;
  const refName = id => (s.sources[id] || {}).name || id;
  app.innerHTML = `
    <div class="screen">
      <p class="label">食材表</p>
      ${title("目安と出典")}
      <p class="sub">冷蔵の日数と冷凍できるかは USDA FoodKeeper（購入日から）を基本にし、冷凍の期間は日本の家庭の冷凍庫を前提に農林水産省の「2〜3週間」の短い方（14日）を使っています。食い違うときは短い方（安全側）に合わせています。</p>
      <p class="sub">パッケージに消費期限が書いてあれば、そちらが最優先です。この表は「期限が分からないときの目安」です。</p>
      ${s.items.map(i => `
        <div class="src-item">
          <div class="top"><b>${esc(i.name)}</b>${i.track_expiry ? `<span class="tag ${i.fridge_days <= 2 ? "soon" : "ok"}">冷蔵 ${i.fridge_days}日${i.freezer_days ? `・冷凍 ${i.freezer_days}日` : ""}</span>` : `<span class="tag none">期限なし</span>`}</div>
          <p>${esc(i.fridge_basis)}</p>
          ${i.track_expiry ? `<p>救済行動：${esc(i.rescue)}</p>` : ""}
          ${i.action_basis ? `<p>${esc(i.action_basis)}</p>` : ""}
          ${i.refs.length ? `<p>出典：${i.refs.map(r => esc(refName(r))).join("／")}</p>` : ""}
        </div>`).join("")}
      <h2>出典一覧</h2>
      <ul class="src-list">
        ${Object.values(s.sources).map(r => `<li><a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.name)}</a><br><span class="sub">${esc(r.used_for)}</span></li>`).join("")}
      </ul>
      <div class="actions"><button class="dark" id="back">もどる</button></div>
    </div>`;
  document.getElementById("back").onclick = () => show(draft ? "confirm" : "home");
}

// ---------------- 描画 ----------------

async function render() {
  const seq = ++renderSeq;
  document.body.dataset.view = view;
  try {
    if (view === "home") await renderHome(seq);
    else if (view === "fridge") await renderFridge(seq);
    else if (view === "settings") await renderSettings(seq);
    else if (view === "receipt") renderReceipt();
    else if (view === "confirm") renderConfirm();
    else if (view === "ask") renderAsk();
    else if (view === "action") renderAction();
    else if (view === "freeze") renderFreeze();
    else if (view === "cook") await renderCook(seq);
    else if (view === "sources") await renderSources(seq);
    else if (view === "stats") await renderStats(seq);
  } catch (e) {
    app.innerHTML = `<div class="screen"><p class="error">エラー：${esc(e.message)}</p><button class="dark" id="home">ホームへ</button></div>`;
    document.getElementById("home").onclick = () => show("home");
  }
}

// 通知から開いたとき（/?open=ask など）に出す画面を決める
async function openFromNotification(kind) {
  await loadHome();
  if (kind === "ask" && home.ask.show) { openedFrom = { kind }; return show("ask"); }
  if (kind === "remind" && home.screen === "freeze_check") { openedFrom = { kind }; return show("freeze"); }
  if (kind === "action" && home.today_ask && home.today_ask.action) {
    lastAction = { action: home.today_ask.action, remind_at: home.today_ask.remind_at };
    return show("action");
  }
  if (kind === "cook") {
    cookItems = (await api("GET", "/api/cook-items")).items;
    cookSelected = new Set();
    cookFromAsk = true;
    return show("cook");
  }
  show("home");
}

if ("serviceWorker" in navigator && window.isSecureContext) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
  // アプリが開いているときに通知がタップされた
  navigator.serviceWorker.addEventListener("message", e => { if (e.data && e.data.type === "open") openFromNotification(e.data.kind); });
}
document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });

(async function init() {
  [config, { items: ingredients }] = await Promise.all([api("GET", "/api/config"), api("GET", "/api/ingredients")]);
  const open = new URLSearchParams(location.search).get("open");
  if (open) history.replaceState(null, "", "/");
  await (open ? openFromNotification(open) : render());
  setInterval(poll, 5000);
})();
