#!/bin/sh
# Brings the access-point interface up with its static address.
#
# This exists because `ip addr add` does not survive a reboot, and because
# three separate things have to be true before hostapd or dnsmasq can
# start, none of which are guaranteed on a machine that has just booted:
#
#   1. the adapter exists -- USB devices enumerate late, often after the
#      services that depend on them have already tried and failed
#   2. nothing else is managing it -- NetworkManager and wpa_supplicant
#      both claim wireless interfaces by default and will fight hostapd
#      for control, usually winning
#   3. it is UP and has the address dnsmasq expects to bind to
#
# install.sh fills in <AP_INTERFACE> and <AP_IP_ADDRESS>.

AP_INTERFACE="<AP_INTERFACE>"
AP_IP="<AP_IP_ADDRESS>"

# ---- 1. wait for the adapter -------------------------------------------
WAITED=0
while [ "$WAITED" -lt 30 ]; do
    if ip link show "$AP_INTERFACE" >/dev/null 2>&1; then
        break
    fi
    sleep 1
    WAITED=$((WAITED + 1))
done

if ! ip link show "$AP_INTERFACE" >/dev/null 2>&1; then
    echo "ecowifi-ap: interface $AP_INTERFACE never appeared after ${WAITED}s" >&2
    echo "ecowifi-ap: is the USB WiFi adapter plugged in?" >&2
    exit 1
fi

# ---- 2. take it off anything else that manages it -----------------------
if command -v nmcli >/dev/null 2>&1; then
    nmcli device set "$AP_INTERFACE" managed no >/dev/null 2>&1 || true
fi

# A wpa_supplicant bound to this interface holds the radio and hostapd
# will not get it.
if command -v wpa_cli >/dev/null 2>&1; then
    wpa_cli -i "$AP_INTERFACE" terminate >/dev/null 2>&1 || true
fi
systemctl stop "wpa_supplicant@$AP_INTERFACE.service" >/dev/null 2>&1 || true

# A soft rfkill block stops hostapd with no useful message.
if command -v rfkill >/dev/null 2>&1; then
    rfkill unblock wifi >/dev/null 2>&1 || true
fi

# ---- 3. address and bring it up ----------------------------------------
# Flush first so re-running this does not stack duplicate addresses.
ip addr flush dev "$AP_INTERFACE" 2>/dev/null || true
ip addr add "$AP_IP/24" dev "$AP_INTERFACE" || {
    echo "ecowifi-ap: could not assign $AP_IP to $AP_INTERFACE" >&2
    exit 1
}
ip link set dev "$AP_INTERFACE" up || {
    echo "ecowifi-ap: could not bring $AP_INTERFACE up" >&2
    exit 1
}

echo "ecowifi-ap: $AP_INTERFACE is up with $AP_IP"
