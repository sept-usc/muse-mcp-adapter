#!/bin/sh
# Egress guard for the Muse MCP Adapter.
#
# This container owns the network namespace that the adapter joins (network_mode: service:guard)
# and installs nftables rules there before the adapter starts. The adapter already refuses to call
# private, link-local and cloud-metadata addresses; this blocks the same ranges at the network, so
# a mistake or a bypass in the adapter still cannot reach them. Loopback (Docker's embedded DNS
# lives there) and replies to inbound connections (from the ingress container) stay allowed.
#
# MMA_EGRESS_ALLOW: optional comma or space separated CIDRs to exempt, for example a private
# MCP server you run yourself.
set -eu

ALLOW4=""
ALLOW6=""
for cidr in $(echo "${MMA_EGRESS_ALLOW:-}" | tr ',' ' '); do
  case "$cidr" in
    *[!0-9a-fA-F:./]*) echo "muse-adapter-guard: not a CIDR: $cidr" >&2; exit 1 ;;
    *:*) ALLOW6="$ALLOW6${ALLOW6:+, }$cidr" ;;
    *) ALLOW4="$ALLOW4${ALLOW4:+, }$cidr" ;;
  esac
done
ELEMENTS4=""
ELEMENTS6=""
[ -n "$ALLOW4" ] && ELEMENTS4="elements = { $ALLOW4 }"
[ -n "$ALLOW6" ] && ELEMENTS6="elements = { $ALLOW6 }"

apply() {
  if nft list table inet mma_guard >/dev/null 2>&1; then nft delete table inet mma_guard; fi
  nft -f - <<NFT
table inet mma_guard {
  set allow4 { type ipv4_addr; flags interval; $ELEMENTS4 }
  set allow6 { type ipv6_addr; flags interval; $ELEMENTS6 }
  set block4 {
    type ipv4_addr; flags interval
    elements = { 0.0.0.0/8, 10.0.0.0/8, 100.64.0.0/10, 127.0.0.0/8, 169.254.0.0/16, 172.16.0.0/12,
                 192.0.0.0/24, 192.0.2.0/24, 192.168.0.0/16, 198.18.0.0/15, 198.51.100.0/24,
                 203.0.113.0/24, 224.0.0.0/3 }
  }
  set block6 {
    type ipv6_addr; flags interval
    elements = { ::/128, ::1/128, ::ffff:0:0/96, 64:ff9b::/96, 100::/64, 2001:db8::/32,
                 fc00::/7, fe80::/10, ff00::/8 }
  }
  chain output {
    type filter hook output priority filter; policy accept;
    oifname "lo" accept
    ct state established,related accept
    ip daddr @allow4 accept
    ip6 daddr @allow6 accept
    ip daddr @block4 counter reject with icmp type admin-prohibited
    ip6 daddr @block6 counter reject with icmpv6 type admin-prohibited
  }
}
NFT
}

apply
touch /run/mma-guard.ready
echo "muse-adapter-guard: egress rules installed${ALLOW4:+ (allowed: $ALLOW4)}${ALLOW6:+ (allowed: $ALLOW6)}"

# Put the rules back if something removes them; exit cleanly when stopped.
trap 'exit 0' TERM INT
while :; do
  sleep 30 &
  wait $!
  nft list table inet mma_guard >/dev/null 2>&1 || { echo "muse-adapter-guard: rules missing, applying again"; apply; }
done
