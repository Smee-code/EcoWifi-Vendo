# EcoWifi Vendo — piso-wifi gateway software

Captive-portal gateway software for a vending-machine-style WiFi hotspot,
in the manner of Philippine piso-wifi systems (LPB Piso WiFi, PisoFi).
Customers connect to an open SSID, pay at the machine, and get metered
internet access.

**The payment hardware is up to you.** This software does not care whether
customers pay with plastic bottles, coins, tokens or a card reader. It
exposes one HTTP endpoint, `POST /grant`, which your hardware calls when it
has validated a payment. Everything else — what a payment is called, what
it is worth, and what the portal says to customers — is configured by the
operator at first run.

Runs on any Linux box that can host an access point. It was built for an
Orange Pi with a USB WiFi adapter and a USB-Ethernet uplink.

## What is in this folder

```
├── main.py               FastAPI app: portal, /claim, /grant, admin API
├── database.py           SQLite: sessions, payment queue, transactions,
│                          vouchers, rates, settings, admin credentials
├── auth.py               Password hashing, sessions, brute-force lockout
├── network_service.py    nftables control + MAC resolution via ARP
├── system_monitor.py     Gateway health checks for the dashboard
├── session_worker.py     Background thread: revokes expired sessions
├── branding.py           Logo processing: resize + background removal
├── clock.py              Absorbs NTP jumps on a board with no RTC
├── templates/            portal, admin dashboard, login, first-run setup
├── assets/               Shipped default logos
├── hostapd.conf          Template: broadcasts the open SSID
├── dnsmasq.conf          Template: DHCP + DNS hijack for the portal
├── nginx.conf            Template: portal on HTTP, admin forced to HTTPS
├── doctor.sh             Read-only diagnostic for a deployed gateway
├── setup_ap_interface.sh Template: brings the AP interface up at boot
├── setup_nftables.sh     Template: default-drop firewall + portal redirect
├── systemd/              Units for the app and the firewall ruleset
└── install.sh            Interactive installer — run this on the gateway
```

## Installing

### Before you start

You need, plugged into the board:

- a **USB WiFi adapter that supports AP mode** — this is the part that most
  often does not work. RT5370, MT7610U and MT7612U are known good. Check
  with `iw list | grep -A10 "Supported interface modes"` and look for `AP`.
- a **USB-Ethernet adapter** connected to your existing router, for the
  uplink.

The board's onboard WiFi is usually not usable as an access point, which
is why a separate adapter is specified.

### First contact with the board

If you have only just flashed Armbian:

1. Connect the board to your router by Ethernet and power it on.
2. Find its address from your router's client list, or:
   ```
   ping armbian.local
   ```
3. SSH in. Armbian's first login is `root` with password `1234`, and it
   forces you to set a new password and create a user on first boot:
   ```
   ssh root@<board-ip>
   ```
4. Get the time right before anything else. These boards have no
   battery-backed clock, so a fresh one can boot years out of date:
   ```
   timedatectl set-ntp true
   timedatectl        # check "System clock synchronized: yes"
   ```

### Install

Run on the board itself, as root:

```
sudo apt update && sudo apt install -y git
git clone https://github.com/Smee-code/EcoWifi-Vendo.git
cd EcoWifi-Vendo
sudo bash install.sh
```

It asks for your interface names (it checks they exist and lists the real
ones if you mistype), the SSID to broadcast, your two-letter WiFi country
code, the AP IP address, the app port, and an admin username and password.
The password is hashed straight into the database and never written to
disk in plain text. It then installs hostapd,
dnsmasq, nftables and nginx, generates a self-signed certificate, fills in
every config template, and starts everything on boot.

It also clears the three things that most often break this on a fresh
Debian-based image, none of which announce themselves clearly:

- **hostapd ships masked** on Debian, so enabling it silently fails
- **hostapd is not told where its config is** (`DAEMON_CONF` in
  `/etc/default/hostapd`), so it starts with no configuration and exits
- **systemd-resolved holds port 53**, so dnsmasq cannot start
- **rfkill soft-blocks the radio**, so hostapd exits without saying why
- **NetworkManager claims the WiFi adapter** and fights hostapd for it
- **the AP address does not survive a reboot** unless something reassigns
  it; a `ecowifi-ap` unit waits for the USB adapter to appear, takes it
  away from NetworkManager, and brings it up with its address

At the end it checks each service is genuinely running and tells you which
`journalctl` command to read if one is not.

Afterwards:

- Customer portal — `http://<AP_IP>/`
- Admin dashboard — `https://<AP_IP>/admin`

### Check it is really working

```
# every service up?
systemctl is-active hostapd dnsmasq nginx ecowifi-nftables ecowifi-app

# is the SSID on the air?
iw dev

# firewall ruleset applied?
sudo nft list ruleset | head -40

# clock sane? (sessions depend on it)
timedatectl
```

If anything is wrong, run the diagnostic. It changes nothing and names
the specific cause rather than a generic failure:



