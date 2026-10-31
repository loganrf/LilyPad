/* Applies the saved display theme before first paint (no flash of the wrong
   theme at 3 a.m.). Themes are per-device and never sent to the server. */
(function () {
  "use strict";
  var script = document.currentScript;
  var isDash = !!(script && script.hasAttribute("data-dash"));
  var key = isDash ? "lilypad-theme-dash" : "lilypad-theme";
  var order = isDash ? ["dark", "night", "light"] : ["auto", "light", "dark", "night"];
  var labels = { auto: "Auto", light: "Light", dark: "Dark", night: "Night" };

  function read() {
    try { return localStorage.getItem(key); } catch (e) { return null; }
  }
  function apply(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    var nodes = document.querySelectorAll("[data-theme-label]");
    for (var i = 0; i < nodes.length; i++) nodes[i].textContent = labels[theme];
  }
  var theme = read();
  if (order.indexOf(theme) < 0) theme = order[0];
  apply(theme);

  document.addEventListener("DOMContentLoaded", function () {
    apply(theme);
    var buttons = document.querySelectorAll("[data-theme-toggle]");
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].addEventListener("click", function () {
        theme = order[(order.indexOf(theme) + 1) % order.length];
        try { localStorage.setItem(key, theme); } catch (e) { /* private mode */ }
        apply(theme);
      });
    }
  });
})();
