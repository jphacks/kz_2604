// 画面。判定ロジックは持たない。表示する内容はすべてAPIから受け取る。
"use strict";

const app = document.getElementById("app");
const clockEl = document.getElementById("clock");

let view = "home";
let serverState = { now: null, screen: null };
let config = { demo: false };
let ingredients = [];
let draft = null;          // 画面2の編集中データ
let sentImages = [];       // APIに送った直前の画像（画面2で確認できる）
let editIdx = null;        // 画面2で修正中の行
let lastAction = null;     // 画面4の内容
let freezeResult = null;   // 画面5の結果
let cookItems = [];
let cookSelected = new Set();
let cookFromAsk = false;   // 画面6を「作る」から開いたか
let renderSeq = 0;         // 古い非同期描画が新しい画面を上書きしないように

// ---------------- 共通 ----------------

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
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
function fmtNow(iso) { return `${fmtDay(iso)} ${fmtTime(iso)}`; }
function fmtSeconds(s) {
  if (!s) return "";
  return s < 60 ? `${s}秒` : `${Math.round(s / 60)}分`;
}
function stateTag(state, label) {
  return `<span class="tag ${esc(state)}">${esc(label)}</span>`;
}
function tag(item) { return stateTag(item.freshness, item.freshness_label); }
function joinNames(names) { return names.join("と"); }
// 「おつかれさま。\n今日はこれだけ。」のように改行で区切った見出し
function title(text) { return `<h1>${text.split("\n").map(l => `<span class="br">${esc(l)}</span>`).join("")}</h1>`; }

