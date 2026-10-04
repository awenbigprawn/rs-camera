#!/bin/sh
# Use an existing wired/local connection; retain address, routes and SSH service.
set -eu
action=${1:-status}
interface=${2:-wlan0}
case "$interface" in *[!a-zA-Z0-9_.:-]*|'') echo 'Invalid interface' >&2; exit 2;; esac
case "$action" in
  status) ip -br address; ip route; exit;;
  disable-wifi)
    if [ -n "${SSH_CONNECTION:-}" ]; then
      peer=${SSH_CONNECTION%% *}
      route=$(ip route get "$peer")
      case " $route " in *" dev $interface "*) echo 'SSH uses this interface; connect through Ethernet first.' >&2; exit 1;; esac
    fi
    sudo ip link set dev "$interface" down
    ;;
  enable-wifi) sudo ip link set dev "$interface" up;;
  *) echo 'Usage: network.sh {status|disable-wifi|enable-wifi} [wlan0]' >&2; exit 2;;
esac
ip -br address show dev "$interface"
