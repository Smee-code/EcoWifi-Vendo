#!/bin/bash
# Read-only diagnostic for a deployed gateway.
#
#   sudo bash doctor.sh
#
# Changes nothing. Run it when something is not working and paste the
# output. Each check names the specific thing that is wrong rather than
# reporting a generic failure, because on this stack almost every symptom
# ("no WiFi", "no portal", "no internet after paying") has four or five
# possible causes that look identical from the outside.

PASS=0
FAIL=0
WARN=0

ok()   { echo "  [ OK ] $1"; PASS=$((PASS + 1)); }
bad()  { echo "  [FAIL] $1"; FAIL=$((FAIL + 1)); }
warn() { echo "  [WARN] $1"; WARN=$((WARN + 1)); }
hint() { echo "         -> $1"; }

# How was this machine set up? Written by install.sh. Without it the
# checks below would report a missing hostapd as a fault on a deployment
# that deliberately never had one.
WIFI_MODE="onboard"
if [ -r /etc/ecowifi/deployment.conf ]; then
    . /etc/ecowifi/deployment.conf
fi

echo
echo "EcoWifi Vendo diagnostic"
echo "========================"
if [ "$WIFI_MODE" = "external" ]; then
    echo "  WiFi: external access point on ${AP_INTERFACE:-unknown}"
else
    echo "  WiFi: USB adapter on this board (hostapd)"
fi

# ---------------------------------------------------------------- services
echo
echo "Services"
UNITS="ecowifi-ap dnsmasq nginx ecowifi-nftables ecowifi-app"
[ "$WIFI_MODE" != "external" ] && UNITS="ecowifi-ap hostapd dnsmasq nginx ecowifi-nftables ecowifi-app"

for unit in $UNITS; do
    if ! systemctl list-unit-files "$unit.service" >/dev/null 2>&1 ||
       ! systemctl cat "$unit.service" >/dev/null 2>&1; then
        warn "$unit is not installed"
        continue
    fi
    if systemctl is-active --quiet "$unit"; then
        ok "$unit running"
    else
        bad "$unit not running"
        hint "journalctl -u $unit -n 30 --no-pager"
    fi
done

# --------------------------------------------------------------- interfaces
echo
echo "Network interfaces"
AP_IFACE="$AP_INTERFACE"
if [ -z "$AP_IFACE" ]; then
    AP_IFACE=$(awk -F= '/^interface=/{print $2}' /etc/hostapd/hostapd.conf 2>/dev/null)
fi
if [ -z "$AP_IFACE" ]; then
    bad "cannot determine the client-facing interface"
    hint "expected /etc/ecowifi/deployment.conf or /etc/hostapd/hostapd.conf"
else
    if ip link show "$AP_IFACE" >/dev/null 2>&1; then
        ok "AP interface $AP_IFACE exists"

        if ip link show "$AP_IFACE" | grep -q "state UP"; then
            ok "$AP_IFACE is UP"
        else
            bad "$AP_IFACE is DOWN"
            hint "systemctl restart ecowifi-ap"
        fi

        ADDR=$(ip -4 -o addr show "$AP_IFACE" 2>/dev/null | awk '{print $4}')
        if [ -n "$ADDR" ]; then
            ok "$AP_IFACE has address $ADDR"
        else
            bad "$AP_IFACE has NO IP address"
            hint "dnsmasq cannot bind and the portal is unreachable"
            hint "systemctl restart ecowifi-ap"
        fi
    else
        bad "client interface $AP_IFACE does not exist"
        hint "is the adapter plugged in? check: ip -o link show"
    fi

    # With an external AP, every customer must reach this machine with
    # their OWN MAC. If the AP is left in router mode they all arrive
    # wearing its single address and the per-device model collapses.
    if [ "$WIFI_MODE" = "external" ]; then
        NEIGHBOURS=$(ip neigh show dev "$AP_IFACE" 2>/dev/null | grep -c lladdr)
        if [ "$NEIGHBOURS" -gt 1 ]; then
            ok "$NEIGHBOURS client device(s) visible on $AP_IFACE"
        elif [ "$NEIGHBOURS" -eq 1 ]; then
            warn "only one device visible on $AP_IFACE"
            hint "if that is the access point itself, it is still in router"
            hint "mode -- switch it to bridge/AP mode and disable its DHCP"
        else
            warn "no client devices seen on $AP_IFACE yet"
            hint "normal until a phone connects through the access point"
        fi
    fi
fi

# --------------------------------------------------------------- radio / AP
if [ "$WIFI_MODE" != "external" ]; then
echo
echo "Wireless"
if command -v rfkill >/dev/null 2>&1; then
    if rfkill list 2>/dev/null | grep -q "Soft blocked: yes"; then
        bad "something is rfkill soft-blocked"
        hint "rfkill unblock all"
    else
        ok "no rfkill block"
    fi
fi

if command -v iw >/dev/null 2>&1 && [ -n "$AP_IFACE" ]; then
    PHY=$(iw dev "$AP_IFACE" info 2>/dev/null | awk '/wiphy/ {print $2}')
    if [ -n "$PHY" ]; then
        if iw phy "phy$PHY" info 2>/dev/null |
             awk '/Supported interface modes/{f=1;next} /^\t[A-Za-z]/{f=0} f' |
             grep -qw '\* AP'; then
            ok "adapter supports AP mode"
        else
            bad "adapter does NOT support AP mode"
            hint "hostapd cannot work with this adapter; use RT5370/MT7610U/MT7612U"
        fi
    fi
    if iw dev 2>/dev/null | grep -q "type AP"; then
        ok "an interface is actually in AP mode (SSID is on the air)"
    else
        warn "no interface is currently in AP mode"
    fi
