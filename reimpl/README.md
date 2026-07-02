# `fae` — reimplementazione del motore FirmAE

Scheletro Python 3.11+ del motore di emulazione. Orchestrazione riscritta; i binari
istrumentati (`../../FirmAE/binaries/`) e le librerie C (`libnvram`, `console`) si riusano.

Vedi [../03_reimplementazione.md](../03_reimplementazione.md) per le decisioni di design.

## Stato

| Modulo | Stato | Note |
|--------|-------|------|
| `models.py` | ✅ completo | blackboard tipizzata + serializzazione state.json |
| `config.py` | ✅ completo | tabelle arch/kernel/qemu (ex firmae.config), no credenziali |
| `netinfer.py` | ✅ **funzionante + self-check** | i 5 estrattori + classify(); `python -m fae.netinfer` |
| `qemu.py` | ✅ funzionante | Popen + QMP, teardown context-managed, builder command line |
| `image.py` | ✅ funzionante | `mke2fs -d` + `debugfs` mknod, **no root/mount/loop** |
| `extract.py` | ⛲ da portare | wrapper binwalk (path da $PATH), meccanico |
| `arch.py` | ⛲ da portare | porting getArch.py senza SQL |
| `verify.py` | ⛲ da portare | ping+http con handle processo (ex check_emulation.sh) |
| `arbitration.py` | ⛲ da portare | 5 FIRMAE_* → oggetti Strategy |
| `pipeline.py` / `cli.py` | ⛲ da portare | orchestratore + entrypoint |

## Prova rapida

```bash
cd analisi/reimpl
python -m fae.netinfer          # self-check del parser di rete (offline, niente QEMU)
```

## Dipendenze runtime (host)

- `python3` (>=3.11), stdlib soltanto per il core.
- `qemu-system-{mips,mipsel,arm}` — emulazione.
- `mke2fs`, `debugfs` (pacchetto `e2fsprogs`) — build immagine senza root.
- `binwalk` (v3, da `$PATH`) — estrazione.
- `iproute2` / `tunctl` — solo per il setup TAP host (richiede comunque privilegi rete).

## Perché è meglio del FirmAE attuale

- **Niente root per build**: `mke2fs -d`/`debugfs` al posto di losetup+mount+chroot → elimina i bug #7-10.
- **Niente `ps|grep` kill**: `QemuProcess` termina dal proprio handle → elimina il bug #5.
- **Un linguaggio, tipizzato, testabile**: il parser di rete (parte più fragile) ha un self-check
  eseguibile offline sui log reali. Prima: zero test.
- **Parallelizzabile**: senza loop device globale, N emulazioni girano davvero in parallelo.
- **Stato unico**: `RunState` → `state.json`, niente più filesystem+PostgreSQL+env var triplicati.
</content>
