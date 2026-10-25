// Service Worker：Webプッシュを受けて通知を出す（冷蔵コーチ）。
// 問いかけには［作る］［無理］、リマインドには［入れた］［あとで］のボタンを付ける（2つまで）。
// ボタンが出ない端末（iPhoneなど）や通知の本体がタップされたときは、問いかけは画面3b、リマインドは画面5bを開く。
// Service Worker は localStorage を読めないので、端末ごとのIDは通知の中身（device_id）から使う。

const ACTIONS = {
  ask: [{ action: "cook", title: "作る" }, { action: "skip", title: "無理" }],
  remind: [{ action: "done", title: "入れた" }, { action: "later", title: "あとで" }],
};

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", event => event.waitUntil(self.clients.claim()));

self.addEventListener("push", event => {
  let data = { title: "冷蔵コーチ", body: "", url: "/" };
  try { data = { ...data, ...event.data.json() }; } catch (e) { /* テキストだけのときはそのまま */ }
  event.waitUntil(self.registration.showNotification(data.title, {
    body: data.body,
    icon: "/icons/icon-192.png",
    badge: "/icons/icon-192.png",
    tag: `reizo-${data.kind || "info"}`,
    renotify: true,
    actions: ACTIONS[data.kind] || [],
    data,
  }));
});

async function post(data, path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Device-Id": data.device_id },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function openApp(kind) {
  const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
  for (const w of wins) {
    if (w.url.startsWith(self.location.origin)) {
      await w.focus();
      w.postMessage({ type: "open", kind });
      return;
    }
  }
  await self.clients.openWindow(`/?open=${kind}`);
}

function fmtTime(iso) { return iso.split("T")[1].slice(0, 5); }

self.addEventListener("notificationclick", event => {
  const data = event.notification.data || {};
  event.notification.close();
  event.waitUntil((async () => {
    try {
      if (data.kind === "ask" && (event.action === "cook" || event.action === "skip")) {
        // 通知のボタンで答えた。「無理」は救済行動（画面4）、「作る」は使った食材（画面6）を開く
        await post(data, "/api/answer", { date: data.date, answer: event.action, taps: 1, via: "notification" });
        return openApp(event.action === "skip" ? "action" : "cook");
      }
      if (data.kind === "remind" && (event.action === "done" || event.action === "later")) {
        const res = await post(data, "/api/freeze-check", { date: data.date, answer: event.action, taps: 1, via: "notification" });
        const msg = event.action === "done"
          ? { title: "救えました。", body: "今日はもう何もしなくて大丈夫。ゆっくり休んでください。" }
          : res.reask_at
            ? { title: "了解。", body: `${fmtTime(res.reask_at)}に、もう一度だけ聞きます。それで今日はおしまい。` }
            : { title: "了解。", body: "今日はここまでにします。返事がなくても大丈夫です。" };
        return self.registration.showNotification(msg.title, {
          body: msg.body, icon: "/icons/icon-192.png", badge: "/icons/icon-192.png", tag: "reizo-remind", data: {},
        });
      }
    } catch (e) {
      // 送れなかったときは画面で答えてもらう
    }
    if (data.kind === "ask") return openApp("ask");
    if (data.kind === "remind") return openApp("remind");
    return openApp("home");
  })());
});
