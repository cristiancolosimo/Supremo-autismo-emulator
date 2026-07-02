"""Tabelle arch/kernel/qemu — rimpiazza firmae.config (le funzioni bash get_*).

Nessuna credenziale, nessuna funzione shell. Sola verità sui path dei binari.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# Le tre architetture supportate da FirmAE (servono kernel istrumentati dedicati).
ARCHS = ("mipseb", "mipsel", "armel")


@dataclass(frozen=True)
class ArchSpec:
    qemu: str          # binario qemu-system-*
    machine: str       # -M
    rootfs: str        # root= device
    kernel: str        # basename kernel in binaries/ (senza suffisso .2/.4)
    net_device: str    # -device per le NIC


# root= punta al disco intero (/dev/sda, non sda1): mke2fs -d produce un
# filesystem senza tabella delle partizioni (niente più fdisk/losetup, bug #7-10).
ARCH_TABLE: dict[str, ArchSpec] = {
    "mipseb": ArchSpec("qemu-system-mips", "malta", "/dev/sda", "vmlinux.mipseb", "e1000"),
    "mipsel": ArchSpec("qemu-system-mipsel", "malta", "/dev/sda", "vmlinux.mipsel", "e1000"),
    "armel":  ArchSpec("qemu-system-arm", "virt", "/dev/vda", "zImage.armel", "virtio-net-device"),
}


@dataclass
class Config:
    root: Path                       # radice del progetto (contiene analisi/)
    binaries: Path = field(init=False)
    scratch: Path = field(init=False)
    guest_scripts: Path = field(init=False)
    # timeout in secondi: boot di test / verifica web (erano TIMEOUT/CHECK_TIMEOUT)
    infer_timeout: int = 240
    check_timeout: int = 360
    binwalk: str = "binwalk"         # da $PATH, NON hardcoded (bug #15)
    # flag arbitration (ex FIRMAE_*)
    boot: bool = True
    net: bool = True
    nvram: bool = True
    kernel: bool = True
    etc: bool = True

    def __post_init__(self) -> None:
        # asset preservati sotto assets/ (vedi assets/ASSETS.md)
        self.binaries = self.root / "assets" / "binaries"
        self.guest_scripts = self.root / "assets" / "guest-scripts"
        self.scratch = self.root / "scratch"

    def kernel_path(self, arch: str, *, debug: bool = False) -> Path:
        spec = ARCH_TABLE[arch]
        if arch.startswith("mips"):
            suffix = ".4" if self.kernel else ".2"      # ex get_kernel FIRMAE_KERNEL
            return self.binaries / f"vmlinux.{arch}{suffix}"
        return self.binaries / spec.kernel

    def binary(self, name: str, arch: str) -> Path:
        return self.binaries / f"{name}.{arch}"
