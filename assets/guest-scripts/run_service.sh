#!/firmadyne/sh

BUSYBOX=/firmadyne/busybox
BINARY=`${BUSYBOX} cat /firmadyne/service`
BINARY_NAME=`${BUSYBOX} basename ${BINARY}`

if (${FIRMAE_ETC}); then
  # attesa di assestamento prima di forzare il servizio web. 120s erano eterni su
  # MIPS emulato (minuti wall-clock). Default 30s; override via cmdline: SERVICE_DELAY=N
  ${BUSYBOX} sleep ${SERVICE_DELAY:-30}
  $BINARY &

  while (true); do
      ${BUSYBOX} sleep 10
      if ( ! (${BUSYBOX} ps | ${BUSYBOX} grep -v grep | ${BUSYBOX} grep -sqi ${BINARY_NAME}) ); then
          $BINARY &
      fi
  done
fi
