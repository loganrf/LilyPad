# Privacy & security

LilyPad makes a deliberate trade-off: **day-to-day logging needs no
password.** At 3 a.m., with a baby in one arm, the difference between
"tap Wet" and "unlock, type a password, then tap Wet" is the difference
between a complete log and a log full of gaps. Gaps make it harder to
spot patterns and harder to answer a pediatrician's questions.

That convenience has real consequences. This document lays them out so
you can decide how and where to run LilyPad, and what to change if your
situation differs from the default assumptions.

> **The one-sentence version:** treat LilyPad like a whiteboard on your
> fridge. Anyone who can reach it can read the recent page and write on
> it, so keep it on your home network or a private VPN, and never expose
> it directly to the internet.

---

## 1. What is open and what is protected

| Area | Password? | Who can use it |
|---|---|---|
| Home screen, logging forms (feeds, diapers, vitals, notes, routines) | No | Anyone who can reach LilyPad |
| Timeline of the **open window** (last 48 hours by default) | No | Anyone who can reach LilyPad |
| Editing or deleting entries **inside** the open window | No | Anyone who can reach LilyPad |
| Undoing a delete within 30 minutes | No | Anyone who can reach LilyPad |
| `/dash` display and its `/api/dash` JSON feed | No | Anyone who can reach LilyPad |
| `/api/events` live stream (new alerts, "something changed") | No | Anyone who can reach LilyPad |
| Snoozing reminders, acknowledging alerts | No | Anyone who can reach LilyPad, and the ntfy app's action buttons |
| "Logging as …" name picker | No | Stored in a cookie on that device; a label, not a login |
| Theme (light/dark/night), dash sound toggle | No | Stored only in that browser |
| **Anything older than the open window**: viewing, editing, deleting | **Yes** | Admin |
| **History search** across all time | **Yes** | Admin |
| **Reports**, including the pediatrician summary and charts | **Yes** | Admin |
| **Exports**: CSV, JSON backup, raw database download | **Yes** | Admin |
| **Settings**: baby details, time zone, units, caregivers, open-window length, base URL | **Yes** | Admin |
| **Notification settings**: ntfy server/topic/credentials, detail level, dash webhook | **Yes** | Admin |
| **Reminder schedules** (intervals, windows, repeats, active hours, channels) | **Yes** | Admin |
| **Vital parameters** and their expected/alert limits | **Yes** | Admin |
| **Trash** (restore older deletions, empty trash), notification delivery log | **Yes** | Admin |

The home screen and dash always show the **most recent** feed, diaper and
vital reading, even if that entry is older than the open window, because
"when did she last eat?" is the question they exist to answer.

### The open window

The open window (Admin → Settings → Open access) controls how far back the
password-free pages reach. It applies to both reading and editing.

- **Default: 48 hours.** Enough to answer "when was the last poop?" and to
  fix yesterday's mistakes without the password.
- **Range: 24 hours to 30 days.** A longer window is more convenient and
  exposes more history to anyone on your network. A shorter window means
  more trips to the admin area to correct entries.

---

## 2. What the open design exposes

Assume that **anyone who can load LilyPad in a browser** can:

1. **Read** the last 48 hours (or whatever you set) of feeds, diapers,
   vitals, notes and routines, plus the latest of each kind of entry and
   the last 24 hours of totals. Notes are free text and may contain
   anything you typed into them.
2. **Add false entries**, e.g. log a feed that never happened, which would
   also reset that reminder's countdown.
3. **Change or delete recent entries.** Deletes are soft: an admin can
   restore anything from the trash for 30 days. **Edits overwrite the
   previous values; LilyPad does not keep an edit history.**
4. **Snooze reminders** and **acknowledge alerts**, silencing them for
   everyone.
5. **Learn your baby's name and age**, which appear in the page header.

They **cannot**, without the admin password: read older history, run
reports, download exports or backups, change any setting, see stored
ntfy/webhook credentials, change reminder schedules or vital limits, or
empty the trash.

### Who "anyone who can reach it" usually means

| If LilyPad is reachable from… | …then "anyone" includes |
|---|---|
| Your home Wi-Fi only (recommended) | Household members, guests on your Wi-Fi, and any compromised device on your network (a cheap smart plug, an old laptop, a camera with outdated firmware) |
| A VPN such as WireGuard or Tailscale | Only devices you have enrolled in the VPN |
| The public internet, without extra protection | **Everyone, including automated scanners.** Don't do this. |

---

## 3. Recommended deployments

### Good: home network only

The default `docker-compose.yml` publishes port 8080 on the host. That is
reachable from your LAN but not from the internet unless you
port-forward it on your router. **Don't port-forward it.**

