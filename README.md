# EcoWifi Vendo -- server-side software

This is the complete Orange Pi gateway software matching
`EcoWiFi_Full_System_Components.docx`: FastAPI app, local nftables
firewall control, and the OS-level hostapd/dnsmasq/nftables setup, all
wired together with systemd so it starts automatically on boot.

**This does not include the ESP32 firmware.** Per the spec document,
that's a separate codebase running on the microcontroller side (bottle
validation, motor control, sending `POST /grant`). This repo is the
Orange Pi side only.

## What's in this folder

```
ecowifi-vendo-fastapi/
├── main.py                  FastAPI app: portal, /claim, /grant, admin routes
├── database.py               SQLite: sessions, transactions, vouchers, rates
├── network_service.py        Local nftables control + MAC resolution via ARP
├── session_worker.py         Background thread: expires sessions, revokes access
├── requirements.txt
├── .env.example
├── hostapd.conf               Template: broadcasts the open "EcoWifi" SSID
├── dnsmasq.conf                Template: DHCP + DNS-hijack for the captive portal
├── setup_nftables.sh           Template: default-drop firewall + portal redirect
├── systemd/
│   ├── ecowifi-app.service      Runs the FastAPI app via uvicorn
│   └── ecowifi-nftables.service  Applies the firewall ruleset at boot
└── install.sh                  Ties everything together -- run this on the Pi
```

## Important: this only runs on Linux

`network_service.py` calls `nft` and `ip neigh` directly. These don't
exist on Windows. Everything here must be installed and run **on the
Orange Pi itself** (or a Linux VM) -- not your Windows PC.

## Installing (on the Orange Pi)

1. Copy this whole folder onto the Orange Pi (via `git`, `scp`, or a
   USB drive).
2. Plug in your USB WiFi adapter and USB-LAN adapter, then run
   `ip link show` to find their interface names (something like
   `wlx...` for WiFi, `enx...` for LAN).
3. Run the installer:

   ```
   cd ecowifi-vendo-fastapi
   sudo bash install.sh
   ```

   It will ask for:
   - Your AP interface name (USB WiFi adapter)
   - Your WAN interface name (USB-LAN adapter)
   - The static IP to give the AP interface (e.g. `192.168.50.1`)
   - The port to run the FastAPI app on (e.g. `8000`)

   Then it installs `hostapd`, `dnsmasq`, `nftables`, sets up a Python
   virtual environment, fills in all the config file placeholders,
   installs the systemd services, and starts everything.

4. Check everything came up:

   ```
   systemctl status hostapd dnsmasq ecowifi-nftables ecowifi-app
   ```

5. From a phone, connect to the "EcoWifi" WiFi network. You should be
   redirected to the captive portal automatically.

## The claim -> grant flow (recap)

1. Client connects to "EcoWifi", gets redirected to the portal
2. Taps "Claim WiFi" -> `POST /claim` -- this app resolves their MAC
   from the ARP table and marks it `pending`
3. They insert a bottle. The ESP32 validates it (metal/weight/size)
4. On success, the ESP32 calls `POST /grant` on this app
5. This app grants the oldest pending claim: credits time based on the
   `bottle` rate, adds the MAC to nftables' `granted_macs` set, logs a
   transaction
6. `session_worker.py` checks once a minute and revokes access when
   time runs out

## Attribution

The `hostapd.conf` / `dnsmasq.conf` patterns were adapted from
[Splines/raspi-captive-portal](https://github.com/Splines/raspi-captive-portal)
(MIT licensed), rewritten for nftables, USB-LAN WAN + USB WiFi AP, and
a FastAPI portal instead of the original's Node.js server.

## What's still not here

- **HTTPS** -- the admin password crosses the LAN in the clear, since
  the portal is served over plain HTTP. On an open SSID anyone running a
  packet capture can read it. Putting nginx in front with a self-signed
  certificate (spec section 3.3 already calls for nginx) would close this.
- **ESP32 firmware** -- separate codebase, not part of this repo.
- **Log rotation / monitoring** -- the SQLite database and systemd
  journal will grow unbounded over time; add rotation before long-term
  unattended deployment.
