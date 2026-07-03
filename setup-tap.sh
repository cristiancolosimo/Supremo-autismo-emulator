#!/bin/bash
# Crea (o rimuove) un TAP di proprietà dell'utente per il verify boot rootless.
# QEMU vi si attacca senza root; l'host ottiene .2, il guest usa .1 (IP statico).
#
# Uso:
#   sudo ./setup-tap.sh [sae0] [192.168.0.2/24]        TAP semplice (LAN untagged)
#   sudo ./setup-tap.sh [sae0] [192.168.0.2/24] [VID]  TAP + VLAN (LAN taggata)
#   sudo ./setup-tap.sh sae0 down                       rimuove tutto
#
# VLAN: alcuni firmware (MediaTek/Ralink, es. TP-Link) mettono la LAN su interfacce
# VLAN-taggate (br0 su eth0.3/.4/...). L'host untagged non le raggiunge. Passa il VID
# giusto (lo trovi in `sae` → plan/state.json, campo "vlan") e l'IP va sulla sub-if
# taggata: sudo ./setup-tap.sh sae0 192.168.0.2/24 3  →  poi apri http://192.168.0.1/
set -e
TAP=${1:-sae0}
ADDR=${2:-192.168.0.2/24}
VID=${3:-}
USER_NAME=${SUDO_USER:-$(whoami)}

if [ "$ADDR" = "down" ]; then
  for v in $(ip -o link show 2>/dev/null | sed -n "s/.*: \(${TAP}\.[0-9]*\)@.*/\1/p"); do
    ip link del "$v" 2>/dev/null || true
  done
  ip link set "$TAP" down 2>/dev/null || true
  ip tuntap del dev "$TAP" mode tap 2>/dev/null || true
  echo "[*] TAP $TAP (e VLAN) rimosso"
  exit 0
fi

ip tuntap add dev "$TAP" mode tap user "$USER_NAME"
ip link set "$TAP" up

if [ -n "$VID" ]; then
  # LAN taggata: l'IP dell'host va sulla sub-interface VLAN; il TAP grezzo trasporta
  # i frame taggati che il guest riceve su ethN.$VID → br0.
  ip link add link "$TAP" name "${TAP}.${VID}" type vlan id "$VID"
  ip addr add "$ADDR" dev "${TAP}.${VID}"
  ip link set "${TAP}.${VID}" up
  echo "[*] TAP $TAP up + VLAN ${TAP}.${VID}, host=$ADDR, owner=$USER_NAME"
else
  ip addr add "$ADDR" dev "$TAP"
  echo "[*] TAP $TAP up, host=$ADDR, owner=$USER_NAME"
fi
echo "    ora:  ./sae run firmwares/mod10.bin --tap $TAP"