const ICON = {
  camera: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 8h3l2-3h6l2 3h3v11H4z"/><circle cx="12" cy="13" r="3.5"/></svg>`,
  fridge: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="2.5" width="14" height="19" rx="2.5"/><path d="M5 10h14M9 6v2M9 13v3"/></svg>`,
  snow: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M12 2v20M3.3 7l17.4 10M3.3 17L20.7 7"/><path d="M9 4l3 2 3-2M9 20l3-2 3 2M4 10.5l3.4-.3L6 7M20 13.5l-3.4.3L18 17M4 13.5l3.4.3L6 17M20 10.5l-3.4-.3L18 7"/></svg>`,
  pan: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="10" cy="13" r="6.5"/><path d="M16.5 13H22M8 3.5c0 1 1 1.5 1 2.5M11.5 3.5c0 1 1 1.5 1 2.5"/></svg>`,
  sun: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>`,
  out: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/></svg>`,
};

function show(v) {
  view = v;
  render();
  window.scrollTo(0, 0);
}

async function syncState(follow) {
  try {
    serverState = await api("GET", "/api/state");
  } catch (e) { return; }
  clockEl.textContent = fmtNow(serverState.now);
  // ホームにいるとき（または強制時）だけ、サーバー側の問いかけに切り替える
  if ((follow || view === "home") && serverState.screen) {
    const map = { ask: "ask", action: "action", freeze_check: "freeze" };
    if (serverState.screen === "action") lastAction = { action: serverState.ask.action, next_tip: serverState.ask.next_tip, remind_at: serverState.ask.remind_at };
    if (view !== map[serverState.screen]) show(map[serverState.screen]);
  } else if (follow && !serverState.screen && ["ask", "action", "freeze"].includes(view)) {
    show("home");
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

// ---------------- 画面0 ホーム ----------------

async function renderHome(seq) {
  const [today, week] = await Promise.all([api("GET", "/api/today"), api("GET", "/api/weekly")]);
  if (seq !== renderSeq) return;
  const weekCard = week.wasted_count === 0
    ? `<div class="wk"><span class="k">今週</span><span>捨てたもの <b>0</b></span><span class="sep">／</span><span>守れた <b>${week.protected_days}</b>回</span></div>`
    : `<div class="wk"><span class="k">今週</span><span>守れたもの <b>${week.saved_count}</b>品</span><span class="sep">／</span><span class="quiet">間に合わなかった ${week.wasted_count}品</span></div>`;
  app.innerHTML = `
    <div class="screen">
      <p class="label">${esc(fmtDayLong(serverState.now))}</p>
      ${today.should_ask ? title(`${joinNames(today.reason_items)}を、\n早めに使いたい日です`) : title("今日は、\n急ぐ食材はありません")}
      <p class="sub">${today.should_ask ? "18時に「今日、作れそう？」と聞きます。" : "急ぐ食材がない日は、何も聞きません。"}</p>
      ${serverState.screen ? `<button class="primary" id="resume">問いかけに答える</button>` : ""}
      <div class="home-buttons">
        <button class="tile" id="go-fridge">${ICON.fridge}<span>冷蔵庫の中身を見る</span></button>
        <button class="tile" id="go-receipt">${ICON.camera}<span>レシートを撮る</span></button>
      </div>
      ${weekCard}
      ${config.demo ? `<button class="demo" id="demo18">18時にする（デモ用）</button>` : ""}
      <div class="card" id="notify"><p class="sub">通知の設定を確認しています…</p></div>
      <div class="actions"><button class="link muted" id="go-sources">目安と出典</button></div>
    </div>`;
  setupNotifyCard(seq);
  document.getElementById("go-receipt").onclick = () => show("receipt");
  document.getElementById("go-fridge").onclick = () => show("fridge");
  document.getElementById("go-sources").onclick = () => show("sources");
  const d18 = document.getElementById("demo18");
  if (d18) d18.onclick = async () => {
    const day = serverState.now.slice(0, 10);
    if (fmtTime(serverState.now) < "18:00") await api("POST", "/api/debug/advance", { to: `${day}T18:00:00+09:00` });
    else await api("POST", "/api/debug/trigger-ask");
    await syncState(true);
    if (!serverState.screen) render();  // 急ぐ食材がない日は問いかけが出ない
  };
  const r = document.getElementById("resume");
  if (r) r.onclick = () => syncState(true);
}

// ---------------- 画面0b 冷蔵庫の中身 ----------------

async function renderFridge(seq) {
  const [inv, sug] = await Promise.all([api("GET", "/api/inventory"), api("GET", "/api/suggestions")]);
  if (seq !== renderSeq) return;
  app.innerHTML = `
    <div class="screen">
      <p class="label">${esc(fmtDayLong(serverState.now))}</p>
      ${title("冷蔵庫の中身")}
      <p class="sub">期限の目安が近い順です。目安は購入日からの推定です。</p>
      <div class="list">
        ${inv.fridge.length ? inv.fridge.map(i => `
          <div class="item">
            <div class="body"><div class="name">${esc(i.name)}</div>
              <div class="raw">${esc(fmtShort(i.purchased_at))} に購入</div></div>
            ${stateTag(i.state, i.state_label)}
            <button class="use" data-id="${esc(i.inventory_id)}">使った</button>
          </div>`).join("")
          : `<p class="sub" style="padding:12px 0">冷蔵庫に登録された食材はありません。</p>`}
      </div>
      ${inv.freezer.length ? `
        <h2>冷凍庫</h2>
        <div class="list">
          ${inv.freezer.map(i => `
            <div class="item"><div class="body"><div class="name">${esc(i.name)}</div>
              <div class="raw">${esc(fmtShort(i.frozen_at))} に冷凍・${esc(fmtShort(i.use_by))} ごろまでに</div></div>
              <span class="tag ok">冷凍</span></div>`).join("")}
        </div>` : ""}
      ${dishesHtml(sug.dishes, "早めの食材で作れそうな料理（時間は目安）")}
      <div class="actions"><button class="dark" id="back">ホームへ</button></div>
    </div>`;
  app.querySelectorAll(".use").forEach(b => b.onclick = async () => {
    b.disabled = true;
    await api("POST", "/api/inventory/use", { inventory_ids: [b.dataset.id] });
    render();
  });
  document.getElementById("back").onclick = () => show("home");
}

// ---------------- 画面1 レシート撮影 ----------------
// 主：Gemini API（画像を送って文字起こし）→ 予備：端末の中の Tesseract.js → 最後：デモ用の固定データ。
// 食材への当てはめはどの経路でもサーバー側のコード（server/ocr_match.py）。

const TESSERACT_URL = "https://cdn.jsdelivr.net/npm/tesseract.js@5.1.1/dist/tesseract.min.js";
const TESS_LANG_PATH = "https://cdn.jsdelivr.net/npm/@tesseract.js-data/jpn/4.0.0_best_int";
const API_LONG_SIDE = 1568;   // AI に送る1枚の長辺
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
          ${config.demo ? `<button class="link muted" id="fixture">デモ用レシート</button>` : ""}
        </div>
      </div>
    </div>`;
  const file = document.getElementById("file");
  const pick = document.getElementById("pick");
  document.getElementById("shoot").onclick = () => file.click();
  document.getElementById("pick-btn").onclick = () => pick.click();
  document.getElementById("later").onclick = () => show("home");
  const fx = document.getElementById("fixture");
  if (fx) fx.onclick = async () => { sentImages = []; await finishReceipt(await api("POST", "/api/receipts", { use_fixture: true })); };
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

// レシート（明るい紙）の部分だけを切り出す。周りの机などの余白を落として文字を大きく保つ
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
  // 1. Gemini API（主）
  try {
    const result = await api("POST", "/api/receipts", {
      images: sentImages.map(u => ({ data: u.split(",")[1], media_type: "image/jpeg" })),
    });
    if (!cancelled) await finishReceipt(result);
    return;
  } catch (e) { reasons.push(`AIでの読み取り：${e.message}`); }
  if (cancelled) return;

  // 2. 端末の中の Tesseract.js（予備）
  try {
    msg.textContent = "予備の方法（端末の中の文字読み取り）で読んでいます…";
    bar.style.width = "0%";
    const text = await tesseractText(prepareForOcr(cropped), p => { bar.style.width = `${Math.round(p * 100)}%`; });
    if (cancelled) return;
    const result = await api("POST", "/api/receipts", { ocr_text: text });
    result.fallback_reasons = reasons;
    await finishReceipt(result);
    return;
  } catch (e) { reasons.push(`端末の文字読み取り：${e.message}`); }
  if (cancelled) return;

  // 3. デモ用の固定データ（両方失敗したとき）
  const result = await api("POST", "/api/receipts", { use_fixture: true });
  result.fallback_reasons = reasons;
  await finishReceipt(result);
}

