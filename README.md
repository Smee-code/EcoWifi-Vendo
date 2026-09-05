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
├── templates/            portal, admin dashboard, login, first-run setup
├── hostapd.conf          Template: broadcasts the open SSID
├── dnsmasq.conf          Template: DHCP + DNS hijack for the portal
├── nginx.conf            Template: portal on HTTP, admin forced to HTTPS
├── setup_nftables.sh     Template: default-drop firewall + portal redirect
├── systemd/              Units for the app and the firewall ruleset
└── install.sh            Interactive installer — run this on the gateway
```

## Installing

Run on the gateway machine itself, as root:

```
git clone https://github.com/Smee-code/EcoWifi-Vendo.git
cd EcoWifi-Vendo
sudo bash install.sh
```

It asks for your interface names, the SSID to broadcast, the AP IP address,
the app port, and an admin username and password. It then installs
hostapd, dnsmasq, nftables and nginx, generates a self-signed certificate,
fills in every config template, and starts everything on boot.

Afterwards:

- Customer portal — `http://<AP_IP>/`
- Admin dashboard — `https://<AP_IP>/admin`

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
