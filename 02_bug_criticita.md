# 02 — Bug e criticità

Catalogo ordinato per severità. Riferimenti `file:riga` sull'albero FirmAE attuale.
Ogni voce nota la **root cause**, non solo il sintomo.

## S1 — Bloccanti / correttezza

| # | Dove | Problema | Root cause |
|---|------|----------|------------|
| 1 | `sources/console/console.c:17` | `if ((fd = open(...) == -1))` — precedenza: `fd` riceve il *risultato del confronto* (0/1), non il descrittore. `perror` scatta sempre anche a open riuscita, e la console usa un fd sbagliato. | Parentesi mal posizionate. Fix: `if ((fd = open(...)) == -1)`. |
| 2 | `sources/libnvram/nvram.c:296,413` | `strcpy` senza bound check su buffer `BUFFER_SIZE=256`. Chiave/valore > 256 byte → overflow. | Nessuna validazione ai confini. Usare `strlcpy`/`snprintf`. |
| 3 | `sources/libnvram/nvram.c:705` | `system("/bin/cp " OVERRIDE_POINT "* " MOUNT_POINT)` — shell injection / rottura su path con spazi. | Uso di `system()` con path concatenati. |
| 4 | `scripts/getArch.py:62`, `tar2db.py:28,30`, `util.py:34,47` | SQL injection: query costruite con `%` string-format su input non escapato. | Nessuna query parametrizzata. |
| 5 | `check_emulation.sh:56`, `run.sh:257,285`, `delete.sh` | `kill $(ps aux \| grep <qemu> \| awk '{print $2}')` — uccide qualsiasi processo che matcha, anche di altri run/utenti; race sul match. | Nessun handle di processo; PID recuperato per pattern. |
| 6 | `scripts/tar2db.py:109` | `g.groups(1)` invece di `g.group(1)` → parsing iid rotto nel fallback. | Refuso API `re`. |

## S2 — Race condition / concorrenza

| # | Dove | Problema |
|---|------|----------|
| 7 | `firmae.config:add_partition/del_partition`, `makeImage.sh`, `makeNetwork.py:98-105` | Loop device **globale** condiviso: due run concorrenti possono attaccarsi allo stesso `/dev/loopNp1`. `get_device()` fa `ls -t /dev/mapper \| head -1` — prende l'ultimo device *di chiunque*. |
| 8 | `makeImage.sh:59,69` / vari | `sleep 1; sync` come sincronizzazione tra `losetup` e comparsa del device. Fragile su host lenti/carichi. |
| 9 | `sources/util/compare.py:68` | `/dev/loop0` hardcoded. |
| 10 | `firmae.config:add_partition` | busy-wait `while (! FOUND)` senza timeout → hang infinito se il device non compare mai. |

## S3 — Fragilità / euristiche hardcoded

| # | Dove | Problema |
|---|------|----------|
| 11 | `makeNetwork.py:219-226` | IP DHCP riconosciuti con euristiche per-modello: `10.0.2.*` e `.190` (Netgear R6900). Non generalizza. |
| 12 | `network.sh:38-56` | State machine bridge con workaround commentati per singoli modelli (WNR2000v5, DIR-505L, TL-WA850RE). |
| 13 | `fixImage.sh:42-49` | Scansiona con `strings` **ogni** binario in bin/sbin per dedurre directory da creare — O(n·size), lentissimo su rootfs grandi. |
| 14 | `fixImage.sh` | Creazione device node MTD hardcoded fino a `/dev/mtd10`; soglia "5 device" arbitraria. |
| 15 | `sources/extractor/extractor.py:23` | `BINWALK_CMD = ~/.cargo/bin/binwalk` hardcoded (path utente `voidspace`). Fallisce per altri utenti. Già annotato nel README come debito. |
| 16 | `makeNetwork.py` regex parsing (`findPorts`, `findMacChanges`, …) | Nessun recupero su log malformati/troncati; byte non-UTF8 silenziosamente scartati (`readWithException`). |

## S4 — Spreco risorse / qualità

| # | Dove | Problema |
|---|------|----------|
| 17 | `network.sh:63-70` | Loop infinito che flusha iptables ogni 5s → CPU busy per tutta l'emulazione. |
| 18 | `run_service.sh:11-16` | Loop infinito di respawn del servizio con `ps\|grep`; delay 120s hardcoded. |
| 19 | `sources/libnvram/nvram.c:812` | `parse_nvram_from_file`: `sprintf` byte-per-byte in loop. Enormemente inefficiente. |
| 20 | `sources/libnvram/config.h:5` | `DEBUG` sempre attivo → stderr verboso in "produzione", possibile info disclosure. |
| 21 | `sources/libnvram/nvram.c` | `IPC_TIMEOUT=1000` iterazioni per il semaforo; su VM lenta il semaforo può fallire → race silenziosa (ftok è weak symbol, se assente niente lock). |
| 22 | `delete.sh:39` | `for i in 0 .. 4` — stringa letterale, non aritmetica: il loop di cleanup TAP **non fa nulla**. |
| 23 | ovunque (`getArch.py`, `tar2db.py`, `util.py`) | Credenziali PostgreSQL hardcoded (`firmadyne/firmadyne`) e `except:` nudi che ingoiano errori. |

## S5 — Architetturali (le vere ragioni del "troppo buggato")

1. **Poliglotta a strati**: bash che genera bash (`run.sh` templatizzato da python `makeNetwork.py`)
   che sorgente `firmae.config` con funzioni bash usate anche da python via `subprocess`.
   Debugging e testing quasi impossibili; lo stato passa per file, env var e DB insieme.
2. **Root obbligatorio** per costruzione immagine (loop+mount+chroot) → non isolabile,
   non parallelizzabile in sicurezza, non containerizzabile senza `--privileged`.
3. **Stato triplicato**: filesystem `scratch/`, PostgreSQL, env var. Nessuna sorgente di verità.
4. **Nessun test**: zero unit/integration test. Le euristiche di rete (parte più delicata)
   non hanno un solo caso di regressione.
5. **Cleanup non idempotente**: se un run muore a metà lascia loop device, TAP, mount e
   processi QEMU appesi che avvelenano i run successivi (`cleanup.sh` esiste ma è best-effort).

## Cosa NON è rotto (da preservare)

- I **kernel istrumentati** e il **protocollo di log** (`firmadyne: fn[...]: ...`) — funzionano.
- La **libnvram** come idea (LD_PRELOAD + tmpfs + alias vendor) — solo da irrobustire (S1/S4).
- Le **5 arbitration** come strategia di fallback — vanno strutturate, non buttate.
- Il **parsing del serial log** come metodo di inferenza topologia — l'algoritmo è corretto,
  è l'implementazione (regex sparse, stato globale) a essere fragile.
</content>