async function finishReceipt(result) {
  draft = result;
  editIdx = null;
  show("confirm");
}

// ---------------- 画面2 読み取り結果の確認 ----------------

const ENGINE_LABEL = {
  gemini: "Gemini API で読み取りました",
  claude: "Claude API で読み取りました",
  tesseract: "予備の方法（端末の中の文字読み取り）で読み取りました",
  fixture: "デモ用の固定データです",
};

async function refreshDraft() {
  const items = draft.items.map(({ ingredient_id, raw, expiry_override }) => ({ ingredient_id, raw, expiry_override }));
  draft.items = (await api("POST", "/api/inventory/preview", { purchased_at: draft.purchased_at, items })).items;
  render();
}

function renderConfirm() {
  const options = sel => ingredients.map(i => `<option value="${esc(i.id)}" ${i.id === sel ? "selected" : ""}>${esc(i.name)}</option>`).join("");
  const n = draft.items.length;
  const reasons = draft.fallback_reasons || [];
  app.innerHTML = `
    <div class="screen">
      <p class="label">${esc(fmtDay(draft.purchased_at))}のレシート</p>
      ${title(n ? `${n}品を読み取りました` : "食材を追加してください")}
      <p class="sub">違っていたらタップして直せます。</p>
      <div class="list">
        ${n ? draft.items.map((it, idx) => `
          <div class="item tappable" data-idx="${idx}">
            <div class="body">
              <div class="name">${esc(it.name)}</div>
              <div class="raw">${it.raw ? `レシート表記：${esc(it.raw)}` : "手で追加"}${it.expiry_override ? `・期限 ${esc(fmtShort(it.expiry_override))}` : ""}</div>
              ${editIdx === idx ? `
                <div class="edit" onclick="event.stopPropagation()">
                  <select class="pick" data-idx="${idx}">${options(it.ingredient_id)}</select>
                  <label class="raw">期限（任意）<input type="date" class="expin" data-idx="${idx}" value="${esc(it.expiry_override || "")}"></label>
                  <button class="remove" data-idx="${idx}">外す</button>
                  <button class="done">閉じる</button>
                </div>
                <div class="raw">目安 ${esc(fmtShort(it.limit_date))} ごろまで（推定）</div>` : ""}
            </div>
            ${tag(it)}
          </div>`).join("") : `<p class="sub" style="padding:12px 0">下の「読み取れなかった食材を追加」から入れてください。</p>`}
      </div>
      <div class="field"><label class="sub" for="pdate">購入日</label><input type="date" id="pdate" value="${esc(draft.purchased_at)}"></div>
      ${draft.ignored.length ? `<h2>対象外（食材表にないもの）</h2><p class="sub">${draft.ignored.map(esc).join("、")}</p>` : ""}
      <p class="sub" style="margin-top:14px">「早めに」などの目安：USDA FoodKeeper・農林水産省ほか（<a href="#" id="src">目安と出典</a>）。期限は推定で、短めに取っています。</p>
      <details class="ocr">
        <summary>${esc(ENGINE_LABEL[draft.engine] || "")}</summary>
        ${reasons.length ? `<p>うまくいかなかった方法：${reasons.map(esc).join(" ／ ")}</p>` : ""}
        ${sentImages.length ? `<p>送った画像（${sentImages.length}枚）</p><div class="shots small">${sentImages.map(u => `<img src="${u}" alt="">`).join("")}</div>` : ""}
        ${draft.transcript ? `<p>読み取った文字</p><pre>${esc(draft.transcript)}</pre>` : ""}
      </details>
      <div class="actions">
        <button class="primary" id="ok" ${n ? "" : "disabled"}>これでOK</button>
        <div class="field" id="add-row" hidden><select id="addsel">${options()}</select><button id="add" style="width:auto;margin:0;min-height:40px">追加</button></div>
        <button class="link" id="add-open">読み取れなかった食材を追加</button>
      </div>
    </div>`;

  app.querySelectorAll(".item.tappable").forEach(el => el.onclick = () => {
    editIdx = editIdx === Number(el.dataset.idx) ? null : Number(el.dataset.idx);
    render();
  });
  app.querySelectorAll(".pick").forEach(s => s.onchange = () => { draft.items[s.dataset.idx].ingredient_id = s.value; refreshDraft(); });
  app.querySelectorAll(".expin").forEach(i => i.onchange = () => { draft.items[i.dataset.idx].expiry_override = i.value || null; refreshDraft(); });
  app.querySelectorAll(".remove").forEach(b => b.onclick = () => { draft.items.splice(Number(b.dataset.idx), 1); editIdx = null; render(); });
  app.querySelectorAll(".done").forEach(b => b.onclick = () => { editIdx = null; render(); });
  document.getElementById("pdate").onchange = e => { draft.purchased_at = e.target.value; refreshDraft(); };
  document.getElementById("src").onclick = e => { e.preventDefault(); show("sources"); };
  document.getElementById("add-open").onclick = () => {
    document.getElementById("add-row").hidden = false;
    document.getElementById("add-open").hidden = true;
  };
  document.getElementById("add").onclick = () => {
    draft.items.push({ ingredient_id: document.getElementById("addsel").value, raw: "", expiry_override: null });
    refreshDraft();
  };
  document.getElementById("ok").onclick = async () => {
    await api("POST", "/api/inventory/confirm", { purchased_at: draft.purchased_at, items: draft.items });
    draft = null;
    sentImages = [];
    show("home");
  };
}

