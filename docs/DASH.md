# Dash displays

LilyPad can show its reminders and a live summary on a "dash": a tablet on
the nursery wall, a monitor in the kitchen, or a panel in a home dashboard
you already run. There are three ways to connect one, and you can use any
combination:

| Option | Best for | Push or poll |
|---|---|---|
| [Built-in `/dash` page](#1-the-built-in-dash-page) | A tablet, old phone, or browser in kiosk mode | Live push (Server-Sent Events) |
| [Dash webhook](#2-dash-webhook-push) | Home Assistant, Node-RED, n8n, a custom display | LilyPad POSTs each alert |
| [JSON feed](#3-json-feed-poll) | TRMNL, MagicMirror, Home Assistant REST sensors, scripts | Your dashboard polls |
| [Dash LED clocks](#4-dash-led-clocks) | A [Dash](https://github.com/loganrf/dash) matrix clock in the living room | LilyPad POSTs a short message |

The same reminders that go to ntfy go to the dash. Each reminder has a
**Dash** switch (Admin → Reminders) controlling the built-in page, the
webhook and Dash clocks.

---

## 1. The built-in `/dash` page

Open `http://<lilypad>:8080/dash` on the display. It shows:

- a large clock and the baby's name/age;
- a banner for anything that needs attention (feeding window open, due,
  overdue, vital-sign alerts), with **Snooze 15m** / **Acknowledge** buttons;
- time since the last feed (or a running timer during a feed), the next due
  time, and the suggested breast side;
- last diaper / wet / dirty, with a warning for concerning stool colours,
  and optional **Wet**, **Dirty** and **Both** buttons to log a change
  (see [Logging diapers from the dash](#logging-diapers-from-the-dash));
- last-24-hour totals, an "up next" list of every reminder, and the latest
  vitals with their status. Each vital shows the time since the check, the
  date and time of the check, or both (Admin → Settings → Display).

It updates instantly when anyone logs something or an alert fires, and
re-checks every minute in case the connection drops (the dot in the corner
is green while connected).

**Tips for a wall display**

- Install `/dash` as its own app (**LilyPad Dash**): it opens full screen,
  straight to the dash, and shows a "Can't reach LilyPad" screen that
  reconnects by itself after a power cut or Wi-Fi drop. See
  [INSTALL_APP.md](INSTALL_APP.md).
- The page asks the browser to keep the screen awake (Screen Wake Lock)
  while it's visible (browsers allow this only over HTTPS). Kiosk browsers such as *Fully Kiosk Browser*
  (Android) or Guided Access (iPad) keep it in front.
- Tap **Sound: off** once to enable a chime for new alerts (browsers only
  allow sound after a tap). An admin can disable chimes entirely in
  Settings → Dash display.
- The theme button cycles Dark → Night (dim red, for a nursery) → Light.
  The choice is saved on that device only.
- `/dash` is open (no password), like the home screen. See
  [PRIVACY_AND_SECURITY.md](PRIVACY_AND_SECURITY.md).

**Logging diapers from the dash**

Turn on Admin → Settings → Dash display → *Show Wet, Dirty and Both
buttons on the dash*. Each tap logs the change at that moment, and the dash
stays on screen:

- For one minute, the dash shows **Undo**, which deletes the entry again.
- After **Dirty** or **Both**, it also shows **Add details**. This opens the
  diaper form for colour and consistency and then goes back to the dash.
  The stool-colour warning needs the colour.
- The buttons are off by default. They do not change who can log: anyone
  who can reach LilyPad can already log a diaper from the home screen.

**Embedding in another dashboard (iframe)**

`/dash` may be framed by any site by default, so it works in a Home
Assistant *Webpage* card, Dashy, Homarr, Organizr and so on. To restrict
which sites may embed it, set e.g.
`LILYPAD_DASH_FRAME_ANCESTORS="'self' http://homeassistant.local:8123"`.
All other LilyPad pages refuse to be framed.

---

## 2. Dash webhook (push)

Admin → Settings → Dash display → *POST every reminder/alert to a webhook*.
LilyPad sends one JSON `POST` per notification:

```json
{
  "event": "lilypad.alert",
  "baby": null,
  "dash_url": "http://192.168.1.20:8080/dash",
  "alert": {
    "id": 42,
    "created_at": "2026-10-28T20:50:12Z",
    "source": "reminder",
    "reminder_id": 1,
    "stage": "due",
    "title": "Feeding due",
    "message": "Due now (8:50 PM). Last feed started 5:50 PM (3h 01m ago).",
    "priority": 3,
    "tags": ["baby_bottle"]
  },
  "context": {
    "reminder": {
      "id": 1, "name": "Feeding", "kind": "feeding", "stage": "due",
      "stage_label": "Due now", "anchor": "2026-10-28T17:50:00Z",
      "window_at": "2026-10-28T20:20:00Z", "due_at": "2026-10-28T20:50:00Z",
      "minutes_to_due": -0.2, "progress": 1.001, "snoozed_until": null,
      "in_active_hours": true, "interval_minutes": 180, "anchor_from_event": true
    }
  }
}
```

- `source` is `reminder`, `vital` (an out-of-range reading) or `test`.
- `stage` is `window`, `due` or `overdue` for reminders, `alert` for vitals.
- `priority` uses ntfy's 1–5 scale.
- For vital alerts, `context` is `{"param": "Temperature", "value": 100.8,
  "unit": "°F", "check_id": 7}`.
- `baby` is only filled in at the **Detailed** notification level, and
  `context` is empty at **Minimal**. `title`/`message` follow the same
  detail level as ntfy.
- Add authentication with *Extra headers*, one per line, e.g.
  `Authorization: Bearer abc123`. They are stored but never displayed again.
- Failures are retried once and recorded in Admin → Notifications.

### Home Assistant example

Create an automation with a webhook trigger (pick a long random ID), then
set the LilyPad webhook URL to
`http://homeassistant.local:8123/api/webhook/lilypad-7f3c9a…`.

```yaml
alias: LilyPad alert
trigger:
  - platform: webhook
    webhook_id: lilypad-7f3c9a2b5e
    allowed_methods: [POST]
    local_only: true
action:
  - service: persistent_notification.create
    data:
      title: "{{ trigger.json.alert.title }}"
      message: "{{ trigger.json.alert.message }}"
  # e.g. flash the nursery lamp for overdue feeds
  - if: "{{ trigger.json.alert.stage == 'overdue' }}"
    then:
      - service: light.turn_on
        target: { entity_id: light.nursery }
        data: { flash: short }
mode: queued
```

---

## 3. JSON feed (poll)

`GET /api/dash` returns the same snapshot the built-in page uses (open, no
password). Abbreviated:

```json
{
  "now": "2026-10-28T20:51:00Z",
  "baby": {"name": "Lily", "age": "3 weeks 6 days"},
  "feeding": {
    "active": null,
    "last": {"started_at": "…", "ended_at": "…", "method": "breast", "side": "both",
             "last_side": "left", "duration_min": 20.0, "amount_ml": null, "quality": "Good",
             "logged_by": "Sam"},
    "suggested_side": "right"
  },
  "diaper": {"last": {…}, "last_wet": {…}, "last_dirty": {"color": "yellow", "flag": null, …}},
  "last_24h": {"feeds": 8, "feed_minutes": 164.0, "bottle_ml": 0, "wet": 7, "dirty": 3, "diapers": 9},
  "volume_unit": "ml",
  "reminders": [{"id": 1, "name": "Feeding", "stage": "window", "due_at": "…", …}],
  "routines": [],
  "vitals": [{"name": "Temperature", "display": "99.1 °F", "status": "ok", "measured_at": "…"}],
  "vital_alerts": [],
  "alerts": [ /* notifications from the last 12 hours */ ],
  "dash_sound": true,
  "dash_diaper_buttons": false,
  "vitals_time_display": "ago"
}
```

All timestamps are UTC (ISO 8601 with `Z`).

`dash_diaper_buttons` tells if the built-in page shows its diaper buttons.
`vitals_time_display` is `ago` (time since the check), `date` (date and
time of the check) or `both`.

A feed's `side` summarises it (`left`, `right` or `both` if the breast was
switched mid-feed); `last_side` is the breast in use last (`left`/`right`,
or `null` if unknown). `suggested_side` is the opposite of the most recent
breastfeed's `last_side`.

**Home Assistant REST sensor**

```yaml
rest:
  - resource: http://192.168.1.20:8080/api/dash
    scan_interval: 60
    sensor:
      - name: Lily last feed
        value_template: "{{ value_json.feeding.last.started_at }}"
        device_class: timestamp
      - name: Lily next feed due
        value_template: >
          {{ (value_json.reminders | selectattr('kind', 'eq', 'feeding') | list | first).due_at }}
        device_class: timestamp
      - name: Lily wet diapers 24h
        value_template: "{{ value_json.last_24h.wet }}"
```

**E-ink displays (TRMNL and similar):** most poll a URL from the
vendor's cloud, which would require LilyPad to be reachable from the
internet. Prefer a self-hosted/local server for the display, or relay the
data from something already on your network (e.g. Home Assistant reading
`/api/dash`). See [PRIVACY_AND_SECURITY.md](PRIVACY_AND_SECURITY.md)
before exposing anything.

**Live events:** `GET /api/events` is a Server-Sent Events stream that
emits `alert` (with the alert JSON) and `changed` (someone logged or edited
something) events, plus a keep-alive comment every 20 seconds.

**Snooze, acknowledge or log a diaper from a script:**

```sh
curl -X POST "http://192.168.1.20:8080/api/reminders/1/snooze?minutes=15"
curl -X POST "http://192.168.1.20:8080/api/alerts/42/ack"
curl -X POST "http://192.168.1.20:8080/api/diapers?kind=wet"
```

For `/api/diapers`, `kind` is `wet`, `dirty`, `mixed` (wet + dirty) or
`dry`. The reply is the new entry, e.g. `{"ok": true, "id": 57, "kind":
"wet", "kind_label": "Wet", "occurred_at": "2026-10-28T20:51:00Z"}`. It
works if the dash shows its buttons or not. For example, a Home Assistant
automation can call it from a button next to the changing table.

---

## 4. Dash LED clocks

Admin → Settings → **Dash clocks** sends a short scrolling message
straight to one or more [Dash](https://github.com/loganrf/dash) 32×8 LED
clocks whenever a reminder or alert fires. No Home Assistant needed.

| Setting | Default | Meaning |
|---|---|---|
| Clock addresses | | One per line, up to 8: a clock (`http://192.168.1.63`, `http://dash-a1b2c3.local`) or a Dash server (`http://dash.home.arpa:8080`), which forwards to every clock it manages |
| Message | `Check {baby}` | `{baby}` is the baby's name, `{title}` the notification title. Printable ASCII; other characters show as `?` |
| Color | `#FF4080` | Text color |
| Show for | 10 minutes | 1–30 minutes, or 0 to keep scrolling until someone presses the clock's middle button |
| Skip the "window open" heads-up | on | Send only due, overdue and vital-sign alerts |
| Extra headers | | For a Dash server behind authentication, e.g. `Authorization: Basic …` |

LilyPad POSTs to `<address>/api/notify` on each:

```json
{"text": "Check Lily", "color": "#FF4080", "font": "small", "repeat": 224, "scroll_speed": 25}
```

Dash keeps text that fits on the display up for `duration` seconds, but
scrolls longer text a fixed number of times. LilyPad measures the message
in the clock's small font and works out the number of passes: "Check Lily"
is 35 px wide, so one pass is 35 + 32 = 67 px, 2.68 s at 25 px/s, and 10
minutes is 224 passes. The firmware allows at most 255, so long display
times scroll more slowly instead.

**Good to know**

- The clock queues up to 8 messages. Each alert adds its own, so a due
  alert followed by an overdue nag can keep it busy for 20 minutes.
- Acknowledging or snoozing in LilyPad doesn't clear the clock (Dash has no
  API to cancel a message). Press the clock's middle button.
- Delivery to each clock is retried once; the result shows under
  Admin → Notifications as `clock`.
- The text measurement uses a copy of the Dash firmware's small-font width
  table (`app/dash_clock.py`). If the firmware font changes, update it there.
