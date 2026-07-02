# HANDOFF — supremo-autismo-emulator

Stato al 2026-07-01. Da riprendere domani.

## Cosa è stato fatto

Implementata la reimplementazione `fae` descritta in `03_reimplementazione.md`:
pipeline completa che **estrae → rileva arch → costruisce immagine (no root) →
boota in QEMU → inferisce la rete → verifica web**. CLI: `./sae run <firmware>`.

### File nuovi/scritti
- `sae` — launcher a root repo (`./sae run firmwares/mod10.bin`)
- `reimpl/fae/extract.py` — wrapper binwalk v3 → dir rootfs (path da `$PATH`, no hardcode)
- `reimpl/fae/arch.py` — arch/endian via `file` su bin/sbin (→ mipseb/mipsel/armel)
- `reimpl/fae/verify.py` — verifica web rootless via user-net/slirp (vedi sotto)
- `reimpl/fae/pipeline.py` — orchestratore (init/service detect, tail launcher, 2 boot)
- `reimpl/fae/cli.py` — argparse entrypoint

### File modificati (bug reali trovati bootando davvero mod10.bin)
- `config.py`: path asset corretti (`assets/`, non `analisi/assets/`).
  **`root=/dev/sda` non `/dev/sda1`** — `mke2fs -d` fa un filesystem SENZA tabella
  partizioni, quindi il disco intero È il fs. Con `/dev/sda1` → *kernel panic VFS*.
- `image.py`: split `build` → `prepare` + `seal` (per iniettare il launcher tra i due).
  **debugfs mknod: serve `cd <dir>` + basename** in uno script `-f`, altrimenti l'inode
  viene allocato ma NON linkato in /dev → `/dev/null` mancante → getty/tool falliscono.
  Copia guest-scripts + symlink `/firmadyne/sh`→busybox. Immagine dimensionata
  `total+32MB` (prima 3x = 1GB → mke2fs lentissimo, timeout).
- `qemu.py`: append aggiunge `FIRMAE_NET/NVRAM/KERNEL/ETC=true` (le guest-script ci
  gateano sopra). NIC placeholder da `socket,listen=:200x` → `user,restrict=on`
  (i socket collidevano: "Address already in use", stato globale/bug #7).
  Aggiunto path `user_net` rootless con slirp sulla subnet del guest.
- `netinfer.py`: **regex porte** aggiornata. Il kernel 4.1.17 istrumentato logga
  `inet_bind[...]: proto:SOCK_STREAM, port:80` (SENZA `ip:port: 0x..:`), formato diverso
  da quello che il makeNetwork.py originale (e lo scheletro) si aspettava.

## mod10.bin — dove siamo arrivati

`./sae run firmwares/mod10.bin` funziona end-to-end fino alla verifica:
- arch: **mipseb** (TP-Link, uImage MIPS32 BE, squashfs)
- init: `/etc/rc.d/rcS`  · service: `/usr/bin/httpd`
- **boota davvero**: kernel 4.1.17, rootfs monta, rcS gira, e nel serial log:
  - `inet_bind[PID:99 (httpd)]: SOCK_STREAM port:80`  → **httpd è vivo e ascolta**
  - anche `dropbear` :22, e :1900
- inferenza: `network_type=default`, `ip=192.168.0.1`, `ports=[80,22,1900,udp]`
  - la rete cade a `default` perché il firmware NON assegna IP a eth0 da solo:
    `ifconfig: SIOCSIFHWADDR: Cannot assign requested address` (eth0 up ma senza IP).
    Quindi al verify boot è `network.sh` (network_type=default) a mettere br0=192.168.0.1.

Log di riferimento: `scratch/1/qemu.initial.serial.log` (48k righe, boot completo).
Ri-parsarlo offline: `cd reimpl && python3 -c "from fae import netinfer; print(netinfer.infer(open('../scratch/1/qemu.initial.serial.log','rb').read(),'eb'))"`

## IL punto aperto (da fare domani) ⬅️

**La verifica web end-to-end NON è ancora dimostrata.** L'ostacolo è di rete:

- Il firmware usa un **IP LAN statico (192.168.0.1)**. FirmAE usa un TAP host per
  raggiungerlo → richiede **root/CAP_NET_ADMIN**. Qui **non abbiamo root né sudo**.
- Soluzione rootless implementata in `qemu.py` (`user_net=True`): slirp configurato
  con `net=192.168.0.0/24,host=192.168.0.2` e `hostfwd=tcp::8080-192.168.0.1:80`,
  così slirp instrada verso l'IP statico del guest senza TAP. **CODICE PRONTO MA
  NON TESTATO** — l'ultimo boot di prova non è partito (interazione con l'hook di shell
  sul `&`, `/tmp/v.log` mai creato).

### Prossimo passo concreto
1. Boot di verifica in foreground e curl:
   ```
   cd supremo-autismo-emulator
   qemu-system-mips -m 256 -M malta -kernel assets/binaries/vmlinux.mipseb.4 \
     -drive if=ide,format=raw,file=scratch/1/image.ext2 \
     -append "root=/dev/sda console=ttyS0 rdinit=/firmadyne/preInit.sh rw FIRMAE_NET=true FIRMAE_NVRAM=true FIRMAE_ETC=true firmadyne.syscall=1" \
     -serial file:/tmp/v.log -display none \
     -device e1000,netdev=net0 \
     -netdev "user,id=net0,net=192.168.0.0/24,host=192.168.0.2,hostfwd=tcp::8080-192.168.0.1:80" &
   # aspetta ~90s che rcS+network.sh alzino br0, poi:
   curl -v http://127.0.0.1:8080/
   ```
2. Se slirp NON raggiunge 192.168.0.1 (probabile problema: eth0 è enslaved a br0,
   verificare che slirp faccia ARP verso l'IP di br0 e non di eth0; provare hostfwd
   verso l'IP e verificare `ip addr` nel guest via `/firmadyne/debug.sh` con nc:31337).
   Fallback: se c'è root disponibile domani, usare il path TAP (già in `qemu.py`,
   `user_net=False`) — è la via che FirmAE usa e che funziona con IP statici.
3. Una volta che curl risponde, `verify.verify_web` ritorna True e `./sae run` chiude
   con `web=OK`.

## Note / debito (ponytail)
- `verify.py` è solo rootless/user-net. Il path TAP (root) è in `qemu.py` ma non
  cablato in `verify.py` — aggiungere uno switch `--tap` quando serve IP statico + root.
- arbitration.py non implementato (le 5 strategie FIRMAE_*): per ora i flag sono
  sempre true nell'append. YAGNI finché il boot base non è verde.
- `inferFile` è una lista statica di candidati init/service (in `pipeline.py`), non lo
  scan completo di inferFile.sh. Ha funzionato per mod10 (rcS + /usr/bin/httpd).
- Immagine `scratch/1/image.ext2` già costruita con `network_type=default` — riusabile
  per i test di verify senza rifare estrazione+build.
- C'è un'istanza del **FirmAE originale** che gira in parallelo da
  `~/Works/iot-sec-router-void/FirmAE/` (usa socket netdev :2001-2003): innocua per noi
  ora che usiamo user-net, ma se torni ai socket netdev, occhio ai conflitti di porta.

## Comandi utili
```
./sae run firmwares/mod10.bin --infer-timeout 150 --check-timeout 100   # full
cd reimpl && python3 -m fae.netinfer                                     # self-check parser
debugfs -R "cat /firmadyne/network_type" scratch/1/image.ext2           # ispeziona immagine
debugfs -R "ls -l /dev" scratch/1/image.ext2                            # verifica device node
```