// ---------------- 画面3 18時の問いかけ ----------------

async function renderAsk(seq) {
  const today = await api("GET", "/api/today");
  if (seq !== renderSeq) return;
  const ask = serverState.ask;
  const reason = today.reason_items.length ? `${joinNames(today.reason_items)}を、そろそろ使いたい日です。` : "";
  app.innerHTML = `
    <div class="screen night">
      <p class="date">${esc(fmtDayLong(ask.asked_at))}</p>
      <p class="time">${esc(fmtTime(ask.asked_at))}</p>
      <div class="notif">
        <div class="head"><img src="/icons/icon-192.png" alt=""><span class="app">今日は無理</span><span>いま</span></div>
        <p class="title">今日、作れそう？</p>
        <p class="body">${esc(reason)}ボタンを押すだけでOK。</p>
        <div class="row">
          <button class="cook" id="cook">作る</button>
          <button class="primary" id="skip">無理</button>
        </div>
      </div>
      <p class="note">早めに使いたい食材がある日だけ聞きます。<br>${esc(fmtTime(ask.deadline))}までに返事がなければ「無理」として受け取ります。</p>
    </div>`;
  document.getElementById("skip").onclick = async () => {
    lastAction = await api("POST", "/api/answer", { date: ask.date, answer: "skip" });
    await syncState(false);
    show("action");
  };
  document.getElementById("cook").onclick = async () => {
    cookItems = (await api("POST", "/api/answer", { date: ask.date, answer: "cook" })).items;
    cookSelected = new Set();
    cookFromAsk = true;
    await syncState(false);
    show("cook");
  };
}

