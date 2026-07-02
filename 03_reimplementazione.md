# 03 — Reimplementazione: architettura target `fae`

Obiettivo: stesso risultato di FirmAE (emulare firmware, inferire rete, verificare web),
ma **un solo linguaggio tipizzato**, **senza root per la build**, **senza race condition**,
**testabile**. Riuso integrale dei binari (`binaries/`) e delle librerie C (`sources/libnvram`,
`console`) — la reimplementazione è **solo dell'orchestrazione**.

## Principi

| # | Decisione | Perché |
|---|-----------|--------|
| 1 | **Python 3.11+ tipizzato**, niente bash generato | Un flusso, uno stack trace, un debugger. Fine di bash→bash→python. |
| 2 | **`mke2fs -d rootfs/ image.ext2`** invece di losetup+mount+chroot | Popola l'ext2 da una directory. **Niente root, niente loop device, niente race** (bug #7-10). L'estrazione binwalk già produce una directory: la si patcha come dir normale e la si sigilla in un colpo. Le patch che servivano `chroot` (creare /dev, /etc) diventano semplici `os.makedirs`/`mknod` in un albero posseduto dall'utente (mknod device richiede comunque `fakeroot` o cap; vedi §Immagine). |
| 3 | **`subprocess.Popen` + QMP** per QEMU | Handle reale del processo: `.terminate()`/`.kill()`, niente `ps\|grep` (bug #5). QMP socket per shutdown pulito e query stato. |
| 4 | **Niente PostgreSQL** | `state.json` per-run (blackboard tipizzata) + SQLite opzionale solo per dedup file cross-immagine. Zero credenziali, zero SQL injection (bug #4,#23). |
| 5 | **Arbitration come strategie esplicite** | Ogni arbitration è un oggetto `Strategy` con `applies()`/`apply()`, provate in ordine. Testabili singolarmente. |
| 6 | **Parser di rete tipizzato** | I 5 estrattori regex → un modulo `netinfer` con dataclass `Interface/Bridge/Vlan/Port/NetPlan` e euristiche isolate (bug #11-16). |
| 7 | **Cleanup idempotente e context-managed** | `with EmulationRun(...)` garantisce teardown di QEMU/TAP/tmp anche su eccezione (bug: cleanup non idempotente). |
| 8 | **Test dai log reali** | I `qemu.*.serial.log` esistenti in `scratch/` diventano fixture: il parser di rete si testa offline senza bootare nulla. |

## Struttura pacchetto (`reimpl/fae/`)

```
fae/
  config.py     # dataclass Config (arch table, kernel/qemu map, timeout) — sostituisce firmae.config
  models.py     # dataclass: Interface, Port, NetPlan, RunState, Arch — la blackboard tipizzata
  extract.py    # wrapper binwalk (path da $PATH o config, non hardcoded) → rootfs dir + kernel
  arch.py       # rileva arch/endian da bin/sbin col comando `file` (porting getArch.py, no SQL)
  image.py      # costruisce image.ext2 via mke2fs -d (NO mount/root); applica patch fixImage
  qemu.py       # QemuProcess: Popen + QMP, lifecycle sicuro, cattura serial log
  netinfer.py   # parsing serial log → NetPlan; euristiche checkNetwork (porting makeNetwork.py)
  verify.py     # ping + http/https check con handle processo (porting check_emulation.sh)
  arbitration.py# 5 strategie di fallback come oggetti Strategy
  pipeline.py   # orchestratore: extract→arch→image→infer→verify, con RunState
  cli.py        # entrypoint: fae run|check|analyze|debug <brand> <firmware>
```

Solo i file `netinfer.py`, `image.py`, `qemu.py`, `models.py` sono forniti come scheletro
funzionante di partenza (il resto è porting meccanico del corrispettivo FirmAE).

## Mapping componente vecchio → nuovo

| FirmAE | `fae` | Nota |
|--------|-------|------|
| `run.sh` | `cli.py` + `pipeline.py` | orchestrazione tipizzata |
| `firmae.config` | `config.py` | tabelle arch/kernel/qemu come dict tipizzati |
| `extractor.py` | `extract.py` | binwalk path da config; output = directory |
| `getArch.py` + `inferKernel.py` | `arch.py` | no DB |
| `tar2db.py` + PostgreSQL | *(rimosso)* / SQLite opz. | dedup file non serve al run base |
| `makeImage.sh` + `fixImage.sh` + `inferFile.sh` | `image.py` | mke2fs -d, patch come op su directory |
| `makeNetwork.py` | `netinfer.py` + `qemu.py` | parser isolato dal lancio QEMU |
| `check_emulation.sh` + `firmae.config:check_network` | `verify.py` | handle processo |
| `run.{arch}.sh` / template QEMU | `qemu.py` | un builder di command line |
| 5 flag `FIRMAE_*` | `arbitration.py` | strategie |
| binari `binaries/`, `sources/{libnvram,console}` | **invariati** | riuso diretto |

## Il nodo `mknod` / device node

`fixImage.sh` crea device node (`/dev/mtd*`, `/dev/console`, ttyS1…) via `mknod`, che richiede
`CAP_MKNOD`. Con `mke2fs -d` ci sono due strade pulite senza root globale:

1. **`mke2fs` + file di spec device**: si genera l'ext2 e si iniettano i nodi con `debugfs`
   (`debugfs -w -R 'mknod /dev/console c 5 1' image.ext2`) — nessun mount, nessun root.
2. **`fakeroot mke2fs -d`**: i `mknod` nell'albero avvengono in ambiente fakeroot e vengono
   serializzati nell'ext2. Zero privilegi reali.

Scelta consigliata: **debugfs** per pochi nodi noti (deterministico, auditabile). È il
rimpiazzo diretto e sicuro dell'intero balletto losetup/mount/chroot/mknod.

## Ottimizzazioni di performance

- **Build immagine**: da `qemu-img create 1G` + mkfs + mount → `mke2fs -d` dimensionato sul
  contenuto reale (+margine). Meno I/O, immagini più piccole, niente `e2fsck` di riparazione.
- **`fixImage` directory-scan**: la scansione `strings` su ogni binario (bug #13) → parser
  ELF mirato solo sui binari init-critici, o cache dei path già visti.
- **Parallelismo**: senza loop device globale, N firmware emulano davvero in parallelo
  (TAP già per-IID, ora anche build e QEMU isolati). Prima era il collo di bottiglia.
- **libnvram** (bug #19): sostituire il `sprintf` byte-per-byte con `fread` a blocchi
  (patch C mirata, non riscrittura).

## Cosa resta fuori dallo scheletro (YAGNI finché non serve)

- `analyses/` (pentest post-emulazione): è un layer separato, si aggancia dopo che `verify`
  dà web=true. Non tocca il core.
- `scraper/` (raccolta firmware): tool indipendente, fuori scope.
- SQLite dedup: da aggiungere solo se si emulano dataset grandi e serve deduplicare i file.
- Supporto arch oltre `{mipseb,mipsel,armel}`: le stesse tre di FirmAE; `arm64`/`mips64`
  quando ci sarà un kernel istrumentato per esse.
</content>
