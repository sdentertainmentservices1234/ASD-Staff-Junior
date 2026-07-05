/* ASDC Chamber App — minimal service worker.
   Purpose: satisfy the browser's requirement for a registered service worker
   so it offers a true "Install app" (which uses the app's own icon), instead
   of a Home-screen shortcut badged with the browser logo.

   This app is a CLOUD app that needs live internet (Firebase sync), so this
   worker deliberately does NOT cache app data or intercept network requests.
   It is intentionally pass-through: every request goes straight to the network.
*/

self.addEventListener("install", function(event) {
  self.skipWaiting();
});

self.addEventListener("activate", function(event) {
  // On every activation, purge any old caches so a stale copy of the app can't
  // linger and cause duplicated/outdated UI. Then take control of open pages.
  event.waitUntil(
    caches.keys().then(function(names){
      return Promise.all(names.map(function(n){ return caches.delete(n); }));
    }).then(function(){ return self.clients.claim(); })
  );
});

self.addEventListener("message", function(event) {
  // The page sends this when the user taps "Update now" so a waiting worker
  // activates immediately instead of waiting for every tab to close.
  if (event.data === "skipWaiting") { self.skipWaiting(); }
});

self.addEventListener("fetch", function(event) {
  // Pass-through: always network. No offline caching (cloud app needs live data).
  return;
});
