/* LilyPad dash: a glanceable, always-on display. Polls /api/dash and listens
   on /api/events so new reminders appear (and optionally chime) instantly. */
(function () {
  "use strict";
  var body = document.body;
  var tz = body.getAttribute("data-tz") || undefined;
  var serverSound = body.getAttribute("data-sound") === "1";
  var timeFmt, clockFmt, dayFmt, partsFmt;
  try {
    timeFmt = new Intl.DateTimeFormat([], { hour: "numeric", minute: "2-digit", timeZone: tz });
    clockFmt = new Intl.DateTimeFormat([], { hour: "numeric", minute: "2-digit", weekday: "short", timeZone: tz });
    dayFmt = new Intl.DateTimeFormat([], { weekday: "short", month: "short", day: "numeric", timeZone: tz });
    partsFmt = new Intl.DateTimeFormat("en-US", { year: "numeric", month: "numeric", day: "numeric", timeZone: tz });
  } catch (e) {
    timeFmt = new Intl.DateTimeFormat([], { hour: "numeric", minute: "2-digit" });
    clockFmt = timeFmt;
    dayFmt = new Intl.DateTimeFormat([], { weekday: "short", month: "short", day: "numeric" });
    partsFmt = new Intl.DateTimeFormat("en-US", { year: "numeric", month: "numeric", day: "numeric" });
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
  function text(s) { return document.createTextNode(s); }
  // Short phrases ("in 2h 30m", "(1:31 PM)") stay on one line; long text
  // wraps at the " · " between them instead of mid-phrase.
  function keep(s, cls) { return node("span", "nowrap" + (cls ? " " + cls : ""), s); }
  function joined(parent, parts) {
    parts.forEach(function (p, i) {
      if (i) parent.appendChild(text(" · "));
      parent.appendChild(typeof p === "string" ? keep(p) : p);
    });
    return parent;
  }
  // "3h 07m" -> 3<small>h</small> 07<small>m</small>: digits stay big,
  // units shrink, so figures fit narrow tiles.
  function figure(s) {
    var el = node("span", "v");
    String(s).split(/(\s*[a-z]+)/i).forEach(function (part, i) {
      if (part) el.appendChild(i % 2 ? node("small", null, part) : text(part));
    });
    return el;
  }
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
  // Calendar day (as a day count) in the household time zone.
  function dayNumber(ms) {
    var p = {};
    partsFmt.formatToParts(new Date(ms)).forEach(function (x) { p[x.type] = +x.value; });
    return Math.round(Date.UTC(p.year, p.month - 1, p.day) / 864e5);
  }
  // "Today 9:15 AM", "Yesterday 11:40 PM", "Mon, Nov 2, 8:05 AM".
  function when(ts) {
    if (!ts) return "";
    var t = Date.parse(ts), days = dayNumber(now()) - dayNumber(t);
    var day = days === 0 ? "Today" : days === 1 ? "Yesterday" : dayFmt.format(new Date(t)) + ",";
    return day + " " + at(ts);
  }
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
      var body = node("div", "dash-alert-text");
      body.appendChild(node("strong", null, a.title));
      body.appendChild(node("span", null, a.message));
      row.appendChild(body);
      var btn = node("button", "dash-btn", "Acknowledge");
      btn.type = "button";
      btn.addEventListener("click", function () { post("/api/alerts/" + a.id + "/ack"); });
      row.appendChild(btn);
      box.appendChild(row);
    });
    (data.reminders || []).forEach(function (r) {
      if (["window", "due", "overdue"].indexOf(r.stage) < 0 || !r.in_active_hours) return;
      var row = node("div", "dash-alert " + r.stage);
      var body = node("div", "dash-alert-text");
      body.appendChild(node("strong", null, r.name + ": " + r.stage_label.toLowerCase()));
      var due = node("span", null, "Due ");
      due.appendChild(keep(at(r.due_at)));
      due.appendChild(text(" "));
      due.appendChild(keep("(" + until(r.due_at) + ")"));
      body.appendChild(due);
      row.appendChild(body);
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
    clear(sub);
    if (f.active) {
      big.textContent = timer(f.active.started_at);
      big.setAttribute("data-live-timer", f.active.started_at);
      var parts = ["Feeding now", f.active.method_label];
      if (f.active.side_label) {
        parts.push(f.active.side_label + (f.active.side === "both" && f.active.last_side_label ? ", now on " + f.active.last_side_label.toLowerCase() : ""));
      }
      parts.push("since " + at(f.active.started_at));
      joined(sub, parts);
    } else {
      big.removeAttribute("data-live-timer");
      if (f.last) {
        big.textContent = ago(f.last.started_at);
        var bits = [f.last.method_label + (f.last.side_label ? " " + f.last.side_label.toLowerCase() : "") +
          (f.last.side === "both" && f.last.last_side ? " (ended " + f.last.last_side + ")" : "")];
        if (f.last.duration_min) bits.push(duration(f.last.duration_min));
        if (f.last.amount_ml) bits.push(volume(f.last.amount_ml, data.volume_unit));
        if (f.last.quality) bits.push(f.last.quality.toLowerCase());
        var since = node("span", null, "since last feed started ");
        since.appendChild(keep("(" + at(f.last.started_at) + ")"));
        joined(sub, [since].concat(bits));
      } else {
        big.textContent = "No feeds yet";
      }
    }
    clear(next);
    if (rem && rem.stage !== "in_progress") {
      next.appendChild(node("span", "pill stage-" + rem.stage, rem.stage_label));
      next.appendChild(text("Next due "));
      next.appendChild(keep(at(rem.due_at)));
      next.appendChild(text(" "));
      next.appendChild(keep("(" + until(rem.due_at) + ")"));
    }
    $("[data-feed-hint]").textContent = f.suggested_side && !f.active ? "Suggested side: " + f.suggested_side : "";
  }

  function renderDiapers() {
    var d = data.diaper;
    $("[data-diaper-last]").textContent = d.last ? ago(d.last.occurred_at) : "—";
    $("[data-diaper-last-detail]").textContent = d.last ? d.last.kind_label.toLowerCase() : "";
    $("[data-diaper-wet]").textContent = d.last_wet ? ago(d.last_wet.occurred_at) : "—";
    $("[data-diaper-dirty]").textContent = d.last_dirty ? ago(d.last_dirty.occurred_at) : "—";
    $("[data-diaper-dirty-detail]").textContent = d.last_dirty && d.last_dirty.color_label ? d.last_dirty.color_label.toLowerCase() : "";
    var flag = $("[data-diaper-flag]");
    flag.hidden = !(d.last_dirty && d.last_dirty.flag);
    flag.textContent = d.last_dirty && d.last_dirty.flag ? "⚠ " + d.last_dirty.flag : "";
    $("[data-diaper-log]").hidden = !data.dash_diaper_buttons;
  }

  function renderCounts() {
    var c = data.last_24h, box = $("[data-counts]");
    clear(box);
    var items = [[c.feeds, "feeds"], [duration(c.feed_minutes || 0), "feeding"], [c.wet, "wet"], [c.dirty, "dirty"]];
    if (c.bottle_ml) items.splice(2, 0, [volume(c.bottle_ml, data.volume_unit), "bottle"]);
    items.forEach(function (it) {
      var s = node("div", "dash-stat");
      s.appendChild(figure(it[0]));
      s.appendChild(node("span", "l", it[1]));
      box.appendChild(s);
    });
  }

  function listItem(cls, name, pill) {
    var li = node("li", cls), head = node("div", "dash-li-head");
    head.appendChild(node("span", "dash-li-name", name));
    if (pill) head.appendChild(pill);
    li.appendChild(head);
    return li;
  }

  function renderReminders() {
    var list = $("[data-reminders]");
    clear(list);
    var items = (data.reminders || []).slice().sort(function (a, b) { return Date.parse(a.due_at) - Date.parse(b.due_at); });
    if (!items.length) list.appendChild(node("li", "muted", "No reminders enabled."));
    items.forEach(function (r) {
      var li = listItem("stage-" + r.stage, r.name, node("span", "pill stage-" + r.stage, r.stage_label));
      var sub = node("div", "dash-li-sub");
      if (r.stage === "in_progress") sub.textContent = "Running now";
      else joined(sub, [node("b", "nowrap", at(r.due_at)), until(r.due_at)]);
      li.appendChild(sub);
      list.appendChild(li);
    });
  }

  function renderVitals() {
    var list = $("[data-vitals]");
    clear(list);
    var items = data.vitals || [];
    if (!items.length) list.appendChild(node("li", "muted", "No vitals configured."));
    items.forEach(function (v) {
      var pill = null;
      if (v.status === "alert") pill = node("span", "pill status-alert", "⚠ alert");
      else if (v.status === "watch") pill = node("span", "pill status-watch", "watch");
      var li = listItem("status-" + (v.status || "none"), v.name, pill);
      var sub = node("div", "dash-li-sub");
      if (v.value === null) {
        sub.appendChild(node("span", "muted", "Not logged yet"));
      } else {
        // Admin → Settings → Display: time since, date and time, or both.
        var mode = data.vitals_time_display, parts = [node("b", "nowrap dash-reading", v.display)];
        if (mode === "date" || mode === "both") parts.push(when(v.measured_at));
        if (mode !== "date") parts.push(ago(v.measured_at));
        joined(sub, parts);
      }
      li.appendChild(sub);
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

  // --- logging a diaper (Admin → Settings → Dash display) ----------------------

  var UNDO_MS = 60000, logBusy = false, logTimer = null;

  function logMessage(parts, isError, ms) {
    var msg = $("[data-diaper-msg]");
    clear(msg);
    msg.classList.toggle("error", !!isError);
    parts.forEach(function (p) { msg.appendChild(typeof p === "string" ? node("span", null, p) : p); });
    clearTimeout(logTimer);
    logTimer = setTimeout(function () { clear(msg); }, ms);
  }

  function setLogBusy(busy) {
    logBusy = busy;
    document.querySelectorAll(".dash-log-btn").forEach(function (b) { b.disabled = busy; });
  }

  function undoDiaper(id, btn) {
    btn.disabled = true;
    fetch("/entry/diapers/" + id + "/delete", {
      method: "POST", credentials: "same-origin", body: new URLSearchParams({ undo: "1", next: "/dash" })
    })
      .then(function (r) {
        if (!r.ok) throw new Error(r.status);
        logMessage(["Undone."], false, 5000);
        load();
      })
      .catch(function () { logMessage(["Couldn't undo. Delete it from the timeline."], true, 15000); });
  }

  function logDiaper(kind) {
    if (logBusy) return;
    setLogBusy(true);
    fetch("/api/diapers?kind=" + encodeURIComponent(kind), { method: "POST", credentials: "same-origin" })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (entry) {
        var undo = node("button", "dash-btn", "Undo");
        undo.type = "button";
        undo.addEventListener("click", function () { undoDiaper(entry.id, undo); });
        var parts = [keep(entry.kind_label + " logged at " + at(entry.occurred_at)), undo];
        if (entry.kind === "dirty" || entry.kind === "mixed") {
          // Colour and consistency: the edit form returns to the dash. Only
          // /dash may be framed, so inside another dashboard it opens a tab.
          var details = node("a", "dash-btn", "Add details");
          details.href = "/diaper/" + entry.id + "/edit?next=%2Fdash";
          if (window.top !== window.self) { details.target = "_blank"; details.rel = "noopener"; }
          parts.push(details);
        }
        logMessage(parts, false, UNDO_MS);
        load();
      })
      .catch(function () { logMessage(["Couldn't log it. Try again."], true, 10000); })
      .then(function () { setLogBusy(false); });
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

    // The buttons post a plain form without this script; with it, a tap
    // logs in the background and the dash stays on screen.
    document.querySelectorAll(".dash-log-btn").forEach(function (btn) {
      btn.addEventListener("click", function (e) { e.preventDefault(); logDiaper(btn.value); });
    });

    var clock = $("[data-clock]");
    function tick() {
      clock.textContent = clockFmt.format(new Date(now()));
      var t = document.querySelector("[data-live-timer]");
      if (t) t.textContent = timer(t.getAttribute("data-live-timer"));
    }
    tick(); // no "--:--" placeholder for the first second
    setInterval(tick, 1000);
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
