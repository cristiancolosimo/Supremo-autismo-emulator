# Analisi FirmAE → Reimplementazione (`fae`)

Cartella di analisi tecnica di **FirmAE** (framework di emulazione firmware IoT, KAIST
ACSAC 2020) in vista di una **reimplementazione pulita e ottimizzata**.

FirmAE funziona ma è fragile: mix bash/python non tipizzato, race condition su
loop-device e `mount`, kill di processi via `ps | grep`, SQL injection, stato
globale, euristiche hardcoded per singoli modelli. Emula però correttamente e le sue
**5 tecniche di arbitration** sono l'idea di valore da preservare.

## Indice

| File | Contenuto |
|------|-----------|
| [01_architettura.md](01_architettura.md) | Come funziona FirmAE oggi: pipeline end-to-end, componenti, kernel/libnvram, formato dati |
| [02_bug_criticita.md](02_bug_criticita.md) | Catalogo bug ordinato per severità (con file:riga), race condition, root cause |
| [03_reimplementazione.md](03_reimplementazione.md) | Architettura target `fae`: decisioni, cosa si butta, cosa si tiene, mapping componenti |
| [reimpl/](reimpl/) | Scheletro Python del nuovo motore (tipizzato, no-mount, no-postgres, lifecycle processi corretto) |

## TL;DR delle decisioni architetturali

1. **Un solo linguaggio**: Python 3.11+ tipizzato. Niente più bash che genera bash che genera bash.
2. **Niente mount / niente root per costruire l'immagine**: `mke2fs -d rootfs/ image.ext2`
   popola l'ext2 da una directory senza loop device, senza `mount`, senza `losetup`,
   senza le race condition di `add_partition`/`del_partition`. È la singola ottimizzazione
   con più impatto: elimina ~5 classi di bug e il requisito root per metà pipeline.
3. **Lifecycle QEMU esplicito**: `subprocess.Popen` + handle + QMP socket per lo shutdown.
   Si elimina l'anti-pattern `kill $(ps aux | grep qemu | awk ...)` (uccide anche processi altrui).
4. **Niente PostgreSQL**: il DB serviva solo come blackboard per pochi campi (arch, iid, brand,
   inventario file). Sostituito da uno `state.json` per-run + SQLite opzionale per la dedup file.
   Zero credenziali hardcoded, zero SQL injection.
5. **Arbitration preservata ma strutturata**: il parsing del serial log del kernel istrumentato
   (interfacce, IP, MAC, bridge, VLAN, porte) resta il cuore. Riscritto come parser tipizzato con
   modelli dati espliciti, euristiche isolate e testabili invece di sparse in 765 righe.

I binari precompilati riutilizzabili così come sono (kernel istrumentati, libnvram, busybox,
gdb, strace) restano invariati: la reimplementazione riguarda **l'orchestrazione**, non il
lavoro di reverse engineering sul kernel.
</content>
</invoke>
