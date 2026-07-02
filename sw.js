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
  event.waitUntil(self.clients.claim());
});

self.addEventListener("fetch", function(event) {
  // Pass-through: always network. No offline caching (cloud app needs live data).
  return;
});
