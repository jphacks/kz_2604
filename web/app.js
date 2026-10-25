// 画面（冷蔵コーチ）。判定ロジックは持たない。表示する内容はすべてAPIから受け取る。
"use strict";

const app = document.getElementById("app");

let view = "home";
let home = null;             // GET /api/home の結果
let draft = null;            // 画面2の編集中データ
let sentImages = [];         // APIに送った直前の画像（画面2で確認できる）
let lastAction = null;       // 画面4の内容（POST /api/answer の結果）
let freezeResult = null;     // 5c・5d の内容
let cook = null;             // 画面6 { items, selected:Set, yesterday }
let fridgeTab = "fridge";
let removing = null;         // 冷蔵庫の中身で「消す」を選んでいるとき Set
let openedFrom = null;       // 通知から開いたとき {kind}。タップ数を数えるのに使う
let renderSeq = 0;           // 古い非同期描画が新しい画面を上書きしないように
let config = {};
let devOpen = false;         // 開発者メニュー（アプリ名を3秒長押し）
let devMsg = null;           // 開発者メニューで操作したあとに出す一言

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
function soonBadge(soon) { return soon ? `<span class="tag soon">早めに</span>` : ""; }
function names(list) { return list.join("・"); }
// 「おつかれさま。\n今日はこれだけ。」のように改行で区切った見出し
function title(text) { return `<h1>${text.split("\n").map(l => `<span class="br">${esc(l)}</span>`).join("")}</h1>`; }

