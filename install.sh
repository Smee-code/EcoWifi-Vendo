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

read -p "AP interface (USB WiFi adapter, e.g. wlan1): " AP_INTERFACE
read -p "WAN interface (USB-LAN adapter, e.g. eth1): " WAN_INTERFACE
read -p "Static IP to assign to the AP interface (e.g. 192.168.50.1): " AP_IP
read -p "Port for the FastAPI portal (e.g. 8000): " PORTAL_PORT

# Set the operator password here so the Orange Pi is never briefly live
# with no admin password. Leave it blank to use the first-run setup page
# instead, which prints a one-time token to the journal.
echo
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

echo
echo "Installing system packages..."
apt-get update
apt-get install -y hostapd dnsmasq nftables python3 python3-venv python3-pip

echo "Stopping hostapd/dnsmasq while we configure (avoids port conflicts)..."
systemctl stop hostapd 2>/dev/null || true
systemctl stop dnsmasq 2>/dev/null || true

echo "Writing /etc/hostapd/hostapd.conf..."
sed "s/<AP_INTERFACE>/$AP_INTERFACE/" "$APP_DIR/hostapd.conf" > /etc/hostapd/hostapd.conf

echo "Writing /etc/dnsmasq.conf..."
sed -e "s/<AP_INTERFACE>/$AP_INTERFACE/" -e "s/<AP_IP_ADDRESS>/$AP_IP/" -e "s/<DHCP_RANGE_START>/$DHCP_RANGE_START/" -e "s/<DHCP_RANGE_END>/$DHCP_RANGE_END/" \
    "$APP_DIR/dnsmasq.conf" > /etc/dnsmasq.conf

echo "Assigning static IP $AP_IP to $AP_INTERFACE..."
ip addr flush dev "$AP_INTERFACE" 2>/dev/null || true
ip addr add "$AP_IP/24" dev "$AP_INTERFACE" 2>/dev/null || true

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

echo "Setting up Python virtual environment..."
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "Writing $APP_DIR/.env..."
cat > "$APP_DIR/.env" <<EOF
NFT_FAMILY=inet
NFT_TABLE=fw4
NFT_SET=granted_macs
EOF

if [ -n "$ADMIN_PASSWORD" ]; then
    echo "ECOWIFI_ADMIN_PASSWORD=$ADMIN_PASSWORD" >> "$APP_DIR/.env"
fi

# The .env now holds a password, so keep it off other accounts on the box.
chmod 600 "$APP_DIR/.env"

echo "Installing systemd services..."
sed "s#<APP_DIR>#$APP_DIR#g; s/<PORTAL_PORT>/$PORTAL_PORT/g" \
    "$APP_DIR/systemd/ecowifi-app.service" > /etc/systemd/system/ecowifi-app.service
cp "$APP_DIR/systemd/ecowifi-nftables.service" /etc/systemd/system/ecowifi-nftables.service

systemctl daemon-reload

echo "Enabling and starting all services..."
systemctl enable hostapd dnsmasq ecowifi-nftables ecowifi-app
systemctl restart hostapd
systemctl restart dnsmasq
systemctl restart ecowifi-nftables
systemctl restart ecowifi-app

echo
echo "Done. Check status with:"
echo "  systemctl status hostapd dnsmasq ecowifi-nftables ecowifi-app"
echo
echo "If hostapd fails to start, check: journalctl -u hostapd -n 50"
echo

if [ -z "$ADMIN_PASSWORD" ]; then
    echo "No admin password was set. Open http://$AP_IP:$PORTAL_PORT/admin/setup"
    echo "and enter the one-time token from:"
    echo "  journalctl -u ecowifi-app | grep -A2 'NO ADMIN'"
else
    echo "Admin dashboard: http://$AP_IP:$PORTAL_PORT/admin"
fi
