#!/bin/bash
# EcoWifi Vendo installer -- run this on the Orange Pi itself, as root.
#
#   sudo bash install.sh
#
# This asks for your actual interface names and IPs, fills them into
# hostapd.conf / dnsmasq.conf / setup_nftables.sh / the systemd unit
# files, installs everything, and starts it all on boot.

set -e

if [ "$EUID" -ne 0 ]; then
    echo "Please run this with sudo: sudo bash install.sh"
    exit 1
fi

echo "EcoWifi Vendo installer"
echo "========================"
echo "Run 'ip link show' in another terminal first if you don't know"
echo "your interface names."
echo

# ---------------------------------------------------------------------
# Where does the WiFi come from? Two supported layouts:
#
#   1. A USB WiFi adapter in this board, driven by hostapd. Fewer boxes,
#      but the adapter must support AP mode and many do not.
#
#   2. A separate access point on the end of an Ethernet cable. The Pi
#      never touches a radio; it routes and gates traffic for whatever
#      the AP bridges onto its segment. This is how most piso-wifi
#      deployments are actually built.
#
# The difference matters beyond hostapd: in layout 2 the AP MUST be a
# bridge with its own DHCP disabled. An AP left in router mode NATs its
# clients, so every customer reaches this machine wearing the AP's single
# IP and MAC -- granting one would grant everybody, and the per-device
# model collapses.
# ---------------------------------------------------------------------
echo "How is the customer WiFi provided?"
echo "  1) A USB WiFi adapter in this board (this machine runs hostapd)"
echo "  2) A separate access point connected by Ethernet"
echo
read -p "Choose 1 or 2 [1]: " WIFI_MODE_CHOICE
WIFI_MODE_CHOICE=${WIFI_MODE_CHOICE:-1}

case "$WIFI_MODE_CHOICE" in
    2)
        WIFI_MODE="external"
        echo
        echo "External access point selected. This machine will not run hostapd."
        echo
        echo "IMPORTANT: configure the access point as a BRIDGE / dumb AP:"
        echo "  - set it to Access Point or Bridge mode, NOT router mode"
        echo "  - DISABLE its DHCP server (this machine serves DHCP)"
        echo "  - connect its LAN port to this machine, not its WAN port"
        echo
        echo "If it stays in router mode every customer arrives with the"
        echo "same address and MAC, and granting one grants all of them."
        echo
        ;;
    *)
        WIFI_MODE="onboard"
        ;;
esac

if [ "$WIFI_MODE" = "external" ]; then
    read -p "Interface the access point is plugged into (e.g. eth1): " AP_INTERFACE
else
    read -p "AP interface (USB WiFi adapter, e.g. wlan1): " AP_INTERFACE
fi
read -p "WAN interface (uplink to your router, e.g. eth0): " WAN_INTERFACE
# A typo here is the single most common way this install goes wrong, and
# it fails much later with an error that does not mention the typo.
for iface in "$AP_INTERFACE" "$WAN_INTERFACE"; do
    if ! ip link show "$iface" >/dev/null 2>&1; then
        echo
        echo "Error: no interface named '$iface' on this machine."
        echo "Available interfaces:"
        ip -o link show | awk -F': ' '{print "  " $2}'
        exit 1
    fi
done

