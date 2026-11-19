#!/bin/bash
# Install Cockpit and listen only on a private address. Does not change the firewall.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "install-cockpit: run as root" >&2
  exit 1
fi

if [ ! -r /etc/os-release ]; then
  echo "install-cockpit: needs Debian or Ubuntu" >&2
  exit 1
fi

# shellcheck disable=SC1091
. /etc/os-release
case "${ID:-}" in
  debian|ubuntu) ;;
  *)
    echo "install-cockpit: needs Debian or Ubuntu, not ${ID:-unknown}" >&2
    exit 1
    ;;
esac

bind="${COCKPIT_BIND:-127.0.0.1}"
case "$bind" in
  0.0.0.0|::|'*')
    echo "install-cockpit: refusing to listen on all interfaces" >&2
    exit 1
    ;;
esac

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y cockpit

install -d /etc/systemd/system/cockpit.socket.d
cat > /etc/systemd/system/cockpit.socket.d/listen.conf <<EOF
[Socket]
ListenStream=
ListenStream=${bind}:9090
EOF

systemctl daemon-reload
systemctl enable cockpit.socket
systemctl restart cockpit.socket
echo "install-cockpit: listening on ${bind}:9090"
