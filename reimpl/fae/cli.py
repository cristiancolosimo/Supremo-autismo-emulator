"""Entrypoint CLI: supremo-autismo-emulator run <firmware> [--brand B] [--iid N]."""
from __future__ import annotations

import argparse
from pathlib import Path

from .config import Config
from . import pipeline


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sae", description="supremo-autismo-emulator")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="emula un firmware")
    r.add_argument("firmware", type=Path)
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
    args = p.parse_args(argv)

    cfg = Config(root=args.root, infer_timeout=args.infer_timeout,
                 check_timeout=args.check_timeout)
    from shutil import which
    cfg.binwalk = which("binwalk") or str(Path.home() / ".cargo/bin/binwalk")

    if args.cmd == "run":
        st = pipeline.run(cfg, args.firmware.resolve(), args.brand, args.iid,
                          reuse=not args.no_reuse, tap=args.tap, keep_alive=args.keep_alive)
        if args.keep_alive:
            return 0
        print(f"\n=== RESULT ===\narch={st.arch} network={st.plan.network_type.value if st.plan else '?'} "
              f"ip={st.ip} web={'OK' if st.web else 'FAIL'}")
        return 0 if st.web else 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
