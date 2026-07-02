"""Estrazione firmware via binwalk v3 → directory rootfs + kernel.

Rimpiazza sources/extractor/extractor.py con un wrapper minimale: niente path
binwalk hardcoded (bug #15), niente DB. Output = directory rootfs posseduta
dall'utente, pronta per image.py (mke2fs -d).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from .config import Config

# marcatori che identificano la radice di un rootfs Linux estratto
_ROOTFS_MARKERS = ("bin/busybox", "bin", "sbin", "etc")


def _looks_like_rootfs(d: Path) -> bool:
    hits = sum((d / m).exists() for m in _ROOTFS_MARKERS)
    return hits >= 2 and (d / "bin").is_dir()


def _find_rootfs(extract_dir: Path) -> Path:
    """binwalk annida l'estrazione; trova la dir che contiene il vero rootfs."""
    candidates = [extract_dir, *(p for p in extract_dir.rglob("*") if p.is_dir())]
    # preferisci il rootfs più "profondo/completo" (più marker), poi il più grande
    best = max(
        (c for c in candidates if _looks_like_rootfs(c)),
        key=lambda c: sum((c / m).exists() for m in _ROOTFS_MARKERS),
        default=None,
    )
    if best is None:
        raise RuntimeError(f"nessun rootfs trovato sotto {extract_dir}")
    return best


def extract(cfg: Config, firmware: Path, iid: int) -> Path:
    """Estrae il firmware; ritorna il path della directory rootfs."""
    work = cfg.scratch / firmware.stem
    out = work / "extract"
    if out.exists():
        # riuso: se già estratto, non rifare (idempotente)
        try:
            return _find_rootfs(out)
        except RuntimeError:
            pass
    out.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [cfg.binwalk, "--extract", "--quiet", "--directory", str(out), str(firmware)],
        check=True,
    )
    return _find_rootfs(out)
