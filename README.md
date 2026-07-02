# supremo-autismo-emulator (`sae`)

Emulatore di firmware IoT/router basato su QEMU. Estrae un firmware, ne rileva
l'architettura, costruisce un'immagine di disco avviabile **senza root**, boota il
kernel istrumentato, **inferisce la configurazione di rete** leggendo il serial log e
**verifica che l'interfaccia web del dispositivo risponda**.

È una reimplementazione pulita e tipizzata di [FirmAE](https://github.com/pr0v3rbs/FirmAE)
(KAIST, ACSAC 2020): stesse tecniche di arbitration e gli stessi binari istrumentati, ma
orchestrazione in un solo linguaggio (Python 3.11+), senza mount/loop-device, senza
PostgreSQL, senza kill di processi via `ps | grep`. Vedi [§Contesto](#contesto-e-analisi).

```console
$ ./sae run firmwares/mod10.bin
[*] firmware: firmwares/mod10.bin
[*] rootfs:   scratch/mod10/extract/.../squashfs-root
[*] arch:     mipseb (eb)
[*] init:     /etc/rc.d/rcS
[*] service:  /usr/bin/httpd (httpd)
[*] image:    scratch/mod10/image.ext2 (49152 KiB)
[*] inference boot (max 240s, early-exit ad assestamento)...
[*] rete assestata a ~34s (18 eventi)
[*] network:  default  ips=['192.168.0.1']  ports=[80, 22, 1900]
[*] verify boot (user-net rootless, max 180s)...
[*] web:      True

=== RESULT ===
arch=mipseb network=default ip=192.168.0.1 web=OK
```

---

## Indice

- [Come funziona](#come-funziona)
- [Requisiti](#requisiti)
- [Installazione](#installazione)
- [Quickstart](#quickstart)
- [Comandi e opzioni](#comandi-e-opzioni)
- [Rete: rootless vs TAP](#rete-rootless-vs-tap)
- [Editare il filesystem del guest](#editare-il-filesystem-del-guest)
- [Cache e artefatti](#cache-e-artefatti)
- [Output ed exit code](#output-ed-exit-code)
- [Troubleshooting](#troubleshooting)
- [Struttura del progetto](#struttura-del-progetto)
- [Contesto e analisi](#contesto-e-analisi)

---

## Come funziona

La pipeline (`reimpl/fae/pipeline.py`) è un orchestratore a stadi con stato tipizzato
(`RunState` → `state.json`). Ogni run fa **due boot**:

```
firmware.bin
   │  extract.py   binwalk v3 → directory rootfs (file tuoi, editabili)
   ▼
 rootfs/
   │  arch.py      `file` sui binari → mipseb | mipsel | armel
   │  image.py     mke2fs -d + debugfs mknod → image.ext2   (NO root, NO mount, NO loop)
   ▼
 image.ext2
   │  ┌─ boot #1 (inferenza) ─────────────────────────────────────────────┐
   │  │ QEMU + kernel istrumentato. Il vero init del firmware alza le       │
   │  │ interfacce; il kernel logga inet_bind / SIOCSIFHWADDR / br_add.     │
   │  │ netinfer.py parsa il serial log → IP, MAC, bridge, VLAN, porte.     │
   │  └─────────────────────────────────────────────────────────────────────┘
   │  ┌─ boot #2 (verify) ────────────────────────────────────────────────┐
   │  │ Applica la rete inferita, poi curl sull'interfaccia web.            │
   │  └─────────────────────────────────────────────────────────────────────┘
   ▼
 RESULT: arch / network / ip / web=OK|FAIL   +   scratch/<slug>/state.json
```

Il boot #1 esce in anticipo (`early-exit`) appena le righe di rete nel log smettono di
crescere: niente attesa a timeout fisso.

---

## Requisiti

| Comando | Pacchetto (Fedora / Arch) | Serve per |
|---|---|---|
| `python3` ≥ 3.11 | `python3` / `python` | orchestratore (solo stdlib) |
| `qemu-system-mips`, `-mipsel`, `-arm` | `qemu-system-mips`, `qemu-system-arm` | emulazione |
| `mke2fs`, `debugfs` | `e2fsprogs` | build immagine ext2 senza root |
| `file` | `file` | rileva architettura/endianness |
| `binwalk` **v3** | `cargo install binwalk` | estrazione firmware (obbligatoria la v3 Rust) |
| `ip` | `iproute` / `iproute2` | solo per il TAP (`setup-tap.sh`) |
| `telnet` | `telnet` / `inetutils` | shell di debug (`--keep-alive`) |
| `curl` | `curl` | test manuale del web |

Verifica tutto in un colpo:

```bash
./check-deps.sh            # report OK/MISS
./check-deps.sh --pkgs     # stampa il comando d'installazione per la tua distro
```

> **Nota su binwalk:** serve la v3 in Rust (`cargo install binwalk`), non la vecchia v2
> Python. `check-deps.sh` lo verifica. `sae` prende il binario da `$PATH` (fallback
> `~/.cargo/bin/binwalk`), mai hardcoded.

---

## Installazione

```bash
git clone <repo> supremo-autismo-emulator
cd supremo-autismo-emulator
./check-deps.sh                        # cosa manca
./check-deps.sh --pkgs | sh            # (opzionale) esegui direttamente l'install
```

Nessun `pip install`: il core usa solo la stdlib. Il launcher `./sae` mette
`reimpl/` sul path ed esegue `fae.cli`.

---

## Quickstart

```bash
# emulazione completa end-to-end
./sae run firmwares/mod10.bin

# se il boot risponde web=OK il dispositivo è emulato e la sua UI è raggiungibile
# (in modalità rootless è forwardata su http://127.0.0.1:8080/ — vedi §Rete)
```

Aiuto ed esempi sempre a portata di mano:

```bash
./sae -h            # panoramica + esempi (incluso editing offline/runtime)
./sae run -h        # dettaglio di tutti i flag di `run`
```

---

## Comandi e opzioni

Sottocomando unico: **`run`**.

```
./sae run <firmware> [opzioni]
```

| Opzione | Default | Descrizione |
|---|---|---|
| `firmware` | — | path del `.bin` da emulare (posizionale) |
| `--brand B` | `auto` | etichetta brand del dispositivo |
| `--iid N` | `1` | id istanza (per run multipli) |
| `--root PATH` | repo | radice del progetto (dove stanno `assets/`) |
| `--infer-timeout S` | `240` | tetto massimo del boot d'inferenza (esce prima ad assestamento) |
| `--check-timeout S` | `180` | tetto massimo del boot di verify web |
| `--no-reuse` | off | rifà il boot d'inferenza anche se il serial log è in cache |
| `--rebuild` | off | butta **tutta** la cache del run (extract + immagine + log) e riparte pulito |
| `--tap NAME` | off | verify su IP LAN statico via TAP pre-creato (vedi §Rete); senza, usa user-net rootless |
| `--keep-alive` | off | non fa il verify one-shot: tiene l'emulazione **viva** con shell root; Ctrl-C per fermare |

### Esempi

```bash
# firmware lento a bootare: timeout più generosi
./sae run firmwares/mod10.bin --infer-timeout 300 --check-timeout 240

# forza tutto da zero (dopo aver sostituito il .bin, o per debug)
./sae run firmwares/mod10.bin --rebuild

# rifà solo l'inferenza tenendo estrazione e immagine in cache
./sae run firmwares/mod10.bin --no-reuse

# tieni il dispositivo vivo per esplorarlo (browser + shell)
./sae run firmwares/mod10.bin --keep-alive
```

---

## Rete: rootless vs TAP

Il firmware espone i suoi servizi su un IP LAN, spesso **statico** (es. `192.168.0.1`).
Ci sono due modi per raggiungerlo dall'host.

### user-net (default, rootless)

Nessun privilegio richiesto. QEMU usa slirp con la subnet del guest e forwarda le porte
su `127.0.0.1` con offset alto:

| Porta guest | Porta host |
|---|---|
| 80 | 8080 |
| 443 | 8443 |
| 23 (telnet) | 20023 |
| 22 (ssh) | 20022 |
| altre `p` (< 45000) | `20000 + p` |
| shell di debug (31338) | 51338 |

```bash
./sae run firmwares/mod10.bin --keep-alive
# poi dall'host:
curl http://127.0.0.1:8080/          # interfaccia web del dispositivo
telnet 127.0.0.1 51338               # shell root nel guest (senza login)
```

> Limite: funziona se il servizio ascolta su `0.0.0.0`. Alcuni firmware bindano solo
> sull'IP LAN specifico — in quel caso usa il TAP.

### TAP (IP statico, richiede root una volta)

Raggiungi il guest direttamente sul suo IP LAN. Il TAP si crea una volta (rootless per
QEMU, ma la creazione dell'interfaccia richiede privilegi):

```bash
sudo ./setup-tap.sh sae0                 # crea sae0: host=192.168.0.2/24, owner=$USER
./sae run firmwares/mod10.bin --tap sae0

# ora il guest è raggiungibile sul suo IP reale:
curl http://192.168.0.1/
telnet 192.168.0.1 31338                 # shell di debug

sudo ./setup-tap.sh sae0 down            # smonta quando hai finito
```

---

## Editare il filesystem del guest

Esistono due sorgenti di verità: la **directory rootfs** estratta
(`scratch/<slug>/extract/.../`, file normali di tua proprietà) e l'immagine
`image.ext2` montata `rw` nel guest.

### A dispositivo spento (offline)

Modifica i file nella directory rootfs, poi resealla e riavvia:

```bash
vim scratch/mod10/extract/.../etc/passwd
./sae run firmwares/mod10.bin --no-reuse     # prepare+seal ricostruiscono l'ext2 dalla dir
```

La directory persiste tra i run (finché non lanci `--rebuild`).

### A dispositivo vivo (runtime)

```bash
./sae run firmwares/mod10.bin --keep-alive
telnet 127.0.0.1 51338      # (o IP-del-TAP:31338) — la porta è mostrata all'avvio
# ... modifichi i file nel guest ...
sync                        # ⚠️ IMPORTANTE prima di uscire
```

> **Persistenza runtime:** le scritture fuori da tmpfs (`/tmp`, `/var`, `/run`) finiscono
> in `image.ext2`, ma lo shutdown è via SIGTERM: dai **`sync`** nella shell del guest
> prima di Ctrl-C, altrimenti rischi di perdere le modifiche. Restano nell'`image.ext2`;
> la directory rootfs sull'host resta invariata (un successivo `--no-reuse` reseal-erebbe
> dalla dir, sovrascrivendole).

---

## Cache e artefatti

Tutto l'output di un run vive in `scratch/<slug>/`, dove `<slug>` è il nome del `.bin`
senza estensione:

| File | Contenuto |
|---|---|
| `.fingerprint` | sha256 del firmware — invalida la cache se il `.bin` cambia |
| `extract/` | rootfs estratto da binwalk |
| `image.ext2` | immagine di disco avviabile |
| `qemu.initial.serial.log` | serial log del boot d'inferenza (ri-parsabile offline) |
| `qemu.final.serial.log` | serial log del boot di verify |
| `qemu.stderr.log` | stderr di QEMU (diagnostica) |
| `state.json` | `RunState` serializzato (arch, init, servizio, piano di rete, web) |

**Caching intelligente:** i run successivi riusano `extract/` e il serial log
d'inferenza (l'immagine d'inferenza viene sigillata solo se il boot deve girare
davvero). La cache è chiavata sul **contenuto** del firmware: se sostituisci il `.bin`
tenendo lo stesso nome, si invalida da sola. Forza il refresh con `--rebuild` (tutto) o
`--no-reuse` (solo l'inferenza).

Ri-parsare un log offline senza QEMU:

```bash
cd reimpl
python3 -c "from fae import netinfer; \
  print(netinfer.infer(open('../scratch/mod10/qemu.initial.serial.log','rb').read(),'eb'))"

python3 -m fae.netinfer      # self-check del parser di rete
```

---

## Output ed exit code

Ogni run chiude con una riga di riepilogo e un exit code scriptabile:

```
=== RESULT ===
arch=mipseb network=default ip=192.168.0.1 web=OK
```

| Exit code | Significato |
|---|---|
| `0` | web verificato (`web=OK`), oppure `--keep-alive` terminato normalmente |
| `2` | boot/inferenza ok ma il web non risponde (`web=FAIL`) |
| ≠ 0 | errore di pipeline (estrazione fallita, arch non riconosciuta, ecc.) |

---

## Troubleshooting

| Sintomo | Causa probabile | Rimedio |
|---|---|---|
| `binwalk non v3` | installata la v2 Python | `cargo install binwalk`, verifica con `binwalk --version` |
| kernel panic `VFS: unable to mount root` | `root=/dev/sda1` su fs senza tabella partizioni | è già gestito (`root=/dev/sda`); se ricompili, non aggiungere il suffisso partizione |
| `/dev/null` mancante, getty fallisce | device node non linkati | i node sono iniettati via `debugfs`; ispeziona con `debugfs -R "ls -l /dev" scratch/<slug>/image.ext2` |
| `network_type=default`, nessun IP | il firmware non assegna IP a `eth0` da solo | atteso su molti device; `network.sh` mette `br0=192.168.0.1` al verify boot |
| web non risponde in user-net | il servizio binda solo sull'IP LAN | usa `--tap` (vedi §Rete) |
| QEMU esce da solo con `--tap` | TAP inesistente o non di tua proprietà | `sudo ./setup-tap.sh sae0`; controlla `qemu.stderr.log` |
| modifiche runtime perse | shutdown senza sync | `sync` nella shell del guest prima di Ctrl-C |

Ispezione dell'immagine senza montarla:

```bash
debugfs -R "cat /firmadyne/network_type" scratch/<slug>/image.ext2
debugfs -R "ls -l /dev"                   scratch/<slug>/image.ext2
```

---

## Struttura del progetto

```
sae                     launcher (./sae run <firmware>)
check-deps.sh           verifica/installa le dipendenze host
setup-tap.sh            crea/rimuove il TAP per il verify su IP statico

reimpl/fae/
  cli.py                entrypoint argparse (comando `run` + esempi)
  pipeline.py           orchestratore: extract → arch → image → boot×2 → verify
  extract.py            wrapper binwalk v3 → directory rootfs
  arch.py               rilevamento architettura/endianness
  image.py              build ext2 rootless (mke2fs -d + debugfs mknod)
  qemu.py               lifecycle QEMU (Popen + QMP), builder command line, netdev
  netinfer.py           parser del serial log → piano di rete (+ self-check)
  verify.py             verifica web (curl) rootless / via TAP
  models.py             RunState / NetPlan / Interface / Port (→ state.json)
  config.py             tabelle arch/kernel/qemu, path degli asset

assets/                 binari istrumentati, script guest, sorgenti C (vedi assets/ASSETS.md)
firmwares/              i .bin da emulare
scratch/                output per-run (gitignored)
```

---

## Contesto e analisi

Questo repo nasce dall'analisi tecnica di FirmAE e ne è la reimplementazione. I documenti
di analisi restano la reference sul *perché* delle decisioni:

| Documento | Contenuto |
|---|---|
| [01_architettura.md](01_architettura.md) | come funziona FirmAE: pipeline, componenti, kernel/libnvram, formato dati |
| [02_bug_criticita.md](02_bug_criticita.md) | catalogo bug per severità (con `file:riga`), race condition, root cause |
| [03_reimplementazione.md](03_reimplementazione.md) | architettura target `fae`: cosa si tiene, cosa si butta, mapping |
| [assets/ASSETS.md](assets/ASSETS.md) | inventario dei binari istrumentati e sorgenti riusati |
| [HANDOFF.md](HANDOFF.md) | stato di avanzamento e prossimi passi |

**Cosa cambia rispetto a FirmAE:** un solo linguaggio tipizzato al posto di
bash-che-genera-bash; build senza root (`mke2fs -d`/`debugfs` invece di
losetup+mount+chroot, elimina le race su loop-device); lifecycle QEMU esplicito (niente
`kill $(ps aux | grep qemu)`); niente PostgreSQL né credenziali hardcoded (stato in
`state.json`); emulazioni realmente parallelizzabili. Le 5 tecniche di arbitration e i
binari del reverse engineering KAIST sono preservati invariati.
