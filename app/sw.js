// Service worker: makes the app installable and opens the right session from an alert.
// It caches nothing, so every screen is live data from the laptop.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const hash = (e.notification.data && e.notification.data.hash) || "";
  e.waitUntil((async () => {
    const all = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const c of all) {
      if ("focus" in c) {
        await c.focus();
        if ("navigate" in c) return c.navigate("/" + hash);
        return;
      }
    }
    return self.clients.openWindow("/" + hash);
  })());
});