Optionally, lock LilyPad to the names you actually use (this blocks
DNS-rebinding attacks, see §6):

```env
LILYPAD_ALLOWED_HOSTS=lilypad.home.arpa,192.168.1.20,localhost
```

### Better: private VPN for access away from home

To check LilyPad from the pediatrician's office or a grandparent's house,
use a VPN (Tailscale, WireGuard, ZeroTier, your router's VPN server)
instead of opening a port. Only enrolled devices can reach LilyPad, and
traffic is encrypted end to end.

### If you must publish it: put real authentication in front

If LilyPad has to be reachable from the internet, put it behind a reverse
proxy that enforces authentication **for every page**, such as
Authelia, Authentik, oauth2-proxy, Cloudflare Access, or at minimum HTTP
basic auth over HTTPS. That restores a login for the open pages (most
password managers and browsers can remember it so it stays low-friction),
and LilyPad's own admin password still protects the admin area behind it.

When running behind a proxy:

- Terminate TLS at the proxy (HTTPS), so cookies are marked `Secure`
  automatically. `LILYPAD_COOKIE_SECURE=true` forces it.
- Forward the original `Host` header (Caddy and Traefik do by default; in
  nginx use `proxy_set_header Host $host;`). Otherwise set
  `LILYPAD_ALLOWED_ORIGINS=https://lilypad.example.com`, or form posts are
  rejected by the cross-site check.
- Set `FORWARDED_ALLOW_IPS` to the proxy's IP so login throttling sees the
  real client address rather than throttling everyone together.

---

## 4. The admin password

- **Where it comes from.** If `LILYPAD_ADMIN_PASSWORD` is set, that is the
  password and it can only be changed by editing the environment and
  restarting. Otherwise, on first start LilyPad generates a password
  (e.g. `mint-otter-4821-cloud`), prints it once in the container log, and
  writes it to `INITIAL_ADMIN_PASSWORD.txt` (mode `0600`) in the data
  volume. That file is deleted as soon as you set your own password in
  Admin → Settings.
- **Storage.** Only a salted `scrypt` hash is stored in the database. The
  environment-variable password is compared via its SHA-256 digest and
  never written to disk by LilyPad, but it is visible to anyone who can
  read your compose file or run `docker inspect`.
- **Sessions.** Unlocking sets an HMAC-signed, `HttpOnly`,
  `SameSite=Strict` cookie that expires after 30 minutes of inactivity
  (`LILYPAD_ADMIN_SESSION_MINUTES`). "Lock now" in the yellow banner ends
  it immediately. Changing the password signs out every other session.
- **Guessing.** After 5 wrong attempts from one address, logins from that
  address are locked for 30 seconds, doubling up to 15 minutes. This is an
  in-memory counter that resets when the container restarts.
- **Forgotten password.**
  `docker compose exec lilypad python -m app.cli reset-password` prints a
  new one. Anyone who can run commands on the Docker host can do this, and
  can also read the database directly; host access is full access.

Pick something you won't forget at 3 a.m. but that a visitor couldn't
guess: a few random words works well.

---

## 5. Notifications leave your network

Reminders and vital-sign alerts are sent to the **ntfy server** and
**dash webhook** you configure. Those messages leave LilyPad and may leave
your home.

### ntfy.sh (the public server)

- **Topics are effectively public.** On `ntfy.sh`, anyone who knows (or
  guesses) your topic name can subscribe to it and read every message. Use
  a long random topic. The **Generate one** button in settings makes a
  20-character random topic, or use an access token / account on a
  server with access control.
- The ntfy.sh operator can see message contents, like any service that
  relays your messages.
- For the most privacy, **self-host ntfy**. The compose file includes an
  optional ntfy service (`docker compose --profile ntfy up -d`) that
  denies anonymous access by default.

### Notification detail level

Admin → Settings → *How much detail to put in notifications* controls what
goes into ntfy and webhook messages:

| Level | Example | Leaves your network |
|---|---|---|
| **Minimal** | "LilyPad reminder: A routine is due now. Open LilyPad for details." | Only that *something* is due or that a vital sign is out of range |
| **Standard** (default) | "Feeding due: Due now (6:49 PM). Last feed started 3:49 PM (3h ago)." · "Temperature alert: 100.8 °F" | Routine names, due times, last-logged times, vital values. **No baby name.** |
| **Detailed** | "Lily: feeding due … Breast, left, 20m, good." | Everything above, plus the baby's name, feed side/duration/quality and vitals notes |

The built-in `/dash` page always shows full detail because it reads
LilyPad directly; it never goes through ntfy or the webhook.

### Action buttons

With a **Base URL** set, ntfy notifications get "Open LilyPad" and
"Snooze 15 min" (or "Acknowledge") buttons. The snooze/acknowledge
buttons make your phone send a request straight to LilyPad, so they only
work where your phone can reach LilyPad (home Wi-Fi or VPN). The base URL
itself (e.g. `http://192.168.1.20:8080`) is included in each notification.

### Dash webhook

If you enable the dash webhook, every notification is POSTed to the URL
you give (see [DASH.md](DASH.md) for the payload). If that URL is a cloud
service, the same detail-level rules apply, and that service holds the
data under its own privacy policy. Extra headers you configure (e.g. an
API token) are stored in the database and never shown again in the UI.

---

## 6. Built-in protections

These are on by default and need no configuration:

- **Cross-site form protection.** Because logging needs no password, a
  malicious web page you visit could otherwise try to submit forms to
  LilyPad from your browser. LilyPad rejects any form submission whose
  `Origin`/`Referer` is a different site. (Requests with no `Origin`, such
  as the ntfy app's action buttons or `curl`, are allowed; they can't
  ride on a browser session.)
- **CSRF tokens** on every admin form, in addition to the `SameSite=Strict`
  admin cookie.
- **DNS-rebinding protection** when `LILYPAD_ALLOWED_HOSTS` is set: requests
  for unrecognised host names are refused, so a malicious site can't
  re-point its own domain at your LilyPad.
- **Strict Content-Security-Policy.** No inline or third-party scripts; the
  app loads nothing from outside your server. No analytics, fonts, CDNs or
  trackers.
- **No framing** (clickjacking protection) except `/dash`, which may be
  embedded in dashboards (`LILYPAD_DASH_FRAME_ANCESTORS` to restrict it).
- **`noindex`** headers and `robots.txt` so search engines skip it if it's
  ever exposed by mistake.
- **`Cache-Control: no-store`** on every page, so shared devices and the
  back button don't keep copies of health data.
- **Escaping.** Everything typed by users is HTML-escaped when shown, and
  CSV exports neutralise spreadsheet formulas (a note starting with `=`
  can't execute in Excel).
- **Container hardening.** Runs as an unprivileged user on a read-only
  filesystem with all Linux capabilities dropped and `no-new-privileges`.

---

## 7. Data at rest, backups and exports

- **Everything lives in one SQLite file**, `lilypad.sqlite3`, in the
  `lilypad-data` Docker volume. It is **not encrypted** by LilyPad. Anyone
  with access to the Docker host, its disks or its backups can read it.
  Use full-disk encryption on the host if that matters to you.
- The database also stores the hashed admin password, the session signing
  key, and any ntfy token/password and webhook headers in plain text
  (they have to be usable to send notifications).
- **JSON backup** (Admin → Export) contains all records but **excludes**
  the password hash, signing key and notification credentials.
- **Database download** is an exact copy **including** those secrets.
  Store it like a password.
- **CSV exports** contain whatever range you choose. Once downloaded,
  they're ordinary files on your device: keep them somewhere private.
- **Deleted entries** stay in the trash (still in the database) for 30
  days, then are permanently removed on the next restart. "Empty trash"
  removes them immediately. SQLite may leave fragments of deleted data in
  free pages until the file is vacuumed.
- **Notification log** entries (titles/messages of past alerts) are kept
  for a year.
- **Container logs** contain request lines (paths and query strings, such
  as a history search term; never form contents or passwords),
  notification delivery results, and, on first start only, the generated
  admin password.

---

## 8. Limitations to be aware of

- **No per-person accounts.** "Logging as" is a label anyone can pick.
- **No edit history.** Edits within the open window overwrite the old
  values.
- **Not a medical device.** Example vital limits and stool-colour prompts
  follow common guidance and are not medical advice. Confirm thresholds
  with your pediatrician, and call them (or emergency services) whenever
  you're worried, regardless of what LilyPad shows.
- **Reminders depend on LilyPad running.** If the container or host is
  down, no reminders go out. Don't rely on LilyPad as the only safeguard
  for anything time-critical such as medication.

---

## 9. Quick checklist

- [ ] LilyPad is **not** port-forwarded on your router.
- [ ] Remote access, if any, goes through a VPN or an authenticating proxy.
- [ ] You've replaced the generated admin password with one you'll remember.
- [ ] `LILYPAD_ALLOWED_HOSTS` lists the names you use.
- [ ] Your ntfy topic is long and random, or protected by a token, or self-hosted.
- [ ] The notification detail level matches your comfort.
- [ ] The open window is no longer than you need.
- [ ] You back up the `lilypad-data` volume and keep backups private.
