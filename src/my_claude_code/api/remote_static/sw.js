self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("push", (event) => {
  let payload = {};
  if (event.data) {
    try {
      payload = event.data.json();
    } catch {
      payload = { title: "MCC Remote Control", body: event.data.text() };
    }
  }
  const title = payload.title || "MCC Remote Control";
  const body = payload.body || "Session activity update";
  const sessionId = payload.session_id || "";
  const targetUrl =
    payload.url || (sessionId ? `/rc?session=${encodeURIComponent(sessionId)}` : "/rc");
  event.waitUntil(
    self.registration.showNotification(title, {
      body,
      tag: payload.tag || (sessionId ? `mcc-rc-${sessionId}` : "mcc-rc"),
      data: { url: targetUrl, session_id: sessionId },
      renotify: true,
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const data = event.notification.data || {};
  const targetUrl = data.url || "/rc";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((clients) => {
      for (const client of clients) {
        if (client.url && client.url.includes("/rc") && "focus" in client) {
          if ("navigate" in client && targetUrl !== "/rc") {
            client.navigate(targetUrl).catch(() => {});
          }
          return client.focus();
        }
      }
      if (self.clients.openWindow) {
        return self.clients.openWindow(targetUrl);
      }
      return undefined;
    })
  );
});

