# Reminders & notifications

## How reminders work

Each reminder watches one kind of entry and counts its interval from the
**most recent** one:

| Reminder type | Counts from |
|---|---|
| Feeding | the start of the last feed (or its end, if you choose) |
| Any diaper change / Wet diaper / Bowel movement | the last matching diaper entry |
| Vitals check | the last vitals check (or the last reading of one parameter) |
| Custom routine | the last time someone tapped **Done** for it on the home screen |

```
last feed ─────────────── window opens ──── due ── repeat ── repeat
 (logged)       on track     (heads-up)            (overdue)
```

- **Every**: the interval, e.g. 3 h for feeds.
- **Heads-up window**: notify this long before it's due ("feeding window
  open"). 0 skips it.
- **Repeat while overdue**: re-notify every N minutes until it's logged,
  up to **Maximum repeats**.
- **Active hours**: outside these times nothing is sent (the status still
  shows on screen); anything that came due is sent when the window opens.
  Wraps past midnight, e.g. `07:00–22:00` or `22:00–06:00`.
- **Send to**: ntfy (phones) and/or Dash (the built-in `/dash` page, the
  dash webhook and Dash LED clocks).
- **Priority**: ntfy priority when due. The heads-up goes one step lower
  and overdue repeats one step higher, so phones can ring louder as it
  gets later.

Logging the matching entry resets the cycle immediately. While a feed timer
is running the feeding reminder pauses; a timer left running for more than
two hours is treated as finished so a forgotten **Stop** doesn't silence
reminders indefinitely. **Snooze** (on the home screen, the dash, or the
ntfy notification) pauses notifications for that cycle; when it ends you
get one more reminder if it's still outstanding. If LilyPad was offline
through several stages, it sends only the current one when it comes back.

Every enabled reminder appears on the home screen and the dash whether or
not any notification channel is configured.

### Vital-sign alerts

Each vital parameter (Admin → Vitals) has an optional **expected range** and
optional **alert limits**:

- outside the expected range → shown as **watch** (amber), no notification;
- **at or beyond** an alert limit → shown as **alert** (red) and, if
  *Send an immediate notification* is on, sent at once to ntfy (priority 5)
  and the dash, where it stays until someone taps **Acknowledge**.

Each reading stores the limits that applied when it was taken, so changing
limits later does not rewrite history.

## Setting up ntfy

[ntfy](https://ntfy.sh) delivers push notifications to Android, iOS and
desktop browsers.

1. Install the ntfy app on each parent's phone.
2. In LilyPad: Admin → Settings → ntfy notifications.
   - Tick **Send reminders and alerts to ntfy**.
   - Server: `https://ntfy.sh` or your own.
   - Topic: press **Generate one** for a long random topic.
   - Optional: an access token (`tk_…`) or username/password if your server
     requires them.
   - Choose a detail level (see below), and save.
3. In the ntfy app, subscribe to the same server + topic on every phone.
4. Press **Send test notification** (Settings page or Admin →
   Notifications). The delivery log shows what each channel returned.

### Detail levels

| Level | What's in the message |
|---|---|
| Minimal | Only that a routine is due / a vital is out of range |
| Standard (default) | Routine name, due time, last-logged time, vital value; no baby name |
| Detailed | Also the baby's name, feed side/duration/quality and vitals notes |

See [PRIVACY_AND_SECURITY.md](PRIVACY_AND_SECURITY.md#5-notifications-leave-your-network)
for why this matters on the public ntfy.sh server.

### Tap-to-open and Snooze buttons

Set **Base URL** (Settings → Open access) to the address your phones use for
LilyPad, e.g. `http://192.168.1.20:8080`. Notifications then open LilyPad
when tapped and include **Snooze 15 min** (reminders) or **Acknowledge**
(vital alerts) buttons. The buttons make your phone contact LilyPad
directly, so they work on home Wi-Fi or your VPN.

### Self-hosting ntfy

`docker compose --profile ntfy up -d` starts a private ntfy on port 8081
that denies anonymous access. Create a user and token:

```sh
docker compose exec ntfy ntfy user add --role=admin parent
docker compose exec ntfy ntfy token add parent
```

Then use `http://<host>:8081` as the server and the token in LilyPad, and
log in with the same user in the ntfy apps. For iOS push through a
self-hosted server, see ntfy's documentation on `upstream-base-url`.