// ---------------- 画面4 「無理」→ 救済行動 ----------------

function renderAction() {
  const ask = serverState.ask;
  const a = lastAction && lastAction.action;
  const tip = lastAction && lastAction.next_tip
    ? `<div class="tip">${ICON.sun}<div><div class="k">明日の朝の一手</div><div>${esc(lastAction.next_tip)}</div></div></div>` : "";
  const label = ask && ask.answered_by === "timeout" ? "返事がなかったので「無理」として受け取りました" : "「無理」を受け取りました";
  const remind = lastAction && lastAction.remind_at
    ? `<p class="note">${esc(fmtTime(lastAction.remind_at))}ごろに「冷凍した？」と通知で聞きます。</p>` : "";
  const ack = async () => {
    if (ask) await api("POST", "/api/action/ack", { date: ask.date });
    await syncState(false);
    show("home");
  };
  if (!a) {
    app.innerHTML = `
      <div class="screen">
        <p class="label">${label}</p>
        ${title("おつかれさま。\n今日はもう大丈夫。")}
        <p class="sub">いま急いでやることはありません。</p>
        ${tip}
        <div class="actions"><button class="dark" id="home">わかった</button></div>
      </div>`;
    document.getElementById("home").onclick = ack;
    return;
  }
  app.innerHTML = `
    <div class="screen">
      <p class="label">${label}</p>
      ${title("おつかれさま。\n今日はこれだけ。")}
      <div class="rescue">
        <div class="top">${a.kind === "freeze" ? ICON.snow : ICON.pan}${a.seconds ? `<span class="pill">${esc(fmtSeconds(a.seconds))}</span>` : ""}</div>
        <p class="text">${esc(a.text)}</p>
        ${a.note ? `<p class="small">${esc(a.note)}</p>` : ""}
        <p class="storage">${a.storage ? `保存の目安：${esc(a.storage)}` : `目安の出典：${esc(a.source)}`}</p>
      </div>
      ${tip}
      <div class="actions">
        ${remind}
        <button class="dark" id="ack">わかった</button>
        <button class="link" id="used">${esc(a.name)}はもう使った</button>
      </div>
    </div>`;
  document.getElementById("ack").onclick = ack;
  document.getElementById("used").onclick = async () => {
    lastAction = await api("POST", "/api/action/already-used", { date: ask.date });
    await syncState(false);
    render();
  };
}

// ---------------- 画面5 冷凍した？ ----------------

