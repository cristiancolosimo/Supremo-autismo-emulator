#!/bin/sh
# Compila httpd_trigger.<arch>.so (bypass del gate switch: invia il ServiceCfg 0x7ee a
# httpd /var/tmp/8). La pipeline lo installa in /firmadyne se presente in assets/binaries/.
#
# Serve la STESSA cross-toolchain uClibc con cui è stato costruito ioctl_stub.mipsel.so
# (Bootlin mips32el/uClibc: https://toolchains.bootlin.com → bin/mipsel-linux-gcc): porta
# headers + libc + linker mips coerenti col guest. clang da solo qui non basta — compila
# l'oggetto mipsel ma manca sia il sysroot (gnu/stubs) sia un linker mips (ld.bfd non
# conosce elf32ltsmip; niente lld installato).
#
# Uso:
#   ./build.sh <mipsel-linux-gcc>        # es. .../mips32el--uclibc--stable/bin/mipsel-linux-gcc
#   ./build.sh <mipsel-linux-gcc> mipseb # arch alternativa nel nome del .so
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
CC="${1:?serve il cross-compiler uClibc, es. mipsel-linux-gcc (vedi header)}"
ARCH="${2:-mipsel}"
OUT="$HERE/../../binaries/httpd_trigger.$ARCH.so"
"$CC" -shared -fPIC -O2 -o "$OUT" "$HERE/httpd_trigger.c"
echo "-> $OUT"
file "$OUT"