# ---------------------------------------------------------------------
# Does this adapter actually support AP mode? Most USB WiFi dongles do
# not. Without this check hostapd fails much later with a message about
# nl80211 that never mentions the real cause, and the operator has no way
# to tell a bad adapter from a bad config.
# ---------------------------------------------------------------------
if [ "$WIFI_MODE" = "onboard" ] && command -v iw >/dev/null 2>&1; then
    AP_PHY=$(iw dev "$AP_INTERFACE" info 2>/dev/null | awk '/wiphy/ {print $2}')
    if [ -n "$AP_PHY" ]; then
        if iw phy "phy$AP_PHY" info 2>/dev/null |
             awk '/Supported interface modes/{f=1;next} /^\t[A-Za-z]/{f=0} f' |
             grep -qw '\* AP'; then
            echo "  $AP_INTERFACE supports AP mode."
        else
            echo
            echo "WARNING: $AP_INTERFACE does not report AP mode support."
            echo "hostapd will almost certainly fail to start with this adapter."
            echo "Check what it can do with:  iw phy phy$AP_PHY info"
            echo
            read -p "Continue anyway? [y/N]: " AP_CONFIRM
            case "$AP_CONFIRM" in
                [yY]*) echo "  continuing at your request" ;;
                *) echo "Stopping. Use an AP-capable adapter (RT5370, MT7610U, MT7612U)."; exit 1 ;;
            esac
        fi
    else
        echo "  (could not identify the wireless phy for $AP_INTERFACE; skipping AP-mode check)"
    fi
else
    echo "  (iw not installed yet; AP-mode support will be checked by hostapd itself)"
fi

if [ "$WIFI_MODE" = "external" ]; then
    # The access point owns the SSID and the regulatory domain; asking
    # here would imply this machine controls them, and it does not.
    WIFI_SSID="(set on the access point)"
    COUNTRY_CODE="XX"
