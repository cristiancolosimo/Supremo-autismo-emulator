# Asset preservati da FirmAE

Tutto ciò che la reimplementazione riusa e che **non è ricreabile dalla sola documentazione**.
Copiato qui perché la cartella `FirmAE/` non sarà più disponibile.

## `binaries/` — output del reverse engineering KAIST (riuso diretto, NON reimplementare)

| File | Cosa | Serve per |
|------|------|-----------|
| `vmlinux.mipseb.{2,4}`, `vmlinux.mipsel.{2,4}` | kernel MIPS istrumentati (2.6 e 4.x) | boot + log rete (`firmadyne: fn[...]`) |
| `vmlinux.armel`, `zImage.armel` | kernel ARM istrumentato | boot ARM |
| `libnvram.so.{armel,mipseb,mipsel}` | NVRAM fake via LD_PRELOAD | far bootare firmware con NVRAM proprietaria |
| `libnvram_ioctl.so.*` | variante ioctl della libnvram | firmware che usano ioctl NVRAM |
| `busybox.{armel,mipseb,mipsel}` | busybox guest per arch | runtime `/firmadyne/busybox` |
| `busybox.x86_64` | busybox host | usato in build (era il chroot) |
| `console.*` | bind shell su ttyS1 | console seriale guest |
| `gdbserver.*`, `strace.*` | debug/trace guest-side | modalità debug/analyze |
| `unstuff` | estrattore formati compressi | usato da `sources/extractor` |

## `sources/` — sorgenti C ricompilabili

- `libnvram/` — sorgente completo (`nvram.c`, `alias.c`, `config.h`, `Makefile`). Ricompila con:
  `make CC=<cross-gcc-arch>` (vedi `Makefile`). **Applicare i fix S1/S4 di
  [../02_bug_criticita.md](../02_bug_criticita.md)** (strcpy→strlcpy, system()→execve, DEBUG off,
  sprintf byte-per-byte→fread) prima di rifare i `.so`.
- `console/` — `console.c` (bug #1: parentesi in `open()` da correggere alla ricompilazione).
- `extractor/` — `extractor.py`: logica di estrazione binwalk v3. Da avvolgere in `fae/extract.py`
  correggendo il path binwalk hardcoded (bug #15) → prenderlo da `$PATH`/config.

## `guest-scripts/` — asset runtime iniettati nel guest (statici, si copiano as-is)

`preInit.sh` (rdinit), `network.sh` (state machine bridge/IP), `run_service.sh` (respawn web),
`inferFile.sh` (trova init+servizio nel chroot), `fixImage.sh` (patch rootfs), `injectionChecker.sh`.
`fae/image.py` li installa in `/firmadyne/`; `network.sh`/`run_service.sh` vanno ripuliti dai
loop infiniti (bug #17,#18) ma la logica di config resta valida.

## `reference/` — solo per il porting meccanico (non asset runtime)

`firmae.config`, `database/schema`, e gli script Python/bash originali
(`getArch.py`, `inferKernel.py`, `inferDefault.py`, `tar2db.py`, `util.py`,
`check_emulation.sh`, `makeNetwork.py`, `makeImage.sh`) come riferimento durante il porting di
`arch.py`/`extract.py`/`verify.py`/`pipeline.py`.

## `fixtures/` — dati reali per test offline

- `dlink_originale.initial.serial.log` — serial log reale di un boot istrumentato riuscito
  (D-Link `originale.bin`). Usalo per validare `fae/netinfer.py` **senza bootare QEMU**.
- `makeNetwork.log` — output atteso dell'inferenza per quel firmware (network_type=default, web ok).

## Cosa NON è stato copiato (e perché va bene)

- `gdb.{armel,mipseb,mipsel}` (~120M): debugger **host-side**, solo per debug interattivo manuale.
  Riscaricabile / ricompilabile; non serve al ciclo emula→inferisci→verifica.
- `sources/scraper/`: raccolta firmware da internet, fuori scope.
- `core/Dockerfile`: la containerizzazione si rifà pulita (senza `--privileged`, ora che la build
  non richiede root).

## Verifica rapida di completezza

```bash
ls analisi/assets/binaries/vmlinux.* analisi/assets/binaries/libnvram.so.* \
   analisi/assets/binaries/busybox.* analisi/assets/guest-scripts/*.sh
```
Se questi ci sono, hai il necessario per: build immagine, boot, inferenza rete, verifica.
</content>
