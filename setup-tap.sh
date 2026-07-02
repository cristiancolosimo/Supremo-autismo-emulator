#!/bin/bash
# Crea (o rimuove) un TAP di proprietà dell'utente per il verify boot rootless.
# QEMU vi si attacca senza root; l'host ottiene .2, il guest usa .1 (IP statico).
# Uso:  sudo ./setup-tap.sh [sae0] [192.168.0.2/24]   |   sudo ./setup-tap.sh sae0 down
set -e
TAP=${1:-sae0}
ADDR=${2:-192.168.0.2/24}
USER_NAME=${SUDO_USER:-$(whoami)}

if [ "$ADDR" = "down" ]; then
  ip link set "$TAP" down 2>/dev/null || true
  ip tuntap del dev "$TAP" mode tap 2>/dev/null || true
  echo "[*] TAP $TAP rimosso"
  exit 0
fi

ip tuntap add dev "$TAP" mode tap user "$USER_NAME"
ip addr add "$ADDR" dev "$TAP"
ip link set "$TAP" up
echo "[*] TAP $TAP up, host=$ADDR, owner=$USER_NAME"
echo "    ora:  ./sae run firmwares/mod10.bin --tap $TAP"