else
read -p "WiFi network name (SSID) to broadcast [EcoWifi]: " WIFI_SSID
WIFI_SSID=${WIFI_SSID:-EcoWifi}
if [ ${#WIFI_SSID} -gt 32 ]; then
    echo "Error: an SSID cannot be longer than 32 characters."
    exit 1
fi

# The regulatory domain decides which channels and power levels are legal.
# hostapd refuses to start if this disagrees with what the adapter allows,
# and the error does not mention the country.
read -p "Two-letter country code for WiFi regulations [PH]: " COUNTRY_CODE
COUNTRY_CODE=${COUNTRY_CODE:-PH}
COUNTRY_CODE=$(echo "$COUNTRY_CODE" | tr '[:lower:]' '[:upper:]')
if ! echo "$COUNTRY_CODE" | grep -Eq '^[A-Z]{2}$'; then
    echo "Error: use a two-letter country code, e.g. PH, US, GB."
    exit 1
fi
fi
read -p "Static IP to assign to the AP interface (e.g. 192.168.50.1): " AP_IP
read -p "Port for the FastAPI portal (e.g. 8000): " PORTAL_PORT

# Set the operator password here so the Orange Pi is never briefly live
# with no admin password. Leave it blank to use the first-run setup page
# instead, which prints a one-time token to the journal.
echo
read -p "Admin username for the dashboard [admin]: " ADMIN_USERNAME
ADMIN_USERNAME=${ADMIN_USERNAME:-admin}
read -s -p "Admin password for the dashboard (blank = set it in a browser later): " ADMIN_PASSWORD
echo
if [ -n "$ADMIN_PASSWORD" ]; then
    read -s -p "Confirm admin password: " ADMIN_PASSWORD_CONFIRM
    echo
    if [ "$ADMIN_PASSWORD" != "$ADMIN_PASSWORD_CONFIRM" ]; then
        echo "Error: the two passwords do not match."
        exit 1
    fi
    if [ ${#ADMIN_PASSWORD} -lt 8 ]; then
        echo "Error: use at least 8 characters."
        exit 1
    fi
fi

# Sanity-check the AP IP and derive the DHCP range from it. The range
# MUST sit on the same /24 as the AP interface, otherwise dnsmasq hands
# out leases that cannot reach this machine -- so derive it here rather
# than hardcoding a subnet in dnsmasq.conf.
if ! echo "$AP_IP" | grep -Eq '^[0-9]{1,3}([.][0-9]{1,3}){3}$'; then
    echo "Error: '$AP_IP' is not a valid IPv4 address (expected e.g. 192.168.50.1)."
    exit 1
fi

AP_SUBNET="${AP_IP%.*}"
AP_HOST_OCTET="${AP_IP##*.}"
DHCP_RANGE_START="$AP_SUBNET.10"
DHCP_RANGE_END="$AP_SUBNET.200"

if [ "$AP_HOST_OCTET" -ge 10 ] && [ "$AP_HOST_OCTET" -le 200 ]; then
    echo "Error: the AP IP's last octet ($AP_HOST_OCTET) falls inside the DHCP"
    echo "range $DHCP_RANGE_START-$DHCP_RANGE_END, so a client could be handed"
    echo "this machine's own address. Use something outside 10-200, e.g. $AP_SUBNET.1"
    exit 1
fi

echo
echo "Using DHCP range $DHCP_RANGE_START - $DHCP_RANGE_END on $AP_SUBNET.0/24"

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Operator-supplied text goes through sed, so anything it treats specially
# has to be escaped first. An SSID containing a slash would otherwise
# corrupt hostapd.conf rather than fail, which is far harder to diagnose.
sed_escape() {
    printf '%s' "$1" | sed -e 's/[&/\\]/\\&/g'
}

AP_INTERFACE_ESC=$(sed_escape "$AP_INTERFACE")
WAN_INTERFACE_ESC=$(sed_escape "$WAN_INTERFACE")
WIFI_SSID_ESC=$(sed_escape "$WIFI_SSID")

echo
echo "Installing system packages..."
apt-get update
# libjpeg/zlib headers are only needed if pip cannot find an aarch64
# wheel for Pillow and falls back to building it. Cheap insurance against
# an install that dies three quarters of the way through.
WIFI_PACKAGES=""
if [ "$WIFI_MODE" = "onboard" ]; then
    WIFI_PACKAGES="hostapd iw rfkill"
fi

# libjpeg/zlib headers are only needed if pip cannot find a wheel for
# Pillow and falls back to building it.
apt-get install -y dnsmasq nftables nginx openssl \
    python3 python3-venv python3-pip \
    libjpeg-dev zlib1g-dev $WIFI_PACKAGES

echo "Stopping hostapd/dnsmasq while we configure (avoids port conflicts)..."
systemctl stop hostapd 2>/dev/null || true
systemctl stop dnsmasq 2>/dev/null || true

# ---------------------------------------------------------------------
# Debian ships hostapd MASKED. Without this, `systemctl enable hostapd`
# fails and the access point never starts, with an error that says
# nothing about masking.
# ---------------------------------------------------------------------
if [ "$WIFI_MODE" = "onboard" ]; then
    echo "Unmasking hostapd (Debian ships it masked)..."
    systemctl unmask hostapd 2>/dev/null || true
fi

# ---------------------------------------------------------------------
# systemd-resolved listens on 127.0.0.53:53. dnsmasq wants port 53 too
# and refuses to start with "address already in use". Turning off just
# the stub listener leaves name resolution working for the Pi itself.
# ---------------------------------------------------------------------
if systemctl is-active --quiet systemd-resolved 2>/dev/null; then
    echo "Freeing port 53 from systemd-resolved (needed by dnsmasq)..."
    mkdir -p /etc/systemd/resolved.conf.d
    cat > /etc/systemd/resolved.conf.d/ecowifi.conf <<'RESOLVED'
# dnsmasq serves DNS for portal clients and needs port 53. The stub
# listener is disabled rather than the whole service, so the gateway can
# still resolve names for itself (apt, NTP).
[Resolve]
DNSStubListener=no
RESOLVED
    systemctl restart systemd-resolved
    # /etc/resolv.conf may point at the stub that no longer listens.
    if [ -L /etc/resolv.conf ]; then
        ln -sf /run/systemd/resolve/resolv.conf /etc/resolv.conf
    fi
fi

# ---------------------------------------------------------------------
# A soft rfkill block stops hostapd with no useful message. Common on
# fresh SBC images.
# ---------------------------------------------------------------------
if command -v rfkill >/dev/null 2>&1; then
    echo "Clearing any rfkill block on the wireless radio..."
    rfkill unblock wifi 2>/dev/null || true
    rfkill unblock all 2>/dev/null || true
fi

# ---------------------------------------------------------------------
# No battery-backed clock on these boards. fake-hwclock saves the time at
# shutdown and restores it at boot, so the machine starts somewhere near
# reality instead of 1970; NTP then corrects it properly. The app also
# detects the correction and shifts session expiries, so customers keep
# the time they paid for either way (see clock.py).
# ---------------------------------------------------------------------
echo "Setting up timekeeping (no RTC on this board)..."
apt-get install -y fake-hwclock >/dev/null 2>&1 || true
systemctl enable --now fake-hwclock 2>/dev/null || true
timedatectl set-ntp true 2>/dev/null || systemctl enable --now systemd-timesyncd 2>/dev/null || true

if [ "$WIFI_MODE" = "onboard" ]; then
echo "Writing /etc/hostapd/hostapd.conf..."
mkdir -p /etc/hostapd
sed -e "s/<AP_INTERFACE>/$AP_INTERFACE_ESC/" \
    -e "s/<WIFI_SSID>/$WIFI_SSID_ESC/" \
    -e "s/<COUNTRY_CODE>/$COUNTRY_CODE/" \
    "$APP_DIR/hostapd.conf" > /etc/hostapd/hostapd.conf

# Debian's hostapd.service runs `hostapd ... $DAEMON_CONF`, read from
# /etc/default/hostapd. Left unset, hostapd starts with no configuration
# and exits immediately -- the single most common reason this kind of
# setup "installs fine" and then has no WiFi.
echo "Pointing hostapd at its config (/etc/default/hostapd)..."
cat > /etc/default/hostapd <<'HOSTAPD_DEFAULT'
DAEMON_CONF="/etc/hostapd/hostapd.conf"
HOSTAPD_DEFAULT
else
    echo "Skipping hostapd: the access point provides the WiFi."
fi

echo "Writing /etc/dnsmasq.conf..."
sed -e "s/<AP_INTERFACE>/$AP_INTERFACE_ESC/" \
    -e "s/<WAN_INTERFACE>/$WAN_INTERFACE_ESC/" \
    -e "s/<AP_IP_ADDRESS>/$AP_IP/" \
    -e "s/<DHCP_RANGE_START>/$DHCP_RANGE_START/" \
    -e "s/<DHCP_RANGE_END>/$DHCP_RANGE_END/" \
    "$APP_DIR/dnsmasq.conf" > /etc/dnsmasq.conf

# ---------------------------------------------------------------------
# The AP interface needs its address on EVERY boot, not just today.
# `ip addr add` lives in memory only, so a reboot would leave the adapter
# with no address, dnsmasq unable to bind, and the portal unreachable.
# A tiny oneshot unit does it properly, and also waits for the USB
# adapter to enumerate and takes it away from NetworkManager first.
# ---------------------------------------------------------------------
echo "Installing the AP interface setup script..."
sed -e "s/<AP_INTERFACE>/$AP_INTERFACE_ESC/" -e "s/<AP_IP_ADDRESS>/$AP_IP/" \
    "$APP_DIR/setup_ap_interface.sh" > /usr/local/sbin/ecowifi-ap.sh
chmod +x /usr/local/sbin/ecowifi-ap.sh

# NetworkManager claims wireless interfaces by default and will fight
# hostapd for this one. Marking it unmanaged in a conf file survives
# reboots, unlike `nmcli device set`.
if [ -d /etc/NetworkManager ]; then
    echo "Telling NetworkManager to leave $AP_INTERFACE alone..."
    mkdir -p /etc/NetworkManager/conf.d
    cat > /etc/NetworkManager/conf.d/99-ecowifi.conf <<NM_CONF
# $AP_INTERFACE is an access point run by hostapd. NetworkManager must
# not touch it, or it will take the radio back and hostapd will fail.
[keyfile]
unmanaged-devices=interface-name:$AP_INTERFACE
NM_CONF
    systemctl reload NetworkManager 2>/dev/null || true
fi

echo "Bringing up $AP_INTERFACE with $AP_IP..."
/usr/local/sbin/ecowifi-ap.sh || echo "  (will retry at boot via ecowifi-ap.service)"

echo "Installing nftables setup script to /usr/local/sbin/..."
sed -e "s/<AP_INTERFACE>/$AP_INTERFACE/" -e "s/<WAN_INTERFACE>/$WAN_INTERFACE/" \
    -e "s/<PORTAL_PORT>/$PORTAL_PORT/" "$APP_DIR/setup_nftables.sh" \
    > /usr/local/sbin/ecowifi-nftables.sh
chmod +x /usr/local/sbin/ecowifi-nftables.sh

echo "Enabling IP forwarding..."
if grep -q "^#net.ipv4.ip_forward=1" /etc/sysctl.conf; then
    sed -i 's/^#net.ipv4.ip_forward=1/net.ipv4.ip_forward=1/' /etc/sysctl.conf
elif ! grep -q "^net.ipv4.ip_forward=1" /etc/sysctl.conf; then
    echo "net.ipv4.ip_forward=1" >> /etc/sysctl.conf
fi
sysctl -p

# ---------------------------------------------------------------------
# Dependencies, without needing a compiler.
#
# PyPI publishes no wheels for 32-bit ARM (armv7l), which is what an
# Orange Pi PC and most older SBCs are. pip therefore falls back to
# BUILDING from source: Pillow needs a C toolchain, uvloop needs to
# compile libuv, and pydantic-core needs Rust. On a board with 1 GB of
# RAM that is half an hour of compiling if the toolchain is present, and
# an outright failure if it is not:
#
#   error: command 'arm-linux-gnueabihf-gcc' failed: No such file
#   configure: error: no acceptable C compiler found in $PATH
#
# Debian ships all of them prebuilt for this architecture, so install
# those and let the venv see them. pip is then only a fallback for
# platforms where the apt packages are missing.
# ---------------------------------------------------------------------
echo "Installing Python dependencies from Debian packages..."
apt-get install -y python3-fastapi python3-uvicorn python3-pydantic \
    python3-dotenv python3-pil 2>/dev/null || \
    echo "  (some apt packages unavailable; will fall back to pip)"

echo "Setting up Python virtual environment..."

# A venv left over from an earlier run may have been created WITHOUT
# --system-site-packages, which seals it off from the apt packages
# installed above. Re-running the installer would then compile again and
# fail again. Recreating is cheap and it is not the operator's job to
# know that, so do it here rather than telling them to delete a folder.
if [ -d "$APP_DIR/venv" ]; then
    if grep -q "include-system-site-packages *= *false" "$APP_DIR/venv/pyvenv.cfg" 2>/dev/null; then
        echo "  removing an existing venv that cannot see system packages"
        rm -rf "$APP_DIR/venv"
    elif ! "$APP_DIR/venv/bin/python" -c "import sys" >/dev/null 2>&1; then
        echo "  removing a broken venv from an earlier run"
        rm -rf "$APP_DIR/venv"
    fi
fi

# --system-site-packages is what lets the venv use the apt packages
# above. Without it the venv is sealed off and pip starts compiling.
python3 -m venv --system-site-packages "$APP_DIR/venv"

if "$APP_DIR/venv/bin/python" -c "import fastapi, uvicorn, pydantic, dotenv" >/dev/null 2>&1; then
    echo "  core dependencies available from the system (nothing to build)"
else
    echo "  some dependencies missing; installing with pip..."
    if ! "$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"; then
        echo
        echo "Error: pip could not install the dependencies."
        echo "On 32-bit ARM this usually means it tried to compile them."
        echo "Install the Debian packages by hand and re-run this script:"
        echo "  apt-get install -y python3-fastapi python3-uvicorn \\"
        echo "      python3-pydantic python3-dotenv python3-pil"
        exit 1
    fi
fi

# Pillow is optional: the app runs without it, only logo uploads stop
# working. Say so plainly rather than letting the operator discover it
# when an upload fails months later.
if "$APP_DIR/venv/bin/python" -c "import PIL" >/dev/null 2>&1; then
    echo "  image processing available (logo uploads will work)"
else
    echo "  NOTE: Pillow did not install. Everything works except changing"
    echo "        logos. To fix later: $APP_DIR/venv/bin/pip install Pillow"
fi

echo "Writing $APP_DIR/.env..."
cat > "$APP_DIR/.env" <<EOF
NFT_FAMILY=inet
NFT_TABLE=fw4
NFT_SET=granted_macs
EOF

# The admin password is hashed straight into the database rather than
# written to .env. It never touches disk in plain text, it is not visible
# in `ps`, and it avoids the quoting problems a password with $ or quotes
# in it would cause in a dotenv file.
if [ -n "$ADMIN_PASSWORD" ]; then
    echo "Storing admin credentials..."
    printf '%s' "$ADMIN_PASSWORD" | (cd "$APP_DIR" && "$APP_DIR/venv/bin/python" - "$ADMIN_USERNAME" <<'PYSETUP'
import sys
import auth
import database

username = sys.argv[1]
password = sys.stdin.read()

database.init_db()
salt, digest, iterations = auth.hash_password(password)
database.store_admin_password(salt, digest, iterations)
database.store_admin_username(username)
print(f"  admin user '{username}' created")
PYSETUP
    )
fi

# The .env now holds a password, so keep it off other accounts on the box.
chmod 600 "$APP_DIR/.env"

echo "Generating a self-signed certificate for the admin dashboard..."
# The admin password would otherwise cross an OPEN WiFi network in the
# clear, readable by anyone in range running a packet capture.
mkdir -p /etc/ssl/ecowifi
if [ ! -f /etc/ssl/ecowifi/ecowifi.key ]; then
    openssl req -x509 -nodes -newkey rsa:2048 -days 3650 \
        -keyout /etc/ssl/ecowifi/ecowifi.key \
        -out /etc/ssl/ecowifi/ecowifi.crt \
        -subj "/CN=$AP_IP" \
        -addext "subjectAltName=IP:$AP_IP" 2>/dev/null
    chmod 600 /etc/ssl/ecowifi/ecowifi.key
    echo "  certificate created for $AP_IP (valid 10 years)"
else
    echo "  certificate already exists, keeping it"
fi

echo "Writing nginx configuration..."
sed "s/<PORTAL_PORT>/$PORTAL_PORT/" "$APP_DIR/nginx.conf" \
    > /etc/nginx/sites-available/ecowifi
ln -sf /etc/nginx/sites-available/ecowifi /etc/nginx/sites-enabled/ecowifi
rm -f /etc/nginx/sites-enabled/default
nginx -t

# Record how this machine was set up. doctor.sh reads it so it does not
# report a missing hostapd as a fault on a deployment that never had one.
mkdir -p /etc/ecowifi
cat > /etc/ecowifi/deployment.conf <<DEPLOY
WIFI_MODE=$WIFI_MODE
AP_INTERFACE=$AP_INTERFACE
WAN_INTERFACE=$WAN_INTERFACE
AP_IP=$AP_IP
PORTAL_PORT=$PORTAL_PORT
DEPLOY

echo "Installing systemd services..."
sed "s#<APP_DIR>#$APP_DIR#g; s/<PORTAL_PORT>/$PORTAL_PORT/g" \
    "$APP_DIR/systemd/ecowifi-app.service" > /etc/systemd/system/ecowifi-app.service
cp "$APP_DIR/systemd/ecowifi-nftables.service" /etc/systemd/system/ecowifi-nftables.service
cp "$APP_DIR/systemd/ecowifi-ap.service" /etc/systemd/system/ecowifi-ap.service

# hostapd and dnsmasq are distribution units, so they are ordered after
# the interface setup with drop-ins rather than by editing them. Both are
# also told to retry: a USB adapter that is slow to settle should not
# leave the machine permanently without WiFi.
DROPIN_UNITS="dnsmasq"
[ "$WIFI_MODE" = "onboard" ] && DROPIN_UNITS="hostapd dnsmasq"

for unit in $DROPIN_UNITS; do
    mkdir -p "/etc/systemd/system/$unit.service.d"
    cat > "/etc/systemd/system/$unit.service.d/ecowifi.conf" <<UNIT_DROPIN
[Unit]
After=ecowifi-ap.service
Requires=ecowifi-ap.service

[Service]
Restart=on-failure
RestartSec=5
UNIT_DROPIN
done

# Journald defaults to using up to 10% of the filesystem. On an SD card
# that is both a lot of writes and a lot of space, and this machine runs
# unattended for months.
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/ecowifi.conf <<'JOURNALD'
[Journal]
SystemMaxUse=100M
SystemMaxFileSize=20M
JOURNALD
systemctl restart systemd-journald 2>/dev/null || true

systemctl daemon-reload

echo "Enabling and starting all services..."
SERVICES="ecowifi-ap dnsmasq nginx ecowifi-nftables ecowifi-app"
[ "$WIFI_MODE" = "onboard" ] && SERVICES="ecowifi-ap hostapd dnsmasq nginx ecowifi-nftables ecowifi-app"

systemctl enable $SERVICES
systemctl restart ecowifi-ap
[ "$WIFI_MODE" = "onboard" ] && systemctl restart hostapd
systemctl restart dnsmasq
systemctl restart ecowifi-nftables
systemctl restart ecowifi-app
systemctl restart nginx

# ---------------------------------------------------------------------
# Check what actually came up. `systemctl restart` returning 0 does not
# mean a unit stayed running, and a silent half-install is worse than a
# loud failure.
# ---------------------------------------------------------------------
echo
echo "Verifying services..."
sleep 3
FAILED=""
for unit in $SERVICES; do
    if systemctl is-active --quiet "$unit"; then
        echo "  [  OK  ] $unit"
    else
        echo "  [ FAIL ] $unit"
        FAILED="$FAILED $unit"
    fi
done

if [ -n "$FAILED" ]; then
    echo
    echo "These services did not start:$FAILED"
    echo "Look at the reason with:"
    for unit in $FAILED; do
        echo "  journalctl -u $unit -n 30 --no-pager"
    done
    echo
    echo "Common causes:"
    echo "  ecowifi-ap - adapter not plugged in, or wrong interface name"
    echo "  hostapd    - adapter does not support AP mode (check: iw list | grep -A10 'Supported interface modes')"
    echo "  dnsmasq    - something else is still on port 53 (ss -lnup | grep :53)"
    echo "  nginx      - port 80 or 443 already in use"
fi

echo
echo "Checking the portal answers..."
if curl -fsS -m 5 "http://127.0.0.1/" >/dev/null 2>&1; then
    echo "  [  OK  ] portal responds through nginx"
else
    echo "  [ FAIL ] portal did not respond on http://127.0.0.1/"
fi

echo
echo "Done. Check status with:"
echo "  systemctl status $SERVICES"
echo
echo "If hostapd fails to start, check: journalctl -u hostapd -n 50"
echo

if [ "$WIFI_MODE" = "external" ]; then
    echo "WiFi: provided by your access point on $AP_INTERFACE"
    echo "      (it must be in bridge mode with its DHCP disabled)"
else
    echo "SSID broadcasting: $WIFI_SSID"
fi
echo "Customer portal:   http://$AP_IP/"
echo "Admin dashboard:   https://$AP_IP/admin"
echo
echo "The admin certificate is self-signed, so your browser warns once."
echo "That is expected -- it still stops the password being readable by"
echo "anyone sniffing the open WiFi."
echo

if [ -z "$ADMIN_PASSWORD" ]; then
    echo "No admin password was set. Open https://$AP_IP/admin/setup and enter"
    echo "the one-time token from:"
    echo "  journalctl -u ecowifi-app | grep -A2 'NO ADMIN'"
else
    echo "Sign in as: $ADMIN_USERNAME"
fi
echo
echo "Then set what one payment is worth -- the machine cannot grant any"
echo "time until at least one rate exists."
