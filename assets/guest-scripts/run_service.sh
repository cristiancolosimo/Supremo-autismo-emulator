#!/firmadyne/sh

BUSYBOX=/firmadyne/busybox
BINARY=`${BUSYBOX} cat /firmadyne/service`
BINARY_NAME=`${BUSYBOX} basename ${BINARY}`

if (${FIRMAE_ETC}); then
  # attesa di assestamento prima di forzare il servizio web. 120s erano eterni su
  # MIPS emulato (minuti wall-clock). Default 30s; override via cmdline: SERVICE_DELAY=N
  ${BUSYBOX} sleep ${SERVICE_DELAY:-30}
  $BINARY &

  # fallback GUI statica: alcuni web daemon (es. httpd TP-Link) dipendono da un demone
  # di config SoC (cos/dconf) che su QEMU non completa → non bindano la :80. Se dopo un
  # po' nessuno ascolta sulla :80 e abbiamo asset statici, li serviamo col busybox: la
  # UI si vede (il backend dinamico resta monco). Se il daemon vero sale, il bind qui
  # fallisce e basta. ponytail: porta fissa 80, docroot da /firmadyne/web_static.
  WEB=`${BUSYBOX} cat /firmadyne/web_static 2>/dev/null`
  if [ -n "$WEB" ] && [ -d "$WEB" ]; then
    ( ${BUSYBOX} sleep `${BUSYBOX} expr ${SERVICE_DELAY:-30} + 20`
      if ( ! (${BUSYBOX} netstat -ltn 2>/dev/null | ${BUSYBOX} grep -qE ':80[^0-9]') ); then
          ${BUSYBOX} httpd -p 80 -h "$WEB"
      fi ) &
  fi

  while (true); do
      ${BUSYBOX} sleep 10
      if ( ! (${BUSYBOX} ps | ${BUSYBOX} grep -v grep | ${BUSYBOX} grep -sqi ${BINARY_NAME}) ); then
          $BINARY &
      fi
  done
fi
