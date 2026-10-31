/* LilyPad page behaviour. Everything here is progressive enhancement: every
   form works without JavaScript. */
(function () {
  "use strict";

  var serverNow = document.querySelector('meta[name="lilypad-now"]');
  var clockOffset = 0; // server time minus device time, in ms
  if (serverNow && serverNow.content) {
    var parsed = Date.parse(serverNow.content);
    if (!isNaN(parsed)) clockOffset = parsed - Date.now();
  }
  function now() { return Date.now() + clockOffset; }

  function duration(mins) {
    mins = Math.round(mins);
    if (mins < 60) return mins + "m";
    var h = Math.floor(mins / 60), m = mins % 60;
    if (h < 48) return m ? h + "h " + (m < 10 ? "0" : "") + m + "m" : h + "h";
    var d = Math.floor(h / 24);
    return d + "d " + (h % 24) + "h";
  }
  function ago(ts) {
    var mins = (now() - Date.parse(ts)) / 60000;
    if (isNaN(mins)) return "";
    if (mins < 0) return "in " + duration(-mins);
    if (mins < 1) return "just now";
    return duration(mins) + " ago";
  }
  function until(ts) {
    var mins = (Date.parse(ts) - now()) / 60000;
    if (isNaN(mins)) return "";
    if (mins >= 1) return "in " + duration(mins);
    if (mins > -1) return "now";
    return duration(-mins) + " overdue";
  }
  function timer(ts) {
    var secs = Math.max(0, Math.floor((now() - Date.parse(ts)) / 1000));
    var h = Math.floor(secs / 3600), m = Math.floor((secs % 3600) / 60), s = secs % 60;
    var pad = function (n) { return (n < 10 ? "0" : "") + n; };
    return (h ? h + ":" + pad(m) : m) + ":" + pad(s);
  }

  function refreshTimes() {
    document.querySelectorAll("[data-ago]").forEach(function (el) { el.textContent = ago(el.getAttribute("data-ago")); });
    document.querySelectorAll("[data-until]").forEach(function (el) { el.textContent = until(el.getAttribute("data-until")); });
  }
  function refreshTimers() {
    document.querySelectorAll("[data-timer]").forEach(function (el) { el.textContent = timer(el.getAttribute("data-timer")); });
  }

  // Show/hide form sections based on a radio choice: data-show-if="method:bottle,solids"
  function bindToggles(form) {
    var targets = form.querySelectorAll("[data-show-if]");
    function update() {
      targets.forEach(function (el) {
        var spec = el.getAttribute("data-show-if").split(":");
        var checked = form.querySelector('input[name="' + spec[0] + '"]:checked');
        var show = checked && spec[1].split(",").indexOf(checked.value) >= 0;
        el.hidden = !show;
      });
    }
    form.addEventListener("change", update);
    update();
  }

  function randomTopic() {
    var alphabet = "abcdefghijkmnpqrstuvwxyz23456789";
    var bytes = new Uint8Array(20);
    crypto.getRandomValues(bytes);
    var out = "lilypad-";
    for (var i = 0; i < bytes.length; i++) out += alphabet[bytes[i] % alphabet.length];
    return out;
  }

  function vitalFeedback(input) {
    var out = input.parentNode.querySelector("[data-range-feedback]");
    if (!out) return;
    var v = parseFloat(String(input.value).replace(",", "."));
    var num = function (name) { var a = input.getAttribute(name); return a === "" || a === null ? null : parseFloat(a); };
    var lo = num("data-alert-low"), hi = num("data-alert-high"), emin = num("data-expected-min"), emax = num("data-expected-max");
    out.className = "range-feedback";
    if (isNaN(v)) { out.textContent = ""; return; }
    if ((lo !== null && v <= lo) || (hi !== null && v >= hi)) {
      out.textContent = "⚠ At or beyond the alert limit. Saving will send an alert.";
      out.classList.add("is-alert");
    } else if ((emin !== null && v < emin) || (emax !== null && v > emax)) {
      out.textContent = "Outside the expected range.";
      out.classList.add("is-watch");
    } else if (lo !== null || hi !== null || emin !== null || emax !== null) {
      out.textContent = "✓ Within range.";
      out.classList.add("is-ok");
    } else {
      out.textContent = "";
    }
  }

  // Chart tooltips (report page). Text is set with textContent only.
  function bindTooltips() {
    var marks = document.querySelectorAll("[data-tip]");
    if (!marks.length) return;
    var tip = document.createElement("div");
    tip.className = "tip";
    tip.setAttribute("role", "tooltip");
    tip.hidden = true;
    document.body.appendChild(tip);
    function show(el, x, y) {
      tip.textContent = el.getAttribute("data-tip");
      tip.hidden = false;
      var w = tip.offsetWidth, h = tip.offsetHeight;
      var left = Math.min(window.innerWidth - w - 8, Math.max(8, x - w / 2));
      var top = y - h - 12 < 8 ? y + 16 : y - h - 12;
      tip.style.left = left + "px";
      tip.style.top = top + window.scrollY + "px";
    }
    marks.forEach(function (el) {
      el.addEventListener("pointerenter", function (e) { show(el, e.clientX, e.clientY); });
      el.addEventListener("pointermove", function (e) { show(el, e.clientX, e.clientY); });
      el.addEventListener("pointerleave", function () { tip.hidden = true; });
      el.addEventListener("focus", function () { var r = el.getBoundingClientRect(); show(el, r.left + r.width / 2, r.top); });
      el.addEventListener("blur", function () { tip.hidden = true; });
    });
  }

  // A form's Save bar is pinned just under the (sticky) top bar, whose height
  // changes as it wraps on narrow screens.
  function trackTopbar() {
    var bar = document.querySelector(".topbar");
    if (!bar || !document.querySelector(".form-actions-top")) return;
    var set = function () { document.documentElement.style.setProperty("--topbar-h", bar.offsetHeight + "px"); };
    set();
    if (window.ResizeObserver) new ResizeObserver(set).observe(bar);
    else window.addEventListener("resize", set);
  }

  // Rows that scroll sideways on a phone (admin sections, timeline filters)
  // open with the current item in view rather than cut off past the edge.
  function revealCurrent() {
    document.querySelectorAll(".subnav, .filter-row").forEach(function (row) {
      var cur = row.querySelector("[aria-current]");
      if (!cur || row.scrollWidth <= row.clientWidth) return;
      var r = row.getBoundingClientRect(), c = cur.getBoundingClientRect();
      if (c.left < r.left || c.right > r.right) row.scrollLeft += c.left - r.left - (r.width - c.width) / 2;
    });
  }

  // Live refresh: when the other parent logs something, reload this screen.
  function bindLive() {
    if (!document.body.hasAttribute("data-live") || !window.EventSource) return;
    var pending = null;
    function busy() {
      var a = document.activeElement;
      return a && (a.tagName === "INPUT" || a.tagName === "TEXTAREA" || a.tagName === "SELECT");
    }
    function scheduleReload() {
      if (pending) return;
      pending = setTimeout(function () {
        pending = null;
        if (busy() || document.hidden) { scheduleReload(); return; }
        window.location.replace(window.location.pathname + window.location.search.replace(/([?&])(toast|undo|undo_action)=[^&]*/g, "$1").replace(/[?&]+$/, ""));
      }, 1500);
    }
    var source = new EventSource("/api/events");
    source.addEventListener("changed", scheduleReload);
    source.addEventListener("alert", scheduleReload);
    var hiddenAt = null;
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) { hiddenAt = Date.now(); return; }
      if (hiddenAt && Date.now() - hiddenAt > 60000) scheduleReload();
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.documentElement.classList.add("js");
    trackTopbar();
    revealCurrent();
    refreshTimes();
    refreshTimers();
    setInterval(refreshTimes, 30000);
    if (document.querySelector("[data-timer]")) setInterval(refreshTimers, 1000);

    document.querySelectorAll("[data-toggle-form]").forEach(bindToggles);

    document.querySelectorAll("[data-custom-input]").forEach(function (input) {
      input.addEventListener("input", function () {
        var radio = input.closest("fieldset").querySelector("[data-custom-toggle]");
        if (radio) radio.checked = !!input.value;
      });
    });

    document.addEventListener("click", function (e) {
      var setter = e.target.closest("[data-set]");
      if (setter) {
        var target = document.querySelector(setter.getAttribute("data-set"));
        if (target) { target.value = setter.getAttribute("data-value"); target.dispatchEvent(new Event("input")); }
        return;
      }
      var appender = e.target.closest("[data-append]");
      if (appender) {
        var field = document.querySelector(appender.getAttribute("data-append"));
        if (field) {
          var tags = field.value.split(",").map(function (t) { return t.trim(); }).filter(Boolean);
          var value = appender.getAttribute("data-value");
          if (tags.indexOf(value) < 0) tags.push(value);
          field.value = tags.join(", ");
        }
        return;
      }
      var topic = e.target.closest("[data-random-topic]");
      if (topic) {
        var input = document.querySelector(topic.getAttribute("data-random-topic"));
        if (input) input.value = randomTopic();
        return;
      }
      if (e.target.closest("[data-print]")) { window.print(); return; }
      var exportLink = e.target.closest("[data-export-link]");
      if (exportLink) {
        var form = document.querySelector("[data-export-form]");
        if (form) {
          var params = new URLSearchParams(new FormData(form));
          Array.from(params.keys()).forEach(function (k) { if (!params.get(k)) params.delete(k); });
          var qs = params.toString();
          if (qs) { e.preventDefault(); window.location.href = exportLink.getAttribute("href") + "?" + qs; }
        }
      }
    });

    document.querySelectorAll("form[data-confirm]").forEach(function (form) {
      form.addEventListener("submit", function (e) {
        if (!window.confirm(form.getAttribute("data-confirm"))) e.preventDefault();
      });
    });
    document.querySelectorAll("[data-autosubmit]").forEach(function (el) {
      el.addEventListener("change", function () { el.form.submit(); });
    });

    var flagOut = document.querySelector("[data-flag-output]");
    if (flagOut) {
      var updateFlag = function () {
        var checked = document.querySelector('input[name="color"]:checked');
        var holder = checked && checked.closest("[data-flag]");
        flagOut.textContent = holder ? holder.getAttribute("data-flag") : "";
        flagOut.hidden = !holder;
      };
      document.addEventListener("change", function (e) { if (e.target.name === "color") updateFlag(); });
      updateFlag();
    }

    document.querySelectorAll(".vital-input input[type=number]").forEach(function (input) {
      input.addEventListener("input", function () { vitalFeedback(input); });
      vitalFeedback(input);
    });

    // Prevent double-submits from frantic tapping.
    document.querySelectorAll("form[method=post]").forEach(function (form) {
      form.addEventListener("submit", function (e) {
        if (e.defaultPrevented) return;
        if (form.dataset.submitting) { e.preventDefault(); return; }
        form.dataset.submitting = "1";
        setTimeout(function () { delete form.dataset.submitting; }, 4000);
      });
    });

    bindTooltips();
    bindLive();
  });
})();