const ICON = {
  camera: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 8h3l2-3h6l2 3h3v11H4z"/><circle cx="12" cy="13" r="3.5"/></svg>`,
  fridge: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="2.5" width="14" height="19" rx="2.5"/><path d="M5 10h14M9 6v2M9 13v3"/></svg>`,
  snow: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M12 2v20M3.3 7l17.4 10M3.3 17L20.7 7"/><path d="M9 4l3 2 3-2M9 20l3-2 3 2M4 10.5l3.4-.3L6 7M20 13.5l-3.4.3L18 17M4 13.5l3.4.3L6 17M20 10.5l-3.4-.3L18 7"/></svg>`,
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

async function poll() {
  if (view !== "home" || devOpen) return;
  const before = home && JSON.stringify([home.today_state, home.bell.label, home.now.slice(0, 16)]);
  try { await loadHome(); } catch (e) { return; }
  if (JSON.stringify([home.today_state, home.bell.label, home.now.slice(0, 16)]) !== before) render();
}

// ---------------- 共通の部品 ----------------

function dishesHtml(dishes, heading) {
  return `
    <h2>${esc(heading)}</h2>
    ${dishes.length ? dishes.map(d => `
      <div class="dish">
        <div class="dish-head"><b>${esc(d.name)}</b><span class="time">約${d.time_minutes}分</span></div>
        <div class="uses">${d.uses.map(u => `<span class="${d.soon_uses.includes(u) ? "soon" : ""}">${esc(u)}</span>`).join("")}</div>
        <a class="cookpad" href="${esc(d.url)}" target="_blank" rel="noopener">クックパッドで探す${ICON.out}</a>
      </div>`).join("")
      : `<p class="sub">いまある食材で作れる料理は、まだ見つかりません。</p>`}`;
}

function weeklyHtml(w) {
  if (!w.has_records) return `<div class="wk"><span class="k">今週</span><span class="quiet">守れた食材が、ここに出ます</span></div>`;
  return `<div class="wk"><span class="k">今週</span><span>守れた <b>${w.saved_count}</b>品</span>${
    w.missed_count ? `<span class="sep">／</span><span class="quiet">間に合わなかった ${w.missed_count}品</span>` : ""}</div>`;
}

function rescueCard(a, compact) {
  return `
    <div class="rescue">
      <div class="top">${ICON.snow}<span class="pill">${esc(fmtSeconds(a.seconds))}</span></div>
      <p class="text">${esc(a.text)}</p>
      ${compact ? "" : `<p class="small">${esc(a.note)}</p><p class="storage">${esc(a.freezer_note)}（出典：${esc(a.source)}）</p>`}
    </div>`;
}

// ---------------- 画面0 ホーム ----------------

function homeCard(h) {
  const c = h.card;
  switch (h.today_state) {
    case "unanswered":
      return `<div class="ask-card">
          <div class="ask-items"><span class="tag soon strong">早めに</span><b>${esc(names(c.items))}</b></div>
          ${title("今日、作れそう？")}
          <p class="sub">押すだけでOK。返事しなくても大丈夫です。</p>
          <div class="row two"><button class="choice" id="home-cook">作る</button><button class="choice" id="home-skip">今日は無理</button></div>
        </div>`;
    case "skip_pending":
      return `<div class="ask-card">
          <p class="label">今日はこれだけ</p>
          ${rescueCard(c.action, true)}
          <button class="primary" id="home-done">入れた</button>
          <button class="link" id="home-used">${esc(c.action.name)}はもう使った</button>
        </div>`;
    case "done":
      return `<div class="calm-card">${title("今日はもう何もしなくて\n大丈夫")}
          ${c.action && c.result === "frozen"
            ? `<p class="sub">${esc(c.action.name)}は冷凍庫へ。冷凍の目安は${esc(fmtShort(c.until))}ごろまで（2週間）です。</p>`
            : c.note ? `<p class="sub">${esc(c.note)}</p>` : ""}
        </div>`;
    case "cook":
      return c.cook_recorded
        ? `<div class="calm-card">${title("使った食材を\n記録しました")}<p class="sub">今日はもう何もしなくて大丈夫です。</p></div>`
        : `<div class="calm-card">${title("作ったら、\n使った食材を記録")}<p class="sub">押して選ぶだけ。あとでも大丈夫です。</p>
             <button class="primary" id="home-record">使った食材を記録する</button></div>`;
    default:
      return `<div class="calm-card">${title("今日は、急いで使う\n食材はありません")}<p class="sub">何もしなくて大丈夫です。</p></div>`;
  }
}

async function renderHome(seq) {
  await loadHome();
  if (seq !== renderSeq) return;
  const h = home;
  const noDishes = ["skip_pending", "done"].includes(h.today_state);
  app.innerHTML = `
    <div class="screen">
      <div class="home-top">
        <div class="appname" id="appname">冷蔵コーチ</div>
        <button class="notify-chip ${h.bell.quiet || !h.bell.notify_enabled ? "off" : ""}" id="go-settings">${h.bell.quiet || !h.bell.notify_enabled ? ICON.bellOff : ICON.bell}<span>${esc(h.bell.label)}</span></button>
      </div>
      <p class="label">${esc(fmtDayLong(h.now))}</p>
      ${h.yesterday_cook ? `<div class="calm-card">${title("昨日、使った食材は？")}<p class="sub">押して選ぶだけ。分からなければ飛ばしてOK。</p>
        <div class="row two"><button class="primary" id="y-pick">選ぶ</button><button id="y-skip">飛ばす</button></div></div>` : ""}
      ${homeCard(h)}
      <div class="section-head"><h2>期限が近い食材</h2><button class="link" id="see-all">すべて見る</button></div>
      <div class="list">${h.near.length ? h.near.map(n => `
          <div class="item"><div class="body"><div class="name">${esc(n.name)}</div></div>
            <span class="when ${n.soon ? "soon" : ""}">${esc(n.when)}</span></div>`).join("")
        : `<p class="sub" style="padding:12px 0">期限の目安がある食材は、まだありません。</p>`}</div>
      ${noDishes ? `<h2>この食材で作れる料理</h2><p class="sub">今日は料理の提案はお休みです。</p>` : dishesHtml(h.dishes, "この食材で作れる料理")}
      <div class="tiles">
        <button class="tile-sm" id="go-fridge">${ICON.fridge}<span><b>冷蔵庫の中身</b><small>期限の目安が近い順</small></span></button>
        <button class="tile-sm" id="go-receipt">${ICON.camera}<span><b>レシートを撮る</b><small>買い物のあとに1回だけ</small></span></button>
      </div>
      ${weeklyHtml(h.weekly)}
      <p class="note">期限は購入日からの目安です。調理時間は「約」の目安、料理のリンク先はクックパッドの検索結果です。</p>
    </div>
    ${devOpen ? `<div class="dev-sheet" id="dev"></div>` : ""}`;
  const bind = (id, fn) => { const el = document.getElementById(id); if (el) el.onclick = fn; };
  bind("home-cook", () => answer("cook", "home"));
  bind("home-skip", () => answer("skip", "home"));
  bind("home-done", () => rescue("done_now", "home"));
  bind("home-used", () => rescue("already_used", "home"));
  bind("home-record", async () => openCook(false));
  bind("y-pick", () => openCook(true));
  bind("y-skip", async () => { await api("POST", "/api/cook-record/skip"); render(); });
  bind("see-all", () => show("fridge"));
  bind("go-settings", () => show("settings"));
  bind("go-fridge", () => show("fridge"));
  bind("go-receipt", () => show("receipt"));
  setupLongPress(document.getElementById("appname"));
  if (devOpen) renderDevMenu(seq);
}

async function answer(kind, from) {
  const res = await api("POST", "/api/answer", { answer: kind, taps: from === "home" ? 1 : taps(), via: from === "home" ? "home" : via("screen") });
  openedFrom = null;
  if (kind === "skip") { lastAction = res; show("action"); }
  else { cook = { items: res.items, selected: new Set(res.items.filter(i => i.checked).map(i => i.inventory_id)), yesterday: false }; show("cook"); }
}

async function rescue(result, from) {
  const res = await api("POST", "/api/rescue", { result, via: from });
  if (result === "done_now") { freezeResult = { answer: "done", ...res }; show("freeze"); }
  else show("home");
}

async function openCook(yesterday) {
  const { items } = await api("GET", "/api/cook-items");
  cook = { items, selected: new Set(yesterday ? [] : items.filter(i => i.checked).map(i => i.inventory_id)), yesterday };
  show("cook");
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
      devOpen = !devOpen;
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
    <div class="dev-inner">
      <div class="section-head"><h2>開発者メニュー</h2><button class="link muted" id="d-close">閉じる</button></div>
      <p class="sub">この端末のID：<code>${esc(DEVICE_ID)}</code></p>
      ${unlocked ? `
        <div class="dev-buttons">
          <button id="d-stats">記録を見る</button>
          <button id="d-ask">18時にする（通知の代わり）</button>
          <button id="d-reset-today">今日の返事をリセット</button>
          <button id="d-remind">確認を今すぐ出す</button>
          <button id="d-hour">1時間進める</button>
          <button id="d-morning">次の日の朝へ</button>
          <button id="d-demo-receipt">デモ用レシートを登録</button>
          <button id="d-reset">デモを初期化（10/26 19:00）</button>
        </div>
        <p class="sub">デモ用の時計：${esc(fmtDay(home.now))} ${esc(fmtTime(home.now))}</p>
        ${devMsg ? `<p class="ok-msg">${esc(devMsg)}</p>` : ""}`
      : `<div class="field"><input type="password" id="d-pw" placeholder="パスワード（.env の DEBUG_PASSWORD）" autocomplete="off">
          <button id="d-pw-save" style="width:auto;margin:0;min-height:44px">保存</button></div>`}
      <p class="error" id="d-err" hidden></p>
    </div>`;
  devMsg = null;
  const err = m => { const p = document.getElementById("d-err"); p.hidden = false; p.textContent = m; };
  const run = (fn, done) => async () => {
    try { const r = await fn(); devMsg = done ? (typeof done === "function" ? done(r) : done) : null; render(); }
    catch (e) { err(e.message); }
  };
  const day = home.now.slice(0, 10);
  const next = new Date(Date.UTC(...ymd(day).map((v, i) => i === 1 ? v - 1 : v)) + 864e5).toISOString().slice(0, 10);
  const bind = (id, fn) => { const b = document.getElementById(id); if (b) b.onclick = fn; };
  bind("d-close", () => { devOpen = false; render(); });
  bind("d-pw-save", async () => {
    store("reizo.debug_password", document.getElementById("d-pw").value);
    try { await api("GET", "/api/debug/ping"); render(); } catch (e) { store("reizo.debug_password", null); err(e.message); }
  });
  bind("d-stats", () => { devOpen = false; show("stats"); });
  bind("d-ask", run(async () => {
    if (fmtTime(home.now) < "18:00") await api("POST", "/api/debug/advance", { to: `${day}T18:00:00+09:00` });
    return api("POST", "/api/debug/trigger-ask");
  }, "問いかけを出しました"));
  bind("d-reset-today", run(() => api("POST", "/api/debug/reset-today"), r => r.message));
  bind("d-remind", run(() => api("POST", "/api/debug/trigger-remind"), "確認を出しました"));
  bind("d-hour", run(() => api("POST", "/api/debug/advance", { minutes: 60 })));
  bind("d-morning", run(() => api("POST", "/api/debug/advance", { to: `${next}T09:00:00+09:00` })));
  bind("d-demo-receipt", async () => { devOpen = false; sentImages = []; startConfirm(await api("POST", "/api/receipts", { use_fixture: true })); });
  bind("d-reset", run(() => api("POST", "/api/debug/reset", { scope: "demo" }), "初期化しました（10/26 19:00）"));
}

// ---------------- 試用の記録（開発者メニュー） ----------------

async function renderStats(seq) {
  const s = await api("GET", "/api/debug/stats");
  if (seq !== renderSeq) return;
  const KIND = { ask: "問いかけ", remind: "確認", rescue: "救済行動" };
  const VIA = { notification: "通知のボタン", screen: "2択画面", home: "ホームのカード" };
  const VAL = { skip: "今日は無理", cook: "作る", done: "入れた", later: "あとで", gone: "残っていなかった",
                done_now: "入れた（今やった）", already_used: "もう使った" };
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
        <tr><th>端末</th><th>日時</th><th>種類</th><th>通知→回答</th><th>タップ</th><th>どこから</th><th>結果</th></tr>
        ${s.rows.map(r => `<tr><td>${esc(r.device)}</td><td>${esc(r.answered_at ? `${fmtShort(r.answered_at)} ${fmtTime(r.answered_at)}` : "")}</td><td>${esc(KIND[r.kind] || r.kind)}</td>
          <td>${r.seconds == null ? "—" : `${Math.round(r.seconds)}秒`}</td><td>${r.taps ?? ""}</td><td>${esc(VIA[r.via] || r.via || "")}</td>
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
  const [inv, sug] = await Promise.all([api("GET", "/api/inventory"), api("GET", "/api/suggestions?limit=3")]);
  if (seq !== renderSeq) return;
  const list = fridgeTab === "fridge" ? inv.fridge : inv.freezer;
  const row = i => {
    const sub = fridgeTab === "fridge"
      ? `${esc(fmtShort(i.purchased_at))} に購入${i.past ? "・目安を過ぎています。状態を確かめてください" : ""}`
      : `冷凍して${i.frozen_days + 1}日目・目安${esc(fmtShort(i.until))}ごろまで`;
    const picked = removing && removing.has(i.inventory_id);
    return removing
      ? `<div class="check-row ${picked ? "on" : ""}" data-id="${esc(i.inventory_id)}" role="checkbox" aria-checked="${picked}" tabindex="0">
           <span class="box">${picked ? ICON.check : ""}</span><div class="body"><div class="name">${esc(i.name)}</div><div class="raw">${sub}</div></div>${soonBadge(i.soon)}</div>`
      : `<div class="item"><div class="body"><div class="name">${esc(i.name)}</div><div class="raw">${sub}</div></div>${soonBadge(i.soon)}</div>`;
  };
  app.innerHTML = `
    <div class="screen">
      <p class="label">${esc(fmtDayLong(home.now))}</p>
      ${title("冷蔵庫の中身")}
      <div class="tabs" role="tablist">
        <button role="tab" class="${fridgeTab === "fridge" ? "on" : ""}" id="tab-fridge" aria-selected="${fridgeTab === "fridge"}">冷蔵（${inv.fridge.length}）</button>
        <button role="tab" class="${fridgeTab === "freezer" ? "on" : ""}" id="tab-freezer" aria-selected="${fridgeTab === "freezer"}">冷凍（${inv.freezer.length}）</button>
      </div>
      <div class="list ${removing ? "checks" : ""}">${list.length ? list.map(row).join("")
        : `<p class="sub" style="padding:12px 0">${fridgeTab === "fridge" ? "冷蔵に登録された食材はありません。" : "冷凍した食材はありません。"}</p>`}</div>
      ${fridgeTab === "freezer" ? `<p class="note" style="text-align:left">${esc(inv.freezer_note)}（出典：${esc(inv.freezer_source)}）</p>` : ""}
      ${removing
        ? `<div class="row"><button id="rm-used" ${removing.size ? "" : "disabled"}>使った</button><button id="rm-gone" ${removing.size ? "" : "disabled"}>なくなった</button></div>
           <button class="link muted" id="rm-cancel">やめる</button>`
        : list.length ? `<button id="rm-start">使った・なくなった食材を消す</button>` : ""}
      ${dishesHtml(sug.dishes, "作れそうな料理（期限が近い順・時間は目安）")}
      <div class="actions"><button class="dark" id="back">ホームへ</button></div>
    </div>`;
  const bind = (id, fn) => { const b = document.getElementById(id); if (b) b.onclick = fn; };
  bind("tab-fridge", () => { fridgeTab = "fridge"; removing = null; render(); });
  bind("tab-freezer", () => { fridgeTab = "freezer"; removing = null; render(); });
  bind("rm-start", () => { removing = new Set(); render(); });
  bind("rm-cancel", () => { removing = null; render(); });
  const remove = reason => async () => {
    await api("POST", "/api/inventory/remove", { inventory_ids: [...removing], reason });
    removing = null;
    render();
  };
  bind("rm-used", remove("used"));
  bind("rm-gone", remove("gone"));
  app.querySelectorAll(".check-row[data-id]").forEach(el => el.onclick = () => {
    const id = el.dataset.id;
    removing.has(id) ? removing.delete(id) : removing.add(id);
    render();
  });
  bind("back", () => { removing = null; show("home"); });
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

function addHour(hhmm) {
  const [h, m] = hhmm.split(":").map(Number);
  return h + 1 >= 24 ? null : `${String(h + 1).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
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
    permission = `<p>この端末で通知を受け取ります。</p><div class="row"><button id="push-test">テスト通知</button><button id="push-off">許可を外す</button></div>`;
  else permission = `<button class="primary" id="push-on">通知を許可する</button>`;

  app.innerHTML = `
    <div class="screen">
      <p class="label">設定</p>
      ${title("通知の時間")}
      <p class="sub">通知は1日に最大3回です。早めに使いたい食材がある日だけ聞きます。</p>
      <div class="card">
        <label class="time-row switch-row"><span><b>通知</b><small>オフにすると、開いたときにホームで聞きます</small></span>
          <input type="checkbox" role="switch" id="notify" ${s.notify ? "checked" : ""}></label>
        <label class="time-row"><span><b>問いかけ</b><small>「今日、作れそう？」</small></span>
          <input type="time" step="60" id="ask-time" value="${esc(s.ask_time)}"></label>
        <label class="time-row"><span><b>返事の締め切り</b><small>過ぎたら「今日は無理」として受け取ります</small></span>
          <input type="time" step="60" id="deadline-time" value="${esc(s.deadline_time)}"></label>
        <label class="time-row"><span><b>できたかの確認</b><small>「あとでやる」を選んだ日だけ</small></span>
          <input type="time" step="60" id="remind-time" value="${esc(s.remind_time)}"></label>
        <div class="time-row"><span><b>もう一度だけ</b><small>確認の1時間後（固定）</small></span><span class="fixed" id="reask">${esc(addHour(s.remind_time) || "送りません")}</span></div>
        <p class="sub">確認の1時間後が0:00を過ぎる場合は、もう一度の通知は送りません。</p>
        <p class="error" id="warn" hidden>問いかけ → 返事の締め切り → できたかの確認 の順になるようにしてください。</p>
      </div>
      <h2>この端末の通知</h2>
      <div class="card">${permission}<p class="error" id="push-err" hidden></p></div>
      <div class="actions">
        <button class="primary" id="save">保存する</button>
        <button class="link muted" id="back">もどる</button>
      </div>
    </div>`;
  const ask = document.getElementById("ask-time"), dl = document.getElementById("deadline-time"), rm = document.getElementById("remind-time");
  const check = () => {
    const bad = !ask.value || !dl.value || !rm.value || !(ask.value < dl.value && dl.value < rm.value);
    document.getElementById("warn").hidden = !bad;
    document.getElementById("save").disabled = bad;
    document.getElementById("reask").textContent = rm.value ? (addHour(rm.value) || "送りません") : "";
  };
  [ask, dl, rm].forEach(i => i.oninput = check);
  check();
  document.getElementById("save").onclick = async () => {
    try {
      await api("PUT", "/api/settings", { notify: document.getElementById("notify").checked, ask_time: ask.value, deadline_time: dl.value, remind_time: rm.value });
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
        </div>
      </div>
    </div>`;
  const file = document.getElementById("file");
  const pick = document.getElementById("pick");
  document.getElementById("shoot").onclick = () => file.click();
  document.getElementById("pick-btn").onclick = () => pick.click();
  document.getElementById("later").onclick = () => show("home");
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
    startConfirm(await api("POST", "/api/receipts", { ocr_text: text }), reasons);
    return;
  } catch (e) { reasons.push(`端末の文字読み取り：${e.message}`); }
  if (cancelled) return;

  startConfirm(await api("POST", "/api/receipts", { use_fixture: true }), reasons);  // 3. デモ用の固定データ
}

// ---------------- 画面2 読み取り結果の確認 ----------------

const ENGINE_LABEL = { llm: "AIで読み取りました", tesseract: "予備の方法（端末の中の文字読み取り）で読み取りました", fixture: "デモ用の固定データです" };

function startConfirm(result, reasons = []) {
  draft = {
    purchased_at: result.purchased_at,
    engine: result.engine, model: result.model, transcript: result.transcript, reasons,
    matched: result.matched.map(m => ({ ...m, checked: true })),
    unmatched: result.unmatched.map(u => ({ ...u, choice: null })),  // choice：food_id か "not_food"
    not_food: result.not_food || [],
    manual: [],      // 手入力で足した食材 {food_id, name, soon}
    query: "", results: [],
  };
  show("confirm");
}

function confirmCount() {
  return draft.matched.filter(m => m.checked).length
    + draft.unmatched.filter(u => u.choice && u.choice !== "not_food").length
    + draft.manual.length;
}

async function refreshSoon() {
  const ids = [...draft.matched.map(m => m.food_id), ...draft.manual.map(m => m.food_id)];
  const { items } = await api("POST", "/api/inventory/preview", { purchased_at: draft.purchased_at, food_ids: ids });
  [...draft.matched, ...draft.manual].forEach((m, i) => { m.soon = items[i].soon; });
  render();
}

function renderConfirm() {
  const n = confirmCount();
  app.innerHTML = `
    <div class="screen">
      <label class="label date-pick">${esc(fmtDay(draft.purchased_at))}のレシート（購入日を変える）
        <input type="date" id="pdate" value="${esc(draft.purchased_at)}"></label>
      ${title("登録する食材にチェック")}
      <div class="list checks">${draft.matched.length ? draft.matched.map((m, i) => `
          <div class="check-row ${m.checked ? "on" : ""}" data-i="${i}" role="checkbox" aria-checked="${m.checked}" tabindex="0">
            <span class="box">${m.checked ? ICON.check : ""}</span>
            <div class="body"><div class="name">${esc(m.name)}</div><div class="raw">レシート表記：${esc(m.raw)}</div></div>
            ${soonBadge(m.soon && m.checked)}
          </div>`).join("")
        : `<p class="sub" style="padding:12px 0">認識できた食材はありません。下から選ぶか、追加してください。</p>`}</div>

      ${draft.unmatched.length ? `
        <h2>認識できなかった行</h2>
        <p class="sub">食材だけ選んでください。食材でなければ、そのままでOK。</p>
        <div class="list">${draft.unmatched.map((u, i) => `
          <div class="unmatched">
            <div class="raw-line">${esc(u.raw)}</div>
            <div class="chips-row">
              ${u.candidates.map(c => `<button class="cand ${u.choice === c.id ? "on" : ""}" data-u="${i}" data-c="${esc(c.id)}">${esc(c.name)}</button>`).join("")}
              <button class="cand ghost ${u.choice === "not_food" ? "on" : ""}" data-u="${i}" data-c="not_food">食材ではない</button>
            </div>
          </div>`).join("")}</div>` : ""}

      ${draft.not_food.length ? `<p class="sub">前に「食材ではない」を選んだ行：${draft.not_food.map(x => esc(x.raw)).join("、")}</p>` : ""}

      ${draft.manual.length ? `<h2>追加した食材</h2><div class="list">${draft.manual.map((m, i) => `
          <div class="item"><div class="body"><div class="name">${esc(m.name)}</div></div>${soonBadge(m.soon)}
            <button class="use" data-rm="${i}">外す</button></div>`).join("")}</div>` : ""}

      <h2>読み取れなかった食材を追加</h2>
      <div class="search">
        <input type="search" id="q" placeholder="例：ぶたこま、とうふ、キャベツ" value="${esc(draft.query)}" autocomplete="off">
        ${draft.results.length ? `<div class="results">${draft.results.map(f => `<button class="result" data-f="${esc(f.id)}" data-n="${esc(f.name)}">${esc(f.name)}</button>`).join("")}</div>` : ""}
      </div>

      <p class="sub">目安の出典：FoodSafety.gov・目黒区・農林水産省（<a href="#" id="src">目安と出典</a>）。期限は推定です。</p>
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

  app.querySelectorAll(".check-row[data-i]").forEach(el => {
    const toggle = () => { const m = draft.matched[Number(el.dataset.i)]; m.checked = !m.checked; render(); };
    el.onclick = toggle;
    el.onkeydown = e => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); toggle(); } };
  });
  app.querySelectorAll(".cand").forEach(b => b.onclick = () => {
    const u = draft.unmatched[Number(b.dataset.u)];
    u.choice = u.choice === b.dataset.c ? null : b.dataset.c;
    render();
  });
  app.querySelectorAll("[data-rm]").forEach(b => b.onclick = () => { draft.manual.splice(Number(b.dataset.rm), 1); render(); });
  const q = document.getElementById("q");
  let timer = null;
  q.oninput = () => {
    draft.query = q.value;
    clearTimeout(timer);
    timer = setTimeout(async () => {
      draft.results = q.value.trim() ? (await api("GET", `/api/foods/search?q=${encodeURIComponent(q.value)}`)).foods : [];
      const pos = q.selectionStart;
      render();
      const nq = document.getElementById("q");
      nq.focus(); nq.setSelectionRange(pos, pos);
    }, 200);
  };
  app.querySelectorAll(".result").forEach(b => b.onclick = () => {
    draft.manual.push({ food_id: b.dataset.f, name: b.dataset.n, soon: false });
    draft.query = ""; draft.results = [];
    refreshSoon();
  });
  document.getElementById("pdate").onchange = e => { if (e.target.value) { draft.purchased_at = e.target.value; refreshSoon(); } };
  document.getElementById("src").onclick = e => { e.preventDefault(); show("sources"); };
  document.getElementById("cancel").onclick = () => { draft = null; show("home"); };
  document.getElementById("ok").onclick = async () => {
    const items = [
      ...draft.matched.filter(m => m.checked).map(m => ({ line_id: m.line_id, food_id: m.food_id, raw: m.raw, jan: m.jan })),
      // 自分で選んだ行は、次から自動で当たるように覚える（「食材ではない」も）
      ...draft.unmatched.filter(u => u.choice).map(u => u.choice === "not_food"
        ? { line_id: u.line_id, food_id: null, not_food: true, raw: u.raw, jan: u.jan, learn: true }
        : { line_id: u.line_id, food_id: u.choice, raw: u.raw, jan: u.jan, learn: true }),
      ...draft.manual.map(m => ({ food_id: m.food_id })),
    ];
    await api("POST", "/api/inventory/confirm", { purchased_at: draft.purchased_at, items });
    draft = null;
    sentImages = [];
    show("home");
  };
}

// ---------------- 画面3b 今日、作れそう？（通知をタップしたとき） ----------------

function renderAsk() {
  app.innerHTML = `
    <div class="screen">
      <div class="ask-items"><span class="tag soon strong">早めに</span><b>${esc(names(home.card.items))}</b></div>
      ${title("今日、作れそう？")}
      <p class="sub">押すだけでOK。何をするかは、アプリが考えます。</p>
      <div class="big-choice">
        <button class="choice" id="cook">作る</button>
        <button class="choice" id="skip">今日は無理</button>
      </div>
      <p class="note">${esc(home.card.deadline)}までに返事がなければ、「今日は無理」として受け取ります。</p>
      <div class="actions"><button class="link muted" id="back">ホームへ</button></div>
    </div>`;
  document.getElementById("cook").onclick = () => answer("cook", "screen");
  document.getElementById("skip").onclick = () => answer("skip", "screen");
  document.getElementById("back").onclick = () => { openedFrom = null; show("home"); };
}

// ---------------- 画面4 救済行動（「今日は無理」） ----------------

function renderAction() {
  const a = lastAction && lastAction.action;
  const note = lastAction && lastAction.not_freezable_note;
  if (!a) {
    app.innerHTML = `
      <div class="screen">
        ${title("おつかれさま。\n今日はもう大丈夫。")}
        ${note ? `<p class="sub">${esc(note)}</p>` : `<p class="sub">いま急いでやることはありません。</p>`}
        <div class="actions"><button class="dark" id="ok">ホームへ</button></div>
      </div>`;
    document.getElementById("ok").onclick = () => show("home");
    return;
  }
  app.innerHTML = `
    <div class="screen">
      ${title("おつかれさま。\n今日はこれだけ。")}
      ${rescueCard(a, false)}
      ${note ? `<p class="sub">${esc(note)}</p>` : ""}
      <div class="actions">
        <button class="primary" id="done">入れた（今やった）</button>
        <button id="later">あとでやる</button>
        <button class="link" id="used">${esc(a.name)}はもう使った</button>
        ${lastAction.remind_at ? `<p class="note">「あとでやる」なら、${esc(fmtTime(lastAction.remind_at))}に1回だけ聞きます。</p>` : ""}
      </div>
    </div>`;
  document.getElementById("done").onclick = () => rescue("done_now", "screen");
  document.getElementById("later").onclick = () => rescue("later", "screen");
  document.getElementById("used").onclick = () => rescue("already_used", "screen");
}

// ---------------- 画面5b〜5d 冷凍庫に入れた？ ----------------

function renderFreeze() {
  if (freezeResult) return renderFreezeResult();
  const a = home.card.action;
  app.innerHTML = `
    <div class="screen">
      <p class="label">${esc(fmtTime(home.now))}</p>
      ${title(`${a.name}、\n冷凍庫に入れた？`)}
      <p class="sub">まだなら、${esc(a.how_short)}だけ。${esc(fmtSeconds(a.seconds))}です。</p>
      <div class="big-choice">
        <button class="primary" id="done">入れた</button>
        <button id="later">あとで</button>
      </div>
      <button class="link" id="gone">もう残っていなかった</button>
    </div>`;
  const send = async answer => {
    const res = await api("POST", "/api/freeze-check", { answer, taps: taps(), via: via("screen") });
    openedFrom = null;
    freezeResult = { answer, name: a.name, ...res };
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
      <p class="sub">冷凍の目安：2週間（${esc(fmtShort(r.until))}ごろまで）。出典：農林水産省 消費者の部屋</p>
      ${weeklyHtml(r.weekly)}`;
  } else if (r.answer === "later") {  // 5c
    body = r.reask_at
      ? `${title("了解。")}<p class="lead">${esc(fmtTime(r.reask_at))}に、もう一度だけ聞きます。</p>
         <p class="sub">それで今日はおしまい。返事がなくても大丈夫です。</p>`
      : `${title("了解。")}<p class="lead">今日はこれでおしまい。</p>`;
  } else {
    body = `${title("教えてくれて\nありがとう。")}<p class="sub">${esc(r.name)}を在庫から消しました。</p>`;
  }
  app.innerHTML = `<div class="screen">${body}<div class="actions"><button class="dark" id="home">ホームへ</button></div></div>`;
  document.getElementById("home").onclick = () => { freezeResult = null; show("home"); };
}

