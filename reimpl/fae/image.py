"""Costruzione immagine ext2 SENZA mount/loop/root — rimpiazza makeImage.sh + fixImage.sh.

Strategia (vedi 03_reimplementazione.md §Il nodo mknod):
  1. binwalk estrae il rootfs in una directory posseduta dall'utente;
  2. si patcha la directory come normali operazioni di filesystem (dir/etc mancanti);
  3. `mke2fs -d <rootfs> image.ext2 <size>` sigilla la dir nell'ext2 in un colpo;
  4. i device node (/dev/console, ttyS1, mtd*) si iniettano con `debugfs` (no mount, no root).

Elimina add_partition/del_partition/losetup/mount/chroot e le loro race (bug #7-10).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .config import Config

# device node minimi da iniettare via debugfs: (path, type, major, minor)
DEVICE_NODES = [
    ("/dev/console", "c", 5, 1),
    ("/dev/null", "c", 1, 3),
    ("/dev/ptmx", "c", 5, 2),      # Unix98 pty master: serve a telnetd (shell di debug)
    ("/dev/ttyS1", "c", 4, 65),
    *[(f"/dev/mtd{i}", "c", 90, i * 2) for i in range(11)],
    *[(f"/dev/mtdblock{i}", "b", 31, i) for i in range(11)],
]

# directory che fixImage.sh crea sempre (le più comuni; il resto si deduce dai binari init)
ESSENTIAL_DIRS = ["proc", "sys", "dev/pts", "tmp", "var/run", "var/lock", "run",
                  "root", "etc", "usr/bin", "usr/sbin", "firmadyne/libnvram",
                  "firmadyne/libnvram.override"]


def _sizG_from_dir(root: Path) -> int:
    total = sum(f.stat().st_size for f in root.rglob("*") if f.is_file())
    # il guest scrive su tmpfs (/run,/tmp,/var), non sull'ext2: basta un margine
    # fisso per inode/metadati. Immagine piccola = mke2fs veloce.
    return max(48 * 1024 * 1024, total + 32 * 1024 * 1024)


def patch_rootfs(cfg: Config, root: Path) -> None:
    """Operazioni ex fixImage.sh eseguibili senza chroot: dir mancanti, /etc essenziali, binari FirmAE."""
    for d in ESSENTIAL_DIRS:
        (root / d).mkdir(parents=True, exist_ok=True)
    etc = root / "etc"
    if not (etc / "TZ").exists():
        (etc / "TZ").write_text("EST5EDT\n")
    if not (etc / "hosts").exists():
        (etc / "hosts").write_text("127.0.0.1 localhost\n")
    # (porting rimanente di fixImage.sh: passwd/group/nsswitch, disabilita reboot,
    #  scan mirato dei binari init per dir /var|/tmp — vedi 02_bug_criticita #13)


def install_firmadyne(cfg: Config, root: Path, arch: str) -> None:
    """Copia i binari istrumentati e gli script runtime in /firmadyne (ex makeImage.sh)."""
    fd = root / "firmadyne"
    fd.mkdir(exist_ok=True)
    for name in ("busybox", "console", "libnvram.so", "libnvram_ioctl.so", "gdb", "gdbserver", "strace"):
        src = cfg.binary(name, arch)
        if src.exists():
            shutil.copy2(src, fd / name)
            (fd / name).chmod(0o755)
    # /firmadyne/sh: gli script guest hanno shebang #!/firmadyne/sh
    sh = fd / "sh"
    if not sh.exists():
        sh.symlink_to("busybox")
    # script runtime statici (preInit.sh/network.sh/run_service.sh/...)
    for script in cfg.guest_scripts.glob("*.sh"):
        dst = fd / script.name
        shutil.copy2(script, dst)
        dst.chmod(0o755)
    # placeholder scritti a runtime dalla pipeline; li creiamo vuoti perché esistano
    for f in ("network_type", "net_bridge", "net_interface", "service", "service_name"):
        (fd / f).touch()
    (fd / "libnvram").mkdir(exist_ok=True)
    (fd / "libnvram.override").mkdir(exist_ok=True)
    dbg = fd / "debug.sh"
    dbg.write_text("#!/firmadyne/sh\n")
    dbg.chmod(0o755)


def prepare(cfg: Config, rootfs: Path, arch: str) -> None:
    """Patcha e popola /firmadyne nella dir rootfs (idempotente). NON sigilla l'ext2."""
    patch_rootfs(cfg, rootfs)
    install_firmadyne(cfg, rootfs, arch)


def seal(cfg: Config, slug: str, rootfs: Path) -> Path:
    """Sigilla la dir rootfs (già preparata) in scratch/<slug>/image.ext2. No root."""
    work = cfg.scratch / str(slug)
    work.mkdir(parents=True, exist_ok=True)
    image = work / "image.ext2"

    size = _sizG_from_dir(rootfs)
    subprocess.run(["mke2fs", "-q", "-F", "-t", "ext2", "-d", str(rootfs), str(image), str(size // 1024)],
                   check=True)

    # device node via debugfs: serve `cd <dir>` + mknod col basename, altrimenti
    # l'inode viene allocato ma non linkato nella directory (no mount, no root).
    import os
    script_lines = []
    for path, typ, major, minor in DEVICE_NODES:
        d, base = os.path.split(path)
        script_lines.append(f"cd {d or '/'}\nmknod {base} {typ} {major} {minor}\n")
    script = work / "mknod.debugfs"
    script.write_text("".join(script_lines))
    subprocess.run(["debugfs", "-w", "-f", str(script), str(image)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return image
