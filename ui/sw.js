// DeviceHub service worker: notifications with buttons (Approve / Deny on a session's device request).
// The page shows the notifications through this worker; a click on a button answers the request right here,
// even when the DeviceHub tab is in the background. Holds no secrets: each notification carries what it needs.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));

async function pages() {
  return (await self.clients.matchAll({ type: "window", includeUncontrolled: true }))
    .filter(c => new URL(c.url).origin === self.location.origin);
}

// tell the hub when a button could not do its job (devhubd.log)
function report(token, msg) {
  if (!token) return Promise.resolve();
  return fetch("/api/client-log", {
    method: "POST", headers: { "X-DevHub-Token": token, "Content-Type": "application/json" },
    body: JSON.stringify({ client: "ui", msg: "service worker: " + msg }),
  }).catch(() => {});
}

self.addEventListener("notificationclick", e => {
  const n = e.notification, d = n.data || {};
  n.close();
  e.waitUntil((async () => {
    if (e.action === "approve" || e.action === "deny") {
      let ok = false, msg = "";
      try {
        const r = await fetch("/api/lock/approve", {
          method: "POST",
          headers: { "X-DevHub-Token": d.token, "Content-Type": "application/json" },
          body: JSON.stringify({ client: "ui", via: "notification", dev: d.dev, session: d.session || null, name: d.name, allow: e.action === "approve" }),
        });
        const j = await r.json().catch(() => ({}));
        ok = r.ok && j.ok !== false;
        msg = j.error || j.msg || (r.ok ? "" : r.status + " " + r.statusText);
      } catch (err) {
        msg = "DeviceHub not reachable (" + err + ")";
      }
      if (!ok) await report(d.token, `${e.action} ${d.name} on ${d.dev} FAILED: ${msg}`);
      for (const c of await pages()) c.postMessage({ type: "devhub-answered", ok, msg, action: e.action, dev: d.dev, name: d.name });
      // say what happened: a quiet confirmation, or why it did not work
      await self.registration.showNotification(d.title || "DeviceHub", ok
        ? { body: `${e.action === "approve" ? "Approved" : "Denied"}: ${d.name}'s request.`,
            tag: n.tag + "|done", silent: true, data: { dev: d.dev } }
        : { body: `Could not ${e.action} ${d.name}'s request: ${msg || "no answer"}. Open DeviceHub to answer it.`, tag: n.tag + "|error", data: { dev: d.dev } });
      if (ok) setTimeout(() => self.registration.getNotifications({ tag: n.tag + "|done" }).then(ns => ns.forEach(x => x.close())), 6000);
      return;
    }
    // a click on the notification itself: bring a DeviceHub page up at that device
    const all = await pages();
    if (all.length) {
      await all[0].focus();
      all[0].postMessage({ type: "devhub-focus", dev: d.dev });
    } else {
      await self.clients.openWindow("/" + (d.dev ? "#dev=" + encodeURIComponent(d.dev) : ""));
    }
  })());
});