function renderFreeze() {
  if (freezeResult) {
    const r = freezeResult;
    app.innerHTML = `
      <div class="screen">
        <p class="label">${esc(r.label)}</p>
        ${title(r.title)}
        <p class="sub">${esc(r.body)}</p>
        <div class="actions"><button class="dark" id="home">閉じる</button></div>
      </div>`;
    document.getElementById("home").onclick = () => { freezeResult = null; show("home"); };
    return;
  }
  const fc = serverState.freeze_check;
  app.innerHTML = `
    <div class="screen">
      <p class="label">${esc(fmtTime(serverState.now))} の確認</p>
      ${title(`${fc.name}、\n${fc.question}`)}
      <div class="big-choice">
        <button class="primary" id="done">した</button>
        <button id="later">まだ</button>
      </div>
      <button class="link" id="gone">もう残っていなかった</button>
      <p class="note" style="margin-top:18px">「した」を押した日は、ホームの今週のカードの「守れた」回数に入ります。${fc.can_reask ? "" : "<br>「まだ」のときは、今日はもう聞きません。"}</p>
    </div>`;
  const send = async answer => {
    const res = await api("POST", "/api/freeze-check", { date: fc.date, answer });
    freezeResult = {
      done: { label: "記録しました", title: "救えました。", body: `${fc.name}は冷凍庫で待っています。冷凍庫の欄で確かめられます。` },
      later: res.reask_at
        ? { label: "あとで", title: "あとで\nもう一度だけ聞きます", body: `${fmtTime(res.reask_at)}ごろに、もう一度通知します。` }
        : { label: "あとで", title: "今日は\nここまでにします", body: "もう通知はしません。明日、冷蔵庫の中身で確かめてください。" },
      gone: { label: "在庫から消しました", title: "教えてくれて\nありがとう。", body: `${fc.name}を在庫から消しました。` },
    }[answer];
    await syncState(false);
    render();
  };
  document.getElementById("done").onclick = () => send("done");
  document.getElementById("later").onclick = () => send("later");
  document.getElementById("gone").onclick = () => send("gone");
}

// ---------------- 画面6 「作る」→ 使った食材 ----------------

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
            <span>${esc(i.name)}</span><small>${esc(fmtShort(i.purchased_at))}${i.freshness === "soon" ? "・早めに" : ""}</small>
          </button>`).join("") : `<p class="sub">冷蔵庫に登録された食材はありません。</p>`}
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
      <p class="sub">冷蔵の日数と冷凍できるかは USDA FoodKeeper（購入日から）を基本にし、冷凍の期間は日本の家庭の冷凍庫を前提に農林水産省の「2〜3週間」を使っています。食い違うときは短い方（安全側）に合わせています。</p>
      <p class="sub">パッケージに消費期限が書いてあれば、そちらが最優先です。この表は「期限が分からないときの目安」です。</p>
      ${s.items.map(i => `
        <div class="src-item">
          <div class="top"><b>${esc(i.name)}</b><span class="tag ${i.fridge_days <= 2 ? "soon" : "ok"}">冷蔵 ${i.fridge_days}日</span></div>
          <p>${esc(i.fridge_basis)}</p>
          <p>救済行動：${esc(i.rescue)}</p>
          ${i.action_basis ? `<p>${esc(i.action_basis)}</p>` : ""}
          <p>出典：${i.refs.map(r => esc(refName(r))).join("／")}</p>
        </div>`).join("")}
      <h2>出典一覧</h2>
      <ul class="src-list">
        ${Object.values(s.sources).map(r => `<li><a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.name)}</a><br><span class="sub">${esc(r.used_for)}</span></li>`).join("")}
      </ul>
      <div class="actions"><button class="dark" id="back">もどる</button></div>
    </div>`;
  document.getElementById("back").onclick = () => show(draft ? "confirm" : "home");
}

// ---------------- 通知（Webプッシュ） ----------------

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

