/* LilyPad dash: a glanceable, always-on display. Polls /api/dash and listens
   on /api/events so new reminders appear (and optionally chime) instantly. */
(function () {
  "use strict";
  var body = document.body;
  var tz = body.getAttribute("data-tz") || undefined;
  var serverSound = body.getAttribute("data-sound") === "1";
  var timeFmt, clockFmt;
  try {
    timeFmt = new Intl.DateTimeFormat([], { hour: "numeric", minute: "2-digit", timeZone: tz });
    clockFmt = new Intl.DateTimeFormat([], { hour: "numeric", minute: "2-digit", weekday: "short", timeZone: tz });
  } catch (e) {
    timeFmt = new Intl.DateTimeFormat([], { hour: "numeric", minute: "2-digit" });
    clockFmt = timeFmt;
  }
  var data = null, offset = 0, lastLoad = 0;
  var soundOn = false, audio = null, seenAlerts = {};
  var SOUND_KEY = "lilypad-dash-sound";

  function now() { return Date.now() + offset; }
  function $(sel) { return document.querySelector(sel); }
  function node(tag, cls, text) {
    var el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text !== undefined && text !== null) el.textContent = text;
    return el;
  }
  function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); }
  function duration(mins) {
    mins = Math.round(mins);
    if (mins < 60) return mins + "m";
    var h = Math.floor(mins / 60), m = mins % 60;
    if (h < 48) return m ? h + "h " + (m < 10 ? "0" : "") + m + "m" : h + "h";
    return Math.floor(h / 24) + "d " + (h % 24) + "h";
  }
  function ago(ts) {
    if (!ts) return "—";
    var mins = (now() - Date.parse(ts)) / 60000;
    return mins < 1 ? "just now" : duration(mins) + " ago";
  }
  function until(ts) {
    var mins = (Date.parse(ts) - now()) / 60000;
    if (mins >= 1) return "in " + duration(mins);
    if (mins > -1) return "now";
    return duration(-mins) + " overdue";
  }
  function at(ts) { return ts ? timeFmt.format(new Date(Date.parse(ts))) : ""; }
  function timer(ts) {
    var secs = Math.max(0, Math.floor((now() - Date.parse(ts)) / 1000));
    var h = Math.floor(secs / 3600), m = Math.floor((secs % 3600) / 60), s = secs % 60;
    var pad = function (n) { return (n < 10 ? "0" : "") + n; };
    return (h ? h + ":" + pad(m) : m) + ":" + pad(s);
  }
  function volume(ml, unit) {
    if (!ml) return "";
    return unit === "oz" ? (ml / 29.5735).toFixed(1) + " oz" : Math.round(ml) + " ml";
  }

  function post(url) {
    return fetch(url, { method: "POST", credentials: "same-origin" }).then(load);
  }

  // --- rendering -----------------------------------------------------------

  function renderAlerts() {
    var box = $("[data-alerts]");
    clear(box);
    (data.vital_alerts || []).forEach(function (a) {
      var row = node("div", "dash-alert critical");
      var text = node("div", "dash-alert-text");
      text.appendChild(node("strong", null, a.title));
      text.appendChild(node("span", null, a.message));
      row.appendChild(text);
      var btn = node("button", "dash-btn", "Acknowledge");
      btn.type = "button";
      btn.addEventListener("click", function () { post("/api/alerts/" + a.id + "/ack"); });
      row.appendChild(btn);
      box.appendChild(row);
    });
    (data.reminders || []).forEach(function (r) {
      if (["window", "due", "overdue"].indexOf(r.stage) < 0 || !r.in_active_hours) return;
      var row = node("div", "dash-alert " + r.stage);
      var text = node("div", "dash-alert-text");
      text.appendChild(node("strong", null, r.name + ": " + r.stage_label.toLowerCase()));
      text.appendChild(node("span", null, "Due " + at(r.due_at) + " (" + until(r.due_at) + ")"));
      row.appendChild(text);
      var btn = node("button", "dash-btn", "Snooze 15m");
      btn.type = "button";
      btn.addEventListener("click", function () { post("/api/reminders/" + r.id + "/snooze?minutes=15"); });
      row.appendChild(btn);
      box.appendChild(row);
    });
    box.hidden = !box.firstChild;
  }

  function renderFeed() {
    var f = data.feeding, big = $("[data-feed-big]"), sub = $("[data-feed-sub]"), next = $("[data-feed-next]");
    var tile = $(".dash-feed");
    var rem = (data.reminders || []).filter(function (r) { return r.kind === "feeding"; })[0];
    tile.className = "dash-tile dash-feed" + (rem ? " stage-" + rem.stage : "");
    if (f.active) {
      big.textContent = timer(f.active.started_at);
      big.setAttribute("data-live-timer", f.active.started_at);
      sub.textContent = "Feeding now · " + f.active.method_label + (f.active.side_label ? " · " + f.active.side_label : "") + " · since " + at(f.active.started_at);
    } else {
      big.removeAttribute("data-live-timer");
      if (f.last) {
        big.textContent = ago(f.last.started_at);
        var bits = [f.last.method_label + (f.last.side_label ? " " + f.last.side_label.toLowerCase() : "")];
        if (f.last.duration_min) bits.push(duration(f.last.duration_min));
        if (f.last.amount_ml) bits.push(volume(f.last.amount_ml, data.volume_unit));
        if (f.last.quality) bits.push(f.last.quality.toLowerCase());
        sub.textContent = "since last feed started (" + at(f.last.started_at) + ") · " + bits.join(" · ");
      } else {
        big.textContent = "No feeds yet";
        sub.textContent = "";
      }
    }
    clear(next);
    if (rem && rem.stage !== "in_progress") {
      next.appendChild(node("span", "pill stage-" + rem.stage, rem.stage_label));
      next.appendChild(document.createTextNode(" Next due " + at(rem.due_at) + " (" + until(rem.due_at) + ")"));
    }
    if (f.suggested_side && !f.active) {
      next.appendChild(node("div", "dash-hint", "Suggested side: " + f.suggested_side));
    }
  }

  function renderDiapers() {
    var d = data.diaper;
    $("[data-diaper-last]").textContent = d.last ? ago(d.last.occurred_at) + " · " + d.last.kind_label.toLowerCase() : "—";
    $("[data-diaper-wet]").textContent = d.last_wet ? ago(d.last_wet.occurred_at) : "—";
    $("[data-diaper-dirty]").textContent = d.last_dirty ? ago(d.last_dirty.occurred_at) + (d.last_dirty.color_label ? " · " + d.last_dirty.color_label.toLowerCase() : "") : "—";
    var flag = $("[data-diaper-flag]");
    flag.hidden = !(d.last_dirty && d.last_dirty.flag);
    flag.textContent = d.last_dirty && d.last_dirty.flag ? "⚠ " + d.last_dirty.flag : "";
  }

  function renderCounts() {
    var c = data.last_24h, box = $("[data-counts]");
    clear(box);
    var items = [[c.feeds, "feeds"], [duration(c.feed_minutes || 0), "feeding"], [c.wet, "wet"], [c.dirty, "dirty"]];
    if (c.bottle_ml) items.splice(2, 0, [volume(c.bottle_ml, data.volume_unit), "bottle"]);
    items.forEach(function (it) {
      var s = node("div", "dash-stat");
      s.appendChild(node("span", "v", String(it[0])));
      s.appendChild(node("span", "l", it[1]));
      box.appendChild(s);
    });
  }

  function renderReminders() {
    var list = $("[data-reminders]");
    clear(list);
    var items = (data.reminders || []).slice().sort(function (a, b) { return Date.parse(a.due_at) - Date.parse(b.due_at); });
    if (!items.length) list.appendChild(node("li", "muted", "No reminders enabled."));
    items.forEach(function (r) {
      var li = node("li", "stage-" + r.stage);
      li.appendChild(node("span", "dash-li-name", r.name));
      var right = node("span", "dash-li-val");
      right.appendChild(node("span", "pill stage-" + r.stage, r.stage_label));
      right.appendChild(document.createTextNode(r.stage === "in_progress" ? " running" : " " + at(r.due_at) + " · " + until(r.due_at)));
      li.appendChild(right);
      list.appendChild(li);
    });
  }

  function renderVitals() {
    var list = $("[data-vitals]");
    clear(list);
    var items = data.vitals || [];
    if (!items.length) list.appendChild(node("li", "muted", "No vitals configured."));
    items.forEach(function (v) {
      var li = node("li", "status-" + (v.status || "none"));
      li.appendChild(node("span", "dash-li-name", v.name));
      var right = node("span", "dash-li-val");
      if (v.value === null) {
        right.textContent = "—";
      } else {
        right.appendChild(node("strong", null, v.display));
        if (v.status === "alert") right.appendChild(node("span", "pill status-alert", "⚠ alert"));
        else if (v.status === "watch") right.appendChild(node("span", "pill status-watch", "watch"));
        right.appendChild(document.createTextNode(" " + ago(v.measured_at)));
      }
      li.appendChild(right);
      list.appendChild(li);
    });
  }

  function render() {
    if (!data) return;
    document.querySelectorAll("[data-bind]").forEach(function (el) {
      var path = el.getAttribute("data-bind").split("."), v = data;
      path.forEach(function (k) { v = v ? v[k] : ""; });
      el.textContent = v || "";
    });
    renderAlerts();
    renderFeed();
    renderDiapers();
    renderCounts();
    renderReminders();
    renderVitals();
    var updated = $("[data-updated]");
    updated.textContent = "Updated " + timeFmt.format(new Date(now()));
    var attention = (data.vital_alerts || []).length || (data.reminders || []).some(function (r) {
      return (r.stage === "due" || r.stage === "overdue") && r.in_active_hours;
    });
    body.classList.toggle("attention", !!attention);
  }

  function load() {
    lastLoad = Date.now();
    return fetch("/api/dash", { cache: "no-store", credentials: "same-origin" })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (json) {
        offset = Date.parse(json.now) - Date.now();
        data = json;
        if (!json.dash_sound) { serverSound = false; }
        updateSoundButton();
        render();
        setConn(true);
      })
      .catch(function () { setConn(false); });
  }

  function setConn(ok) {
    var c = $("[data-conn]");
    c.classList.toggle("ok", ok);
    c.title = ok ? "Connected" : "Can't reach LilyPad — retrying";
    if (!ok) $("[data-updated]").textContent = "Can't reach LilyPad — retrying…";
  }

  // --- sound -----------------------------------------------------------------

  function chime(urgent) {
    if (!soundOn || !serverSound) return;
    try {
      audio = audio || new (window.AudioContext || window.webkitAudioContext)();
      var t = audio.currentTime;
      var notes = urgent ? [880, 660, 880, 660] : [660, 880];
      notes.forEach(function (freq, i) {
        var o = audio.createOscillator(), g = audio.createGain();
        o.type = "sine";
        o.frequency.value = freq;
        g.gain.setValueAtTime(0.0001, t + i * 0.28);
        g.gain.exponentialRampToValueAtTime(0.25, t + i * 0.28 + 0.03);
        g.gain.exponentialRampToValueAtTime(0.0001, t + i * 0.28 + 0.25);
        o.connect(g); g.connect(audio.destination);
        o.start(t + i * 0.28); o.stop(t + i * 0.28 + 0.27);
      });
    } catch (e) { /* audio not available */ }
  }
  function updateSoundButton() {
    var btn = $("[data-sound-toggle]");
    btn.hidden = !serverSound;
    btn.textContent = "Sound: " + (soundOn ? "on" : "off");
    btn.setAttribute("aria-pressed", soundOn ? "true" : "false");
  }

  // --- keep the screen awake --------------------------------------------------

  var wakeLock = null;
  function keepAwake() {
    if (!("wakeLock" in navigator) || document.hidden) return;
    navigator.wakeLock.request("screen").then(function (l) { wakeLock = l; }).catch(function () {});
  }

  // --- wiring -------------------------------------------------------------------

  document.addEventListener("DOMContentLoaded", function () {
    try { soundOn = localStorage.getItem(SOUND_KEY) === "1"; } catch (e) {}
    updateSoundButton();
    $("[data-sound-toggle]").addEventListener("click", function () {
      soundOn = !soundOn;
      try { localStorage.setItem(SOUND_KEY, soundOn ? "1" : "0"); } catch (e) {}
      updateSoundButton();
      if (soundOn) chime(false); // also unlocks audio after a user gesture
    });

    var clock = $("[data-clock]");
    setInterval(function () {
      clock.textContent = clockFmt.format(new Date(now()));
      var t = document.querySelector("[data-live-timer]");
      if (t) t.textContent = timer(t.getAttribute("data-live-timer"));
    }, 1000);
    setInterval(render, 15000);
    setInterval(function () { if (Date.now() - lastLoad > 55000) load(); }, 60000);

    if (window.EventSource) {
      var es = new EventSource("/api/events");
      es.addEventListener("hello", function () { setConn(true); });
      es.addEventListener("changed", function () { load(); });
      es.addEventListener("alert", function (e) {
        var alert = {};
        try { alert = JSON.parse(e.data); } catch (err) {}
        if (alert.id && !seenAlerts[alert.id]) {
          seenAlerts[alert.id] = true;
          chime(alert.priority >= 4 || alert.source === "vital");
        }
        load();
      });
      es.onerror = function () { setConn(false); };
    }
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) { load(); keepAwake(); }
    });
    keepAwake();
    load();
  });
})();