// ---------------- 画面6 使った食材を選ぶ（「作る」） ----------------

async function renderCook(seq) {
  const sug = await api("GET", "/api/suggestions?limit=3");
  if (seq !== renderSeq) return;
  const n = cook.selected.size;
  app.innerHTML = `
    <div class="screen">
      <p class="label">${cook.yesterday ? "昨日の記録" : "「作る」を受け取りました"}</p>
      ${title(cook.yesterday ? "昨日、使った食材は？" : "使った食材を押してください")}
      <p class="sub">使い切っていなくても大丈夫。少しでも使ったら押します。</p>
      <div class="chips">
        ${cook.items.length ? cook.items.map(i => `
          <button class="chip ${cook.selected.has(i.inventory_id) ? "selected" : ""}" data-id="${esc(i.inventory_id)}" aria-pressed="${cook.selected.has(i.inventory_id)}">
            <span>${esc(i.name)}</span><small>${i.frozen ? "冷凍" : esc(fmtShort(i.purchased_at))}${i.soon ? "・早めに" : ""}</small>
          </button>`).join("") : `<p class="sub">登録された食材はありません。</p>`}
      </div>
      ${dishesHtml(sug.dishes, "何を作るか迷ったら（期限が近い順・時間は目安）")}
      <div class="actions">
        <button class="primary" id="save" ${n ? "" : "disabled"}>${n}品を記録する</button>
        ${cook.yesterday ? `<button class="link muted" id="skip">飛ばす</button>` : `<button id="later">あとで記録する</button>`}
      </div>
    </div>`;
  app.querySelectorAll(".chip").forEach(b => b.onclick = () => {
    const id = b.dataset.id;
    cook.selected.has(id) ? cook.selected.delete(id) : cook.selected.add(id);
    render();
  });
  document.getElementById("save").onclick = async () => {
    await api("POST", "/api/cook-record", { inventory_ids: [...cook.selected], yesterday: cook.yesterday });
    cook = null;
    show("home");
  };
  const later = document.getElementById("later");
  if (later) later.onclick = async () => { await api("POST", "/api/cook-record/later"); cook = null; show("home"); };
  const skip = document.getElementById("skip");
  if (skip) skip.onclick = async () => { await api("POST", "/api/cook-record/skip"); cook = null; show("home"); };
}

