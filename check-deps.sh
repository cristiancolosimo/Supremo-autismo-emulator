#!/bin/bash
# Verifica le dipendenze runtime di supremo-autismo-emulator.
# Uso: ./check-deps.sh          (report)
#      ./check-deps.sh --pkgs   (stampa solo i comandi d'installazione)

# nome_comando  ->  "pkg_fedora pkg_arch  descrizione"
declare -A DEPS=(
  [qemu-system-mips]="qemu-system-mips qemu-system-mips  emula MIPS big-endian"
  [qemu-system-mipsel]="qemu-system-mips qemu-system-mips  emula MIPS little-endian"
  [qemu-system-arm]="qemu-system-arm qemu-system-arm  emula ARM"
  [mke2fs]="e2fsprogs e2fsprogs  costruisce l'immagine ext2 (mke2fs -d)"
  [debugfs]="e2fsprogs e2fsprogs  inietta i device node senza mount/root"
  [file]="file file  rileva architettura/endianness"
  [python3]="python3 python  orchestratore (>=3.11)"
  [ip]="iproute iproute2  crea/gestisce il TAP (setup-tap.sh)"
  [binwalk]="—(cargo) —(cargo)  estrae il firmware (cargo install binwalk, OBBLIGATORIA v3)"
  [telnet]="telnet inetutils  client per la shell di debug (--keep-alive)"
  [curl]="curl curl  test manuale del web"
)

distro() {
  [ -f /etc/os-release ] && . /etc/os-release
  case "$ID $ID_LIKE" in
    *arch*) echo arch;; *fedora*|*rhel*) echo fedora;; *) echo other;;
  esac
}
D=$(distro)
COL_IDX=$([ "$D" = arch ] && echo 2 || echo 1)   # 1=fedora, 2=arch

pkg_for() { echo "$1" | awk -v i="$COL_IDX" '{print $i}'; }

if [ "$1" = "--pkgs" ]; then
  need=""
  for c in "${!DEPS[@]}"; do command -v "$c" >/dev/null || need="$need $(pkg_for "${DEPS[$c]}")"; done
  need=$(echo $need | tr ' ' '\n' | grep -v '^—' | sort -u | tr '\n' ' ')
  case "$D" in
    # arch splitta il firmware SeaBIOS dal pacchetto qemu; su fedora è incluso
    arch)   echo "sudo pacman -S --needed $need seabios" ;;
    fedora) echo "sudo dnf install -y $need" ;;
    *)      echo "installa: $need" ;;
  esac
  exit 0
fi

echo "== supremo-autismo-emulator :: check dipendenze (distro: $D) =="
miss=0
report() {
  local -n MAP=$1; local kind=$2
  for c in $(echo "${!MAP[@]}" | tr ' ' '\n' | sort); do
    if p=$(command -v "$c" 2>/dev/null); then
      printf "  [ OK ] %-20s %s\n" "$c" "$p"
    else
      printf "  [MISS] %-20s -> %s\n" "$c" "${MAP[$c]}"
      [ "$kind" = req ] && miss=$((miss+1))
    fi
  done
}
echo "-- richieste --"; report DEPS req

# controlli fini
if command -v python3 >/dev/null; then
  python3 -c 'import sys; sys.exit(0 if sys.version_info>=(3,11) else 1)' \
    || { echo "  [WARN] python3 < 3.11"; miss=$((miss+1)); }
fi
if command -v binwalk >/dev/null; then
  binwalk --version 2>/dev/null | grep -q " 3" \
    || { echo "  [FAIL] binwalk non v3 (serve la v3 rust: cargo install binwalk)"; miss=$((miss+1)); }
fi

echo
if [ "$miss" -eq 0 ]; then
  echo "Tutte le dipendenze richieste presenti."
else
  echo "$miss dipendenze richieste mancanti. Installa con:"
  echo "  $("$0" --pkgs)"
fi
exit $([ "$miss" -eq 0 ] && echo 0 || echo 1)
