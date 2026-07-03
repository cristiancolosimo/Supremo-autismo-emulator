#!/bin/sh
# Compila lo shim LD_PRELOAD (ioctl + open di device SoC assenti) per un'arch target.
# La pipeline lo auto-installa se trova assets/binaries/ioctl_stub.<arch>.so.
#
# Uso:  ./build.sh <cc> [arch]
#   <cc>   cross-compiler, es. mipsel-linux-gcc (toolchain Bootlin uClibc consigliata:
#          https://toolchains.bootlin.com → mips32el/uclibc → bin/mipsel-linux-gcc)
#   [arch] nome arch per il file .so (default: mipsel)
#
# Esempio:
#   ./build.sh /path/mips32el--uclibc--stable/bin/mipsel-linux-gcc mipsel
set -e
CC="${1:?serve il cross-compiler, es. mipsel-linux-gcc}"
ARCH="${2:-mipsel}"
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/../../binaries/ioctl_stub.$ARCH.so"
"$CC" -shared -fPIC -O2 -o "$OUT" "$HERE/ioctl_stub.c"
echo "-> $OUT"
# sanity: deve dipendere solo dalla libc del guest (no libdl → niente dlsym)
command -v "${CC%gcc}readelf" >/dev/null 2>&1 && "${CC%gcc}readelf" -d "$OUT" | grep NEEDED || true
