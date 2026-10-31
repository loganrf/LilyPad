/* LilyPad service worker. It makes LilyPad installable as an app and shows a
   friendly "can't reach LilyPad" page instead of a browser error when the
   server is out of reach (away from home Wi-Fi, server switched off...).

   Privacy: pages and API responses are never cached. They hold the baby's
   data and the admin area, and the server marks them no-store. The only
   things kept on the device are the static files listed below (styles,
   scripts, the logo) and the generic offline page, which has no data in it.

   Served from /sw.js (not /static/) so its scope covers the whole app. The
   asset version changes whenever a static file does, which changes this
   script, so browsers install the new worker and drop the old cache. */
"use strict";

var VERSION = {{ asset_version|tojson }};
var CACHE = "lilypad-static-" + VERSION;
var OFFLINE_URL = "/offline";
var PRECACHE = [
  OFFLINE_URL,
  "/static/app.css?v=" + VERSION,
  "/static/dash.css?v=" + VERSION,
  "/static/theme.js?v=" + VERSION,
  "/static/app.js?v=" + VERSION,
  "/static/dash.js?v=" + VERSION,
  "/static/pwa.js?v=" + VERSION,
  "/static/icons/icon.svg"
];

self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(CACHE)
      .then(function (cache) {
        // Bypass the HTTP cache so a fresh install never stores stale files.
        return cache.addAll(PRECACHE.map(function (url) { return new Request(url, { cache: "reload" }); }));
      })
      .then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    Promise.resolve()
      .then(function () {
        if (self.registration.navigationPreload) return self.registration.navigationPreload.enable();
      })
      .then(function () { return caches.keys(); })
      .then(function (keys) {
        return Promise.all(keys.filter(function (key) {
          return key.indexOf("lilypad-") === 0 && key !== CACHE;
        }).map(function (key) { return caches.delete(key); }));
      })
      .then(function () { return self.clients.claim(); })
  );
});

self.addEventListener("fetch", function (event) {
  var request = event.request;
  if (request.method !== "GET") return; // form posts go straight to the server
  var url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (request.mode === "navigate") {
    // Pages: always from the network (never cached); offline page on failure.
    event.respondWith(
      Promise.resolve(event.preloadResponse)
        .then(function (preloaded) { return preloaded || fetch(request); })
        .catch(function () {
          return caches.open(CACHE)
            .then(function (cache) { return cache.match(OFFLINE_URL); })
            .then(function (page) { return page || Response.error(); });
        })
    );
    return;
  }

  if (url.pathname.indexOf("/static/") === 0) {
    // Static files: cache first (URLs are versioned), network otherwise.
    event.respondWith(
      caches.open(CACHE)
        .then(function (cache) { return cache.match(request); })
        .then(function (hit) { return hit || fetch(request); })
    );
  }
  // Everything else (API, live updates, exports) goes to the network untouched.
});
