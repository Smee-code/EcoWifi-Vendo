#!/bin/sh
# Base nftables ruleset for the vendo gateway.
#
# Replace <AP_INTERFACE>, <WAN_INTERFACE> and <PORTAL_PORT> with real
# values first (install.sh does this for you).
#
# Model: default-DROP all client forwarding, except devices whose MAC is
# in the 'granted_macs' set, which are ACCEPTed and NATed out through the
# WAN. Un-granted clients hitting port 80 are transparently redirected to
# the portal, so the captive-portal popup fires on their phone.
#
# main.py / network_service.py only add and remove elements from
# 'granted_macs'. They never touch the structure below.
#
# EVERYTHING LIVES IN ONE TABLE, and that is not cosmetic. nftables sets
# are scoped to their table, so a rule in a different table cannot see
# @granted_macs at all. Splitting filter rules into 'inet fw4' and NAT
# rules into 'inet nat' -- which reads naturally, and is what this script
# used to do -- makes the redirect rule fail to load with:
#
#   Error: No such file or directory;
#   did you mean set 'granted_macs' in table inet 'fw4'?
#
# The visible symptom is not an error, though: the gateway comes up, the
# WiFi works, and the captive portal never appears for anybody.
#
# SAFE TO RE-RUN: chains are flushed before their rules are added, so
# restarting the service rebuilds the ruleset instead of appending a
# second copy. 'granted_macs' is deliberately NOT flushed, so paying
# customers keep their access across a firewall restart.

set -e

AP_INTERFACE="<AP_INTERFACE>"
WAN_INTERFACE="<WAN_INTERFACE>"
PORTAL_PORT="<PORTAL_PORT>"

# ---- table and the set of currently-paid MAC addresses ----
# 'add' is idempotent for tables, sets and chains; only rules need
# flushing.

nft add table inet fw4
nft add set inet fw4 granted_macs '{ type ether_addr; }'

# ---- filter: default-drop, allow only granted MACs ----

nft add chain inet fw4 input '{ type filter hook input priority 0; policy accept; }'
nft add chain inet fw4 forward '{ type filter hook forward priority 0; policy drop; }'

nft flush chain inet fw4 input
nft flush chain inet fw4 forward

nft add rule inet fw4 forward iifname "$AP_INTERFACE" ether saddr @granted_macs accept
nft add rule inet fw4 forward ct state established,related accept

# ---- nat: same table, so the set above is in scope ----

nft add chain inet fw4 prerouting '{ type nat hook prerouting priority -100; }'
nft add chain inet fw4 postrouting '{ type nat hook postrouting priority 100; }'

nft flush chain inet fw4 prerouting
nft flush chain inet fw4 postrouting

# Masquerade granted clients out through the WAN.
nft add rule inet fw4 postrouting oifname "$WAN_INTERFACE" masquerade

# Un-granted clients asking for port 80 get the portal instead, whatever
# address they were trying to reach. This is what makes the sign-in
# notification appear, and it is why DNS is NOT hijacked: granted clients
# are not redirected and resolve names normally.
nft add rule inet fw4 prerouting iifname "$AP_INTERFACE" ether saddr != @granted_macs tcp dport 80 redirect to :"$PORTAL_PORT"

echo "Base ruleset applied. Verify with: nft list ruleset"
