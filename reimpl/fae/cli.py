"""Entrypoint CLI: supremo-autismo-emulator run <firmware> [--brand B] [--iid N]."""
from __future__ import annotations

import argparse
from pathlib import Path

from .config import Config
from . import pipeline


_EXAMPLES = """\
esempi
------
  # emula un firmware end-to-end (extract → arch → immagine → boot → inferenza → verify web)
  ./sae run firmwares/mod10.bin

  # timeout piu' generosi su firmware lenti a bootare
  ./sae run firmwares/mod10.bin --infer-timeout 150 --check-timeout 100

  # ri-parte da zero: butta la cache (extract+immagine+log) del run
  ./sae run firmwares/mod10.bin --rebuild

  # rifa' solo il boot d'inferenza (tiene extract/immagine in cache)
  ./sae run firmwares/mod10.bin --no-reuse

  # verify su IP LAN statico via TAP pre-creato (serve: sudo ./setup-tap.sh sae0)
  ./sae run firmwares/mod10.bin --tap sae0

analisi firmware: unpack -> modifica -> (emula | flash)
-------------------------------------------------------
  # spacchetta (header/rootfs/tail + config.json + parts/ ispezionabili)
  ./sae extract firmwares/mod10.bin -o work
  vim work/rootfs/etc/passwd                    # modifichi i file, normali

  # EMULA direttamente la rootfs modificata (niente ripack): boota una COPIA,
  #   la tua work/rootfs resta pulita per il flash
  ./sae run work                                # accetta una dir spacchettata

  # RICOMPONI un .bin flashabile (ripara i checksum, preserva ART/config)
  ./sae build work -o new.bin                   # poi: flashrom -w new.bin (SOIC-8)

editare la root (percorso classico da .bin)
-------------------------------------------
  # OFFLINE (spento): edita i file nella dir rootfs estratta, poi resealla e riboota
  #   la dir vive in scratch/<nome-bin>/extract/.../  ed e' tua, file normali
  vim scratch/mod10/extract/.../etc/passwd
  ./sae run firmwares/mod10.bin --no-reuse      # prepare+seal ricostruiscono l'ext2

  # A RUNTIME (vivo): shell root nel guest, modifica, poi `sync` PRIMA di Ctrl-C
  ./sae run firmwares/mod10.bin --keep-alive
  telnet 127.0.0.1 51338     # (porta shell mostrata all'avvio) → modifichi → `sync`

shell di root (con --keep-alive)
--------------------------------
  # via RETE (telnetd): richiede la rete del guest su
  telnet 127.0.0.1 51338                 # user-net   (o: telnet <ip-guest> 31338 con --tap)

  # via SERIALE (ttyS1): SEMPRE disponibile, indipendente da firmware e rete
  #   funziona anche se il firmware si incastra o non alza la rete
  socat -,raw,echo=0,escape=0x1d UNIX-CONNECT:scratch/mod10/console.sock
  nc -U scratch/mod10/console.sock       # alternativa senza socat (esci con Ctrl-])
"""


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="sae", description="supremo-autismo-emulator — emulazione firmware IoT",
        epilog=_EXAMPLES, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="emula un firmware (.bin) o una rootfs già spacchettata (dir)")
    r.add_argument("firmware", type=Path,
                   help="path al .bin, oppure una dir di `sae extract` (emula la rootfs modificata)")
    r.add_argument("--brand", default="auto")
    r.add_argument("--iid", type=int, default=1)
    r.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2],
                   help="radice del progetto (default: repo)")
    r.add_argument("--infer-timeout", type=int, default=240)
    r.add_argument("--check-timeout", type=int, default=180)
    r.add_argument("--no-reuse", action="store_true",
                   help="rifà il boot d'inferenza anche se il serial log è in cache")
    r.add_argument("--tap", default=None,
                   help="nome TAP pre-creato (es. sae0) per verify su IP statico; "
                        "senza, usa user-net rootless")
    r.add_argument("--keep-alive", action="store_true",
                   help="tiene viva l'emulazione (niente verify one-shot); Ctrl-C per fermare")
    r.add_argument("--rebuild", action="store_true",
                   help="butta la cache del run (extract+immagine+log) e riparte da zero")

    # --- analisi firmware: pack/unpack con checksum vendor (fmk.py) ---
    fe = sub.add_parser("extract", help="scompatta un firmware (header/rootfs/tail + config.json)")
    fe.add_argument("firmware", type=Path)
    fe.add_argument("-o", "--workdir", type=Path, default=None)
    fe.add_argument("--tail-offset", type=lambda x: int(x, 0), default=None,
                    help="inizio coda preservata (ART/config) su dump full-chip, es. 0x3f0000")
    fb = sub.add_parser("build", help="ricompone un workdir in un .bin flashabile (ripara i checksum)")
    fb.add_argument("workdir", type=Path)
    fb.add_argument("-o", "--out", type=Path, default=None)
    fb.add_argument("--nopad", action="store_true",
                    help="output solo header+rootfs (OTA parziale, NON per flash full-chip)")
    fi = sub.add_parser("info", help="scan+parse: stampa il layout del firmware (JSON)")
    fi.add_argument("firmware", type=Path)
    fi.add_argument("--tail-offset", type=lambda x: int(x, 0), default=None)

    args = p.parse_args(argv)

    if args.cmd in ("extract", "build", "info"):
        from . import fmk
        if args.cmd == "info":
            import json
            print(json.dumps(fmk.info(args.firmware, tail_offset=args.tail_offset), indent=2))
        elif args.cmd == "extract":
            wd = args.workdir or args.firmware.with_suffix(".fmk")
            fmk.extract(args.firmware, wd, tail_offset=args.tail_offset)
            print(f"estratto in {wd}/  → edita rootfs/, poi: ./sae build {wd}")
        else:
            out = fmk.build(args.workdir, args.out, args.nopad)
            print(f"scritto {out} ({out.stat().st_size} byte)")
        return 0

    cfg = Config(root=args.root, infer_timeout=args.infer_timeout,
                 check_timeout=args.check_timeout)
    from shutil import which
    cfg.binwalk = which("binwalk") or str(Path.home() / ".cargo/bin/binwalk")

    if args.cmd == "run":
        st = pipeline.run(cfg, args.firmware.resolve(), args.brand, args.iid,
                          reuse=not args.no_reuse, tap=args.tap, keep_alive=args.keep_alive,
                          rebuild=args.rebuild)
        if args.keep_alive:
            return 0
        print(f"\n=== RESULT ===\narch={st.arch} network={st.plan.network_type.value if st.plan else '?'} "
              f"ip={st.ip} web={'OK' if st.web else 'FAIL'}")
        return 0 if st.web else 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
