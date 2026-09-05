#!/bin/sh
# Base nftables ruleset for the EcoWifi Vendo Orange Pi.
#
# This is a STARTING POINT, not a drop-in production script -- replace
# <AP_INTERFACE>, <WAN_INTERFACE>, and <PORTAL_PORT> with your actual
# values first (find interface names with: ip link show; PORTAL_PORT
# is whatever port uvicorn runs main:app on, e.g. 8000).
#
# Model: default-DROP all client (AP-side) forwarding, except anything
# whose MAC address is in the 'granted_macs' set, which is ACCEPTed
# and NATed out through the WAN interface. Un-granted clients trying
# to reach port 80 get transparently redirected to the FastAPI portal
# instead, so the captive-portal popup fires automatically on their
# phone/laptop.
#
# main.py / network_service.py only add/remove elements from
# 'granted_macs' -- they never touch the base structure below.
#
# SAFE TO RE-RUN: every chain is flushed before its rules are added,
# so restarting ecowifi-nftables.service rebuilds the ruleset instead
# of appending a second copy of it. The 'granted_macs' set is
# deliberately NOT flushed -- currently-paid-for clients keep their
# access across a firewall restart, matching the sessions still marked
# active in the database.

AP_INTERFACE="<AP_INTERFACE>"
WAN_INTERFACE="<WAN_INTERFACE>"
PORTAL_PORT="<PORTAL_PORT>"

# ---- filter table: default-drop, allow only granted MACs ----
# 'add' is idempotent for tables, sets and chains -- re-running is a
# no-op for these. Only rules need explicit flushing.

nft add table inet fw4
nft add set inet fw4 granted_macs '{ type ether_addr; }'

nft add chain inet fw4 input '{ type filter hook input priority 0; policy accept; }'
nft add chain inet fw4 forward '{ type filter hook forward priority 0; policy drop; }'

nft flush chain inet fw4 input
nft flush chain inet fw4 forward

nft add rule inet fw4 forward iifname "$AP_INTERFACE" ether saddr @granted_macs accept

# Un-granted clients must still reach the portal and the admin dashboard on
# this machine. The input chain accepts by default, so 80/443 are already
# reachable -- this rule only documents the dependency.
nft add rule inet fw4 forward ct state established,related accept

# ---- nat table: masquerade granted clients out through WAN ----

nft add table inet nat
nft add chain inet nat postrouting '{ type nat hook postrouting priority 100; }'

# ---- captive portal redirect: un-granted clients hitting port 80 ----
# get transparently redirected to the FastAPI portal instead of
# reaching the (blocked) real internet, so the OS captive-portal
# detection popup appears automatically.

nft add chain inet nat prerouting '{ type nat hook prerouting priority -100; }'

nft flush chain inet nat postrouting
nft flush chain inet nat prerouting

nft add rule inet nat postrouting oifname "$WAN_INTERFACE" masquerade
# Redirect to nginx on :80, which proxies the portal. The app itself
# listens on loopback only.
nft add rule inet nat prerouting iifname "$AP_INTERFACE" ether saddr != @granted_macs tcp dport 80 redirect to :80

echo "Base ruleset applied. Verify with: nft list ruleset"