Then connect a phone to the SSID. It should show the sign-in notification
and land on the portal. If it connects but no portal appears, check
dnsmasq is answering DNS and the nftables redirect exists.

**The one thing to verify first if nothing works:** every customer must
appear with their own IP, not the proxy's. On the board:

```
curl -s http://127.0.0.1/status | grep -o '"ip":"[^"]*"'
```

From the Pi itself that correctly reads `127.0.0.1`. From a connected
phone it must show the phone's DHCP address. If a phone shows
`127.0.0.1`, nginx is not passing the real client address, MAC resolution
cannot work, and nothing else will either.

### Timekeeping

These boards have no real-time clock. `install.sh` sets up `fake-hwclock`
(so the board boots near the right time) and enables NTP (so it corrects
once the uplink is up).

The app handles the correction itself: sessions are stored as absolute
expiry times, so an NTP step of hours or days would otherwise cut every
paying customer off at once, or grant them days of free access. The worker
compares wall-clock movement against a monotonic clock, and when they
disagree it shifts every stored expiry by the same amount. A customer with
twenty minutes left keeps twenty minutes left. The dashboard reports any
corrections it absorbed.

## First run

Two setup steps must be completed before the machine can grant anything.

1. **Admin credentials.** If you did not set them during install, open
   `/admin/setup`. It asks for a one-time token that is printed to the
   log — `journalctl -u ecowifi-app | grep -A2 "NO ADMIN"`. The token
   exists because the SSID is open: without it, the first customer to
   reach the setup page could claim the machine.

2. **Payment rates.** No rate is shipped. Define your own payment trigger
   and what it earns, e.g. `coin` = 20 minutes, or `bottle` = 30 minutes.
   Until at least one rate exists, `/claim` and `/grant` return 503 and the
   portal tells customers the machine is not set up.

## Connecting your payment hardware

Your hardware needs to make one HTTP call. When it has validated a payment:

```
POST /grant
{"payment_method": "coin"}
```

`payment_method` names one of your configured rates. It may be omitted when
only one rate exists. The oldest waiting customer is credited.

Optionally, report health so the dashboard can show it:

```
POST /device/heartbeat
{"firmware": "coinbox-v2", "hopper_coins": 412, "accepting": true}
```

Every field is optional and freeform. Two keys carry meaning: `accepting`
(false stops the portal asking customers to pay) and `message` (shown to
customers while that is true). Everything else is displayed on the
dashboard as-is, so any hardware can report whatever it has.

## How a session works

1. Customer connects to the SSID; dnsmasq leases an IP and hijacks DNS
2. nftables redirects their web traffic to the portal
3. They tap the pay button — `POST /claim` queues their MAC
4. They pay at the machine; your hardware validates it
5. Your hardware calls `POST /grant`
6. The app credits time, adds their MAC to the nftables allow set, and logs
   the transaction
7. `session_worker.py` revokes access when the time runs out

Session time is stored as an expiry timestamp rather than a counter, so the
countdown stays correct across restarts and supports pause/resume.

## Branding

The portal shows an operator logo in two forms: a wide lockup centred
across the top on tablets and desktops, and a square badge in the header
on phones. Both are uploaded from the dashboard under **Logo**.

Uploads are processed once, at upload time. They are resized for a
captive portal, and their white background is removed by flooding inward
from the edges, so white *inside* the artwork survives. A 1.4 MB export
on a white card becomes a ~90 KB transparent PNG. Images that already
have real transparency are left alone.

Logos live in the database, so a backup captures them along with
everything else.

## Admin dashboard

System status (payment device liveness, whatever it reports, gateway
services, disk, and whether the firewall agrees with the database),
sessions with pause/resume/revoke, a permanent MAC blocklist, vouchers,
rates, portal wording, backup and restore, and credential changes.

## Security notes

- The admin dashboard is forced onto HTTPS with a self-signed certificate.
  Your browser warns once; click through. It defeats passive sniffing on
  the open WiFi, which is the actual threat. Replace the certificate in
  `/etc/ssl/ecowifi/` if this ever gets a real domain name.
- The customer portal stays on plain HTTP deliberately. Phones probe a
  known URL to detect a captive portal, and that probe fails behind a
  self-signed certificate — customers would never see the sign-in page.
- Admin login and voucher redemption are both rate-limited per IP.
- `POST /grant` is **not** authenticated. Anyone who can reach the gateway
  can call it and credit the oldest waiting claim. If your hardware can
  hold a secret, put it behind a shared token before deploying somewhere
  that matters.

## What is not here

- **Payment hardware firmware** — a separate codebase, whatever your
  hardware is.
- **Log rotation** — the SQLite database and the journal grow unbounded.
  Add rotation before long-term unattended deployment.

## Attribution

The `hostapd.conf` / `dnsmasq.conf` patterns were adapted from
[Splines/raspi-captive-portal](https://github.com/Splines/raspi-captive-portal)
(MIT licensed), rewritten for nftables, a USB-Ethernet WAN and a USB WiFi
AP, and a FastAPI portal instead of the original Node.js server.
