# 01 — Architettura di FirmAE (stato attuale)

Ricostruita leggendo `run.sh`, `firmae.config`, `scripts/*`, `sources/*`, `database/schema`.

## 1. Idea di fondo

Emulare firmware Linux embedded (router/IP-cam MIPS e ARM) su QEMU-system con:

- **kernel custom istrumentati** (`binaries/vmlinux.*`, `zImage.armel`) che loggano su
  serial le syscall di rete rilevanti (`__inet_insert_ifa`, `inet_bind`,
  `ioctl_SIOCSIFHWADDR`, `br_add_if`, `register_vlan_dev`, …) — patch stile Firmadyne;
- una **libnvram** (`LD_PRELOAD`) che finge una NVRAM reale rispondendo alle chiamate
  proprietarie dei vendor;
- **5 arbitration** (flag in `firmae.config`) che al fallimento della prima emulazione
  provano riparazioni successive:
  `FIRMAE_BOOT`, `FIRMAE_NET`, `FIRMAE_NVRAM`, `FIRMAE_KERNEL`, `FIRMAE_ETC`.

Rispetto a Firmadyne (16% success) FirmAE dichiara ~79% grazie a queste arbitration.

## 2. Pipeline end-to-end

Entry point `run.sh` (modalità: `-r` run, `-c` check, `-a` analyze, `-d` debug, `-b` boot),
richiede root. Per ogni firmware chiama `run_emulation()`:

```
firmware.bin
  │
  ├─(1) EXTRACT ─────────── sources/extractor/extractor.py (binwalk v3 rust)
  │        rootfs → images/<iid>.tar.gz     kernel → images/<iid>.kernel
  │        registra iid/brand/hash nel DB PostgreSQL
  │
  ├─(2) ARCH DETECT ─────── scripts/getArch.py
  │        ispeziona bin/sbin/busybox col comando `file` → {mipseb,mipsel,armel}
  │        scripts/inferKernel.py → versione kernel + init= dal binario kernel
  │
  ├─(3) DB INVENTORY ────── scripts/tar2db.py
  │        MD5 di ogni file → tabelle object / object_to_image (dedup)
  │
  ├─(4) BUILD IMAGE ─────── scripts/makeImage.sh   (★ punto critico, vedi §4)
  │        qemu-img create raw 1G → fdisk → mkfs.ext2 → losetup/mount
  │        estrae tarball, chroot busybox:
  │           scripts/inferFile.sh  → trova init (rcS/preinit) + servizio web
  │           scripts/fixImage.sh   → crea /dev nodes, /etc mancanti, disabilita reboot
  │        installa /firmadyne/{busybox,console,libnvram*,gdb,strace,preInit.sh,
  │                              network.sh,run_service.sh}
  │
  ├─(5) NET INFER ───────── scripts/makeNetwork.py  (★ cuore, 765 righe, vedi §5)
  │        boot QEMU di test (TIMEOUT s) con rdinit=/firmadyne/preInit.sh
  │        parsa qemu.initial.serial.log → interfacce/IP/MAC/bridge/VLAN/porte
  │        euristica checkNetwork() → network_type ∈
  │           {normal, reload, bridge, bridgereload, default, None}
  │        genera scratch/<iid>/run.sh (comando QEMU finale + setup TAP host)
  │        scripts/inferDefault.py → semina i default NVRAM se mancanti
  │
  ├─(6) VERIFY ──────────── scripts/check_emulation.sh
  │        lancia run.sh in bg, poi firmae.config:check_network()
  │        prova ping + curl http/https sugli IP inferiti per CHECK_TIMEOUT s
  │        scrive scratch/<iid>/{ping,web,ip,result}
  │
  └─(7) MODE-SPECIFIC
           analyze → analyses/analyses_all.sh (pentest, Metasploit/custom)
           debug   → run_debug.sh + debug.py (nc:31337, telnet:31338)
           boot    → QEMU con -s -S per gdb-multiarch
```

I tempi di ogni fase finiscono in `scratch/<iid>/time_*` (strumentazione benchmark del paper).

## 3. Runtime dentro il guest

`preInit.sh` (rdinit) monta sysfs/proc/devpts/tmpfs, poi la coda appesa da `makeNetwork.py`
lancia in sequenza:
- `/firmadyne/network.sh &` — configura bridge/IP/route in base a `network_type`
  (state machine su 5 casi, con workaround per modelli specifici Netgear/D-Link/TP-Link);
