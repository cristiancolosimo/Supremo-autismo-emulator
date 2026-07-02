"""Rilevamento architettura/endianness da una dir rootfs col comando `file`.

Porting di getArch.py senza tar né PostgreSQL: ispeziona busybox e i binari in
bin/sbin, mappa a {mipseb,mipsel,armel}.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

# file(1) description -> arch base FirmAE
_ARCH = {"MIPS": "mips", "ARM": "arm"}
_END = {"MSB": "eb", "LSB": "el"}

# ordine di preferenza dei binari da ispezionare
_PROBE = ["bin/busybox", "bin/sh"]
_PROBE_DIRS = ["bin", "sbin", "usr/bin", "usr/sbin"]


def _classify(filetype: str) -> str | None:
    arch = next((v for k, v in _ARCH.items() if k in filetype), None)
    end = next((v for k, v in _END.items() if k in filetype), None)
    if not arch or not end:
        return None
    if arch == "arm":            # FirmAE ha solo armel
        return "armel"
    return arch + end            # mipseb / mipsel


def detect(rootfs: Path) -> str:
    """Ritorna l'arch FirmAE (es. 'mipseb') o solleva se non riconosciuta."""
    probes = [rootfs / p for p in _PROBE]
    for d in _PROBE_DIRS:
        dd = rootfs / d
        if dd.is_dir():
            probes += [f for f in dd.iterdir() if f.is_file()]
    for f in probes:
        if not f.exists() or f.is_symlink():
            continue
        ft = subprocess.check_output(["file", "-b", str(f)]).decode()
        if "ELF" not in ft:
            continue
        arch = _classify(ft)
        if arch:
            return arch
    raise RuntimeError(f"architettura non riconosciuta in {rootfs}")
