/* Installable-app support: registers the service worker (offline page),
   offers an "Install app" button where the browser supports one, shows the
   Add to Home Screen hint on iPhone/iPad, and makes the offline page
   reconnect by itself. Progressive enhancement only.

   Service workers need a secure context: HTTPS, or http://localhost. On plain
   http://<LAN address> browsers skip all of this; see docs/INSTALL_APP.md. */
(function () {
  "use strict";

  if ("serviceWorker" in navigator) {
    window.addEventListener("load", function () {
      navigator.serviceWorker.register("/sw.js").catch(function (err) {
        if (window.console) console.warn("LilyPad: service worker not registered:", err);
      });
    });
  }

  function installed() {
    return navigator.standalone === true ||
      (window.matchMedia && (window.matchMedia("(display-mode: standalone)").matches ||
                             window.matchMedia("(display-mode: fullscreen)").matches));
  }

  var deferredPrompt = null;

  // Chrome, Edge, Samsung Internet...: the browser says the app can be installed.
  window.addEventListener("beforeinstallprompt", function (e) {
    e.preventDefault();
    deferredPrompt = e;
    var box = document.querySelector("[data-install]");
    if (!box || installed()) return;
    box.querySelector("[data-install-button]").hidden = false;
    box.hidden = false;
  });

  window.addEventListener("appinstalled", function () {
    deferredPrompt = null;
    var box = document.querySelector("[data-install]");
    if (box) box.hidden = true;
  });

  function retryWhenOnline() {
    var busy = false;
    function check() {
      if (busy) return;
      busy = true;
      fetch("/healthz", { cache: "no-store" })
        .then(function (r) {
          if (!r.ok) return;
          // The offline page is shown in place of the page that failed to load,
          // so reloading retries that page. Opened directly, go home instead.
          if (window.location.pathname === "/offline") window.location.replace("/");
          else window.location.reload();
        })
        .catch(function () {})
        .then(function () { busy = false; });
    }
    setInterval(check, 15000);
    window.addEventListener("online", check);
    document.addEventListener("visibilitychange", function () { if (!document.hidden) check(); });
  }

  document.addEventListener("DOMContentLoaded", function () {
    if (document.querySelector("[data-offline]")) retryWhenOnline();

    var box = document.querySelector("[data-install]");
    if (!box || installed()) return;
    var button = box.querySelector("[data-install-button]");
    if (button) {
      button.addEventListener("click", function () {
        if (!deferredPrompt) return;
        // A prompt can only be shown once; the browser fires
        // beforeinstallprompt again if installing becomes possible later.
        deferredPrompt.prompt();
        deferredPrompt = null;
        box.hidden = true;
      });
    }
    // Safari on iPhone/iPad has no install prompt; navigator.standalone exists
    // only there, and is false when LilyPad is open in a browser tab.
    if (navigator.standalone === false) {
      box.querySelector("[data-install-ios]").hidden = false;
      box.hidden = false;
    }
  });
})();