- `/firmadyne/run_service.sh &` — avvia e **riavvia in loop** il web server rilevato;
- `/firmadyne/debug.sh` — in debug mode espone nc/telnet;
- `busybox sleep 36000` — tiene vivo l'init.

La libnvram intercetta via `LD_PRELOAD` (iniettato dal kernel patchato in `init/main.c`):
storage key-value su tmpfs `/firmadyne/libnvram/`, default da `config.h` (es.
`lan_ipaddr=192.168.0.50`, MAC `01:23:45:67:89:ab`), override per-immagine in
`/firmadyne/libnvram.override/`. `alias.c` fornisce ~30 shim vendor (Broadcom `nvram_*`,
Netgear ACOS `acosNvramConfig_*`, D-Link artblock, Realtek apmib, ZyXel/Edimax envram con
semantica di ritorno invertita).

## 4. Costruzione immagine (il punto fragile)

`makeImage.sh` costruisce un ext2 da 1 GB tramite:
`qemu-img create` → `fdisk` (via `echo -e "o\nn\np\n1..."`) → `add_partition` (=`losetup -Pf`
con **busy-wait** finché il device `pN` compare) → `mkfs.ext2` → `mount` → `tar -xf` →
`chroot busybox ash` per `inferFile.sh`+`fixImage.sh` → `umount` → `del_partition`
(`losetup -d` + `dmsetup remove`) → `e2fsck`.

Tutto questo richiede **root**, un **loop device globale** condiviso tra run concorrenti,
e sequenze `sleep 1; sync` senza garanzie. `add_partition`/`del_partition` in
`firmae.config` sono polling loop su `losetup` output parsato con `awk`. È qui che nascono
metà dei bug di concorrenza (vedi `02_bug_criticita.md`).

## 5. Inferenza di rete (il cuore, `makeNetwork.py`)

Funzioni di parsing del serial log (regex su righe `firmadyne: <fn>[...]: ...`):

| Funzione | Estrae |
|----------|--------|
| `findNonLoInterfaces` | `(iface, ip)` da `__inet_insert_ifa` (esclude 127.0.0.1 / 0.0.0.0) |
| `findMacChanges` | `(iface, mac)` da `ioctl_SIOCSIFHWADDR` |
| `findPorts` | `(proto, ip, port)` da `inet_bind` |
| `findIfacesForBridge` | membri di un bridge da `br_add_if`/`br_dev_ioctl` |
| `findVlanInfoForDev` | vlan id da `register_vlan_dev` |

`getNetworkList` combina interfacce↔bridge↔vlan↔mac in tuple
`(ip, dev, vlan, mac, brif)`. `checkNetwork` applica euristiche per capire la topologia e
rimuovere IP DHCP (`10.0.2.x`, `.190` per Netgear R6900) → `network_type`. `qemuCmd`
templatizza lo script finale: setup TAP host (`tunctl`, `ip link`, `ip addr`, VLAN),
comando `qemu-system-*`, teardown. Endianness gestita con `struct.pack('>I'|'<I')`.

Modalità **user-net** (DHCP): invece del TAP usa `-netdev user` con `hostfwd` sulle porte
trovate (80/443 + quelle da `inet_bind`).

## 6. Formato dati (blackboard su filesystem)

Tutto lo stato di un run vive in `scratch/<iid>/` come file singolo-valore:
`name`, `brand`, `architecture`, `init`, `current_init`, `service`, `ip`, `ip.<n>`,
`ip_num`, `isDhcp`, `ping`, `web`, `result`, `network_type`, `time_*`, `run.sh`,
`qemu.initial.serial.log`, `qemu.final.serial.log`, `image.raw`, `image/` (mountpoint).

Il **PostgreSQL** (`database/schema`) tiene solo: `brand`, `image` (iid, filename, hash,
arch, kernel_version, flag estrazione), `object`/`object_to_image` (dedup file), `product`.
In pratica è una blackboard sovradimensionata: gli unici campi letti a runtime sono
arch/iid/brand — il resto è telemetria del paper.

## 7. Binari riutilizzabili (da NON reimplementare)

`binaries/`: `vmlinux.{mipseb,mipsel}.{2,4}` (kernel 2.6/4.x istrumentati),
`zImage.armel`+`vmlinux.armel`, `busybox.{arch}`, `console.{arch}`, `libnvram[_ioctl].so.{arch}`,
`gdb`/`gdbserver`/`strace`.{arch}, `busybox.x86_64` (per il chroot in build).
Questi sono il vero output del reverse engineering KAIST e vanno tenuti as-is.
</content>
