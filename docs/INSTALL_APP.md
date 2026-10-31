# Installing LilyPad as an app

LilyPad is an installable web app (a *Progressive Web App*). Installed on a
phone, tablet or computer, it:

- has its own icon on the home screen, app drawer, dock or Start menu;
- opens in its own window, without browser address bars or tabs;
- offers shortcuts when you long-press (or right-click) the icon:
  **Log a feed**, **Log a diaper**, **Timeline** and **Dash**;
- shows a **"Can't reach LilyPad"** screen, instead of a browser error,
  when you're away from home Wi-Fi or the server is off. That screen
  reconnects by itself as soon as LilyPad is reachable again;
- stays up to date by itself. After you upgrade LilyPad, the app loads
  the new version the next time it opens.

The dash (`/dash`) installs separately as **LilyPad Dash**, a full-screen
app that opens straight to the dash. It suits a wall tablet.

---

## 1. What works over HTTP and what needs HTTPS

Browsers only allow full installation and the offline screen on secure
addresses: `https://…`, or `http://localhost` on the computer running
LilyPad. A plain address such as `http://192.168.1.20:8080` limits what you
get:

| Device | Plain `http://` address | `https://` address |
|---|---|---|
| iPhone / iPad (Safari) | **Add to Home Screen** works and opens full screen. No offline screen | Same, plus the offline screen |
| Android (Chrome, Edge, Samsung Internet, Firefox) | Home-screen shortcut only, which may open as a normal browser tab | Full install: own window, app drawer, shortcuts, offline screen |
| Computer (Chrome, Edge) | Not installable | Install from the address bar |

On an iPhone-only household, plain HTTP is fine. For Android and computers,
set up HTTPS on your home network first (§3). This does **not** mean
opening LilyPad to the internet.

---

## 2. Installing

### iPhone and iPad

1. Open LilyPad in Safari.
2. Tap **Share** (the square with an arrow), then **Add to Home Screen**,
   then **Add**.

Until LilyPad is installed, a reminder of these steps appears at the
bottom of every page.

### Android

1. Open LilyPad in Chrome (or Edge, Samsung Internet or Firefox).
2. Tap **Install LilyPad as an app** at the bottom of any page. Or open the
   browser menu (**⋮**) and choose **Install app** (sometimes **Add to Home
   screen**).

Long-press the new icon for the logging shortcuts.

### Computers

In Chrome or Edge, click the install icon at the right of the address bar,
or the **Install LilyPad as an app** button at the bottom of any page.

### The dash on a wall tablet

Open `/dash` on the tablet and install from there, as above. It installs
as **LilyPad Dash** and opens full screen on the dash. Pair it with a kiosk
mode (Guided Access on iPad, screen pinning or *Fully Kiosk Browser* on
Android) to keep it in front. See [DASH.md](DASH.md) for more.

---

## 3. HTTPS on your home network

None of these options need a port forward on your router.

**Tailscale (easiest, and gives you safe access away from home).**
Install Tailscale on the computer running LilyPad and on each phone. In the
Tailscale admin console, turn on MagicDNS and HTTPS certificates. Then, on
the LilyPad computer:

```sh
tailscale serve --bg 8080
```

LilyPad is now at `https://<computer-name>.<your-tailnet>.ts.net`, with a
real certificate, and only devices in your tailnet can reach it.

**Your own domain plus a reverse proxy.** Point a name such as
`lilypad.home.example.com` at the LilyPad computer's local IP address.
Caddy, Traefik or Nginx Proxy Manager can then get a free Let's Encrypt
certificate with the DNS challenge, which needs no open ports.

**A private certificate authority.** Caddy's `tls internal` works without
a domain, but every phone has to install and trust Caddy's root
certificate. On iPhone that's under Settings → General → About →
Certificate Trust Settings. This is the most fiddly option.

Then, in `.env`, list the new name (keep the IP address if you still use
it) and restart LilyPad with `docker compose up -d`:

```env
LILYPAD_ALLOWED_HOSTS=lilypad.home.example.com,192.168.1.20
```

If saving an entry over HTTPS fails with *"Blocked: this form was
submitted from a different site"*, your proxy rewrites the `Host` header.
Also set `LILYPAD_ALLOWED_ORIGINS=https://lilypad.home.example.com`. For
the other proxy settings see
[PRIVACY_AND_SECURITY.md](PRIVACY_AND_SECURITY.md#3-recommended-deployments).

Finally, set **Admin → Settings → Base URL** to the HTTPS address, so
notification links open the address the app was installed from.

> **Switching addresses?** An installed app belongs to the address it was
> installed from. After moving from `http://192.168.1.20:8080` to an HTTPS
> address, remove the old icon and install again from the new address. The
> theme and "Logging as" choices are kept per address, so set them again.

### Last resort for a dedicated Android dash tablet

If HTTPS isn't an option, Chrome on a tablet used only as the dash can
treat LilyPad's plain address as secure. Open
`chrome://flags/#unsafely-treat-insecure-origin-as-secure`, enter
`http://192.168.1.20:8080` (your address), enable it, and relaunch Chrome.
This also lets the dash keep the screen awake. Do this only on a device
dedicated to the dash.

---

## 4. What the app keeps on the device

Only LilyPad's styles, scripts and logo, plus the generic "Can't reach
LilyPad" screen. Pages, entries, notes and settings are **never** stored
by the app: every screen is loaded fresh from your server, and nothing can
be viewed or logged offline. Uninstalling the app, or clearing the
browser's data for LilyPad, removes these files.

## 5. Troubleshooting

| Problem | Fix |
|---|---|
| No install option on Android or a computer | Check the address starts with `https://` (§1). |
| The **Install** button doesn't appear | The browser decides when to offer it, and it's hidden once installed. Use the browser menu instead. |
| "Can't reach LilyPad" while at home | Is the server running (`docker compose ps`)? Is the phone on home Wi-Fi rather than mobile data, or connected to the VPN? |
| The icon or name didn't change after an update | Icons are copied at install time. Remove the app and install it again. |
