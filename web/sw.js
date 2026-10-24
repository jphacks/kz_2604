// Service Worker：Webプッシュを受けて通知を出し、タップでアプリを開く。
// 通知の中のボタンには頼らない（端末によって出ないため）。開いた画面で答えてもらう。

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", event => event.waitUntil(self.clients.claim()));

self.addEventListener("push", event => {
  let data = { title: "今日は無理", body: "", url: "/" };
  try { data = { ...data, ...event.data.json() }; } catch (e) { /* テキストだけのときはそのまま */ }
  event.waitUntil(self.registration.showNotification(data.title, {
    body: data.body,
    icon: "/icons/icon-192.png",
    badge: "/icons/icon-192.png",
    tag: "kyou-wa-muri",
    renotify: true,
    data: { url: data.url },
  }));
});

self.addEventListener("notificationclick", event => {
  event.notification.close();
  const url = new URL(event.notification.data?.url || "/", self.location.origin).href;
  event.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const w of wins) {
      if (w.url.startsWith(self.location.origin)) {
        await w.focus();
        w.postMessage({ type: "open-ask" });
        return;
      }
    }
    await self.clients.openWindow(url);
  })());
});