// ---------------- 目安と出典 ----------------

async function renderSources(seq) {
  const s = await api("GET", "/api/sources");
  if (seq !== renderSeq) return;
  app.innerHTML = `
    <div class="screen">
      <p class="label">食材表</p>
      ${title("目安と出典")}
      <p class="sub">冷蔵の目安は、分類ごとに出典の範囲で一番短い値を取っています。冷凍の目安は全品${s.freezer_days}日（農林水産省「2〜3週間以内に使い切ってください」の短い方）です。</p>
      <p class="sub">豆腐・卵・乳製品・きのこ・果物・パンなどは、パッケージの期限を優先します。</p>
      ${s.categories.map(c => `
        <div class="src-item">
          <div class="top"><b>${esc(c.label)}</b><span class="tag ${c.fridge_days && c.fridge_days <= 2 ? "soon" : "ok"}">${
            !c.track_expiry ? "期限を判定しない" : c.fridge_days ? `冷蔵 ${c.fridge_days}日` : "パッケージの期限"}${c.freezable ? "・冷凍に向く" : ""}</span></div>
          <p>例：${c.examples.map(esc).join("、")}</p>
          ${c.source ? `<p>出典：${esc(c.source)}</p>` : ""}
        </div>`).join("")}
      <h2>出典一覧</h2>
      <ul class="src-list">
        ${Object.values(s.sources).map(r => `<li><a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.name)}</a><br><span class="sub">${esc(r.used_for)}</span></li>`).join("")}
        <li>独自判断：薄切り肉（2日）、もやし・カット野菜（2日）</li>
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
  if (kind === "ask" && home.today_state === "unanswered") { openedFrom = { kind }; return show("ask"); }
  if (kind === "remind" && home.today_state === "skip_pending") { openedFrom = { kind }; return show("freeze"); }
  if (kind === "action" && home.card.action && home.today_state === "skip_pending") {
    lastAction = { action: home.card.action, not_freezable_note: home.card.note, remind_at: home.card.remind_time && `${home.now.slice(0, 10)}T${home.card.remind_time}:00` };
    return show("action");
  }
  if (kind === "cook") return openCook(false);
  show("home");
}

if ("serviceWorker" in navigator && window.isSecureContext) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
  // アプリが開いているときに通知がタップされた
  navigator.serviceWorker.addEventListener("message", e => { if (e.data && e.data.type === "open") openFromNotification(e.data.kind); });
}
document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });

(async function init() {
  config = await api("GET", "/api/config");
  const open = new URLSearchParams(location.search).get("open");
  if (open) history.replaceState(null, "", "/");
  await (open ? openFromNotification(open) : render());
  setInterval(poll, 5000);
})();