fi
fi

# ------------------------------------------------------------------- ports
echo
echo "Ports"
if command -v ss >/dev/null 2>&1; then
    if ss -lnup 2>/dev/null | grep -q ":53 "; then
        WHO=$(ss -lnup 2>/dev/null | awk '/:53 /{print $NF}' | head -1)
        case "$WHO" in
            *dnsmasq*) ok "port 53 held by dnsmasq" ;;
            *) bad "port 53 held by something else: $WHO"
               hint "dnsmasq cannot start; usually systemd-resolved" ;;
        esac
    else
        bad "nothing is listening on port 53 (no DHCP/DNS for clients)"
    fi

    ss -lntp 2>/dev/null | grep -q ":80 "  && ok "port 80 listening (portal)"  || bad "nothing on port 80"
    ss -lntp 2>/dev/null | grep -q ":443 " && ok "port 443 listening (admin)"  || warn "nothing on port 443"
fi

# ---------------------------------------------------------------- firewall
echo
echo "Firewall"
if command -v nft >/dev/null 2>&1; then
    if nft list table inet fw4 >/dev/null 2>&1; then
        ok "nftables table inet fw4 exists"
        if nft list set inet fw4 granted_macs >/dev/null 2>&1; then
            COUNT=$(nft list set inet fw4 granted_macs 2>/dev/null | grep -oE '([0-9a-f]{2}:){5}[0-9a-f]{2}' | wc -l)
            ok "granted_macs set exists ($COUNT device(s) currently allowed)"
        else
            bad "granted_macs set missing - nobody can ever be granted access"
            hint "systemctl restart ecowifi-nftables"
        fi
        if nft list chain inet fw4 prerouting 2>/dev/null | grep -q "redirect"; then
            ok "captive portal redirect rule present"
        else
            bad "no portal redirect rule - clients will not see the portal"
        fi
        if nft list chain inet fw4 postrouting 2>/dev/null | grep -q "masquerade"; then
            ok "NAT masquerade rule present"
        else
            bad "no masquerade rule - granted clients get no internet"
        fi
    else
        bad "nftables table inet fw4 does not exist"
        hint "systemctl restart ecowifi-nftables"
    fi
fi

if [ "$(cat /proc/sys/net/ipv4/ip_forward 2>/dev/null)" = "1" ]; then
    ok "IP forwarding enabled"
else
    bad "IP forwarding is OFF - no client can reach the internet"
    hint "sysctl -w net.ipv4.ip_forward=1  (and check /etc/sysctl.conf)"
fi

# ------------------------------------------------------------------- clock
echo
echo "Clock"
YEAR=$(date -u +%Y)
if [ "$YEAR" -lt 2024 ]; then
    bad "system clock reads $(date -u) - not synced"
    hint "sessions are timed against this; run: timedatectl set-ntp true"
else
    ok "system clock looks sane ($(date -u '+%Y-%m-%d %H:%M') UTC)"
fi
if command -v timedatectl >/dev/null 2>&1; then
    if timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -q yes; then
        ok "NTP synchronised"
    else
        warn "NTP not synchronised yet"
    fi
fi

# --------------------------------------------------------------------- app
echo
echo "Application"
if command -v curl >/dev/null 2>&1; then
    if curl -fsS -m 5 http://127.0.0.1/ >/dev/null 2>&1; then
        ok "portal responds through nginx"
    else
        bad "portal does not respond on http://127.0.0.1/"
        hint "journalctl -u ecowifi-app -n 30 --no-pager"
    fi

    STATUS=$(curl -fsS -m 5 http://127.0.0.1/status 2>/dev/null)
    if [ -n "$STATUS" ]; then
        ok "/status answers"
        if echo "$STATUS" | grep -q '"configured":true'; then
            ok "at least one payment rate is configured"
        else
            bad "NO payment rate configured - /claim and /grant refuse to run"
            hint "open https://<AP_IP>/admin and set what one payment is worth"
        fi
    fi
fi

DB=$(ls /root/EcoWifi-Vendo/ecowifi.db /home/*/EcoWifi-Vendo/ecowifi.db 2>/dev/null | head -1)
[ -n "$DB" ] && ok "database at $DB ($(du -h "$DB" 2>/dev/null | cut -f1))"

# -------------------------------------------------------------------- disk
echo
echo "Storage"
# Find the field that ends in %, rather than trusting a column index:
# df output columns shift between implementations.
USE=$(df -P / 2>/dev/null | awk 'NR==2{for(i=1;i<=NF;i++) if($i ~ /%$/){gsub("%","",$i); print $i; exit}}')
if ! echo "$USE" | grep -Eq '^[0-9]+$'; then
    warn "could not read disk usage"
elif [ "$USE" -ge 90 ]; then
    bad "root filesystem is ${USE}% full"
elif [ "$USE" -ge 75 ]; then
    warn "root filesystem is ${USE}% full"
else
    ok "root filesystem ${USE}% used"
fi

# ------------------------------------------------------------------ summary
echo
echo "========================"
echo "  $PASS passed, $WARN warnings, $FAIL failures"
echo
if [ "$FAIL" -gt 0 ]; then
    echo "Fix the [FAIL] items above, starting from the top: they cascade."
    echo "An interface with no address explains dnsmasq, which explains the"
    echo "portal, which explains everything else."
else
    echo "Nothing broken from here. If clients still cannot connect, check"
    echo "from a PHONE that it gets its own IP, not 127.0.0.1:"
    echo "  curl -s http://<AP_IP>/status"
fi
echo