async function setupNotifyCard(seq) {
  const el = document.getElementById("notify");
  if (!el) return;
  const cfg = config;
  const supported = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
  let body;
  let sub = null;
  if (!cfg.push_available) {
    body = `<p class="sub">サーバーでWebプッシュが使えません（pywebpush が入っていません）。</p>`;
  } else if (!window.isSecureContext) {
    body = `<p class="sub">通知を受け取るには、https:// で始まるアドレスで開いてください。それまでは画面を開いておけば18時に問いかけが出ます。</p>`;
  } else if (!supported) {
    body = isIOS && !isStandalone
      ? `<p class="sub">iPhoneでは、共有ボタンから「ホーム画面に追加」し、追加したアイコンから開くと通知を受け取れます（iOS 16.4以降）。</p>`
      : `<p class="sub">このブラウザは通知に対応していません。</p>`;
  } else {
    sub = await currentSubscription().catch(() => null);
    body = sub && Notification.permission === "granted"
      ? `<p>18時の問いかけと、夜の「冷凍した？」を、この端末で受け取ります。</p>
         <div class="row"><button id="push-test">テスト通知</button><button id="push-off">止める</button></div>`
      : `<p class="sub">急ぐ食材がある日だけ、18時に「今日、作れそう？」と通知します。</p>
         <button id="push-on">通知を受け取る</button>`;
  }
  if (seq !== renderSeq) return;
  el.innerHTML = `<h2>通知</h2>${body}<p class="error" id="push-err" hidden></p>`;
  const fail = e => { const p = document.getElementById("push-err"); p.hidden = false; p.textContent = e.message || String(e); };
  const on = document.getElementById("push-on");
  if (on) on.onclick = async () => {
    on.disabled = true;
    try {
      if (await Notification.requestPermission() !== "granted") throw new Error("通知が許可されませんでした。ブラウザの設定から許可してください");
      const reg = await navigator.serviceWorker.register("/sw.js");
      await navigator.serviceWorker.ready;
      const s = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToUint8(cfg.vapid_public_key) });
      await api("POST", "/api/push/subscribe", { subscription: s.toJSON() });
      setupNotifyCard(renderSeq);
    } catch (e) { on.disabled = false; fail(e); }
  };
  const test = document.getElementById("push-test");
  if (test) test.onclick = async () => {
    try { await api("POST", "/api/push/subscribe", { subscription: sub.toJSON() }); await api("POST", "/api/push/test"); }
    catch (e) { fail(e); }
  };
  const off = document.getElementById("push-off");
  if (off) off.onclick = async () => {
    try {
      await api("POST", "/api/push/unsubscribe", { endpoint: sub.endpoint });
      await sub.unsubscribe();
      setupNotifyCard(renderSeq);
    } catch (e) { fail(e); }
  };
}

// ---------------- 描画とデモ操作 ----------------

async function render() {
  const seq = ++renderSeq;
  document.body.dataset.view = view;
  try {
    if (view === "home") await renderHome(seq);
    else if (view === "fridge") await renderFridge(seq);
    else if (view === "receipt") renderReceipt();
    else if (view === "confirm") renderConfirm();
    else if (view === "ask") await renderAsk(seq);
    else if (view === "action") renderAction();
    else if (view === "freeze") renderFreeze();
    else if (view === "cook") await renderCook(seq);
    else if (view === "sources") await renderSources(seq);
  } catch (e) {
    app.innerHTML = `<div class="screen"><p class="error">エラー：${esc(e.message)}</p><button class="dark" id="home">ホームへ</button></div>`;
    document.getElementById("home").onclick = () => show("home");
  }
}

const DEBUG = {
  reset: () => api("POST", "/api/debug/reset"),
  wed18: () => api("POST", "/api/debug/advance", { to: "2026-10-28T18:00:00+09:00" }),
  trigger: () => api("POST", "/api/debug/trigger-ask"),
  remind: () => api("POST", "/api/debug/trigger-remind"),
  plus60: () => api("POST", "/api/debug/advance", { minutes: 60 }),
  satGood: async () => {
    await api("POST", "/api/debug/use", { ingredient_ids: ["moyashi", "tofu_kinu"] });
    await api("POST", "/api/debug/advance", { to: "2026-10-31T10:00:00+09:00" });
  },
  satBad: () => api("POST", "/api/debug/advance", { to: "2026-10-31T10:00:00+09:00" }),
};

document.querySelectorAll("[data-debug]").forEach(b => b.onclick = async () => {
  const key = b.dataset.debug;
  try { await DEBUG[key](); }
  catch (e) { alert(e.message); return; }
  freezeResult = null;
  draft = null;
  document.getElementById("debug").open = false;
  view = "home";
  await syncState(true);
  render();
});

if ("serviceWorker" in navigator && window.isSecureContext) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
  // 通知をタップしてアプリが前に出たとき、問いかけ（または「冷凍した？」）の画面を出す
  navigator.serviceWorker.addEventListener("message", e => { if (e.data && e.data.type === "open-ask") syncState(true); });
}
document.addEventListener("visibilitychange", () => { if (!document.hidden) syncState(false); });

(async function init() {
  [config, { items: ingredients }] = await Promise.all([api("GET", "/api/config"), api("GET", "/api/ingredients")]);
  document.getElementById("debug").hidden = !config.demo;
  await syncState(true);
  if (view === "home") render();
  setInterval(() => syncState(false), 5000);
})();
