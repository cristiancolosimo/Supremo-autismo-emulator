"""Lifecycle QEMU con handle di processo reale — rimpiazza run.{arch}.sh + i kill via ps|grep.

Costruisce la command line e gestisce start/stop via subprocess.Popen (+ QMP opzionale).
Nessun `kill $(ps aux | grep qemu ...)` (bug #5): il processo si termina dal suo handle.
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
from contextlib import suppress
from pathlib import Path

from .config import Config, ARCH_TABLE
from .models import NetPlan, NetworkType, Interface
from .netinfer import host_ip


# porte comuni su router/IoT: spesso i servizi partono DOPO l'inferenza (telnet, ftp,
# admin http alternativi, TR-069, UPnP). Le mappiamo sempre così i servizi a runtime
# sono raggiungibili anche se lo snapshot d'inferenza non li ha visti.
COMMON_TCP_PORTS = (21, 23, 25, 53, 80, 443, 8080, 8443, 8000, 8888,
                    7547, 49152, 1900, 5000, 9000, 31338)


def host_fwd_map(plan: NetPlan) -> dict[int, int]:
    """guest_port -> host_port per la modalità user-net rootless (host port alto)."""
    m = {80: 8080, 443: 8443}
    ports = {p.port for p in plan.ports if p.proto == "tcp"} | set(COMMON_TCP_PORTS)
    for port in sorted(ports):
        if port not in m:
            m[port] = 20000 + port if port < 45000 else port
    return m


def _qemu_netdevs(arch: str, plan: NetPlan, tap_prefix: str, user_net: bool = False,
                  tap_name: str | None = None) -> list[str]:
    """Argomenti -device/-netdev. TAP per l'interfaccia primaria, socket placeholder per le altre.

    user_net=True forza slirp+hostfwd sulla NIC primaria: rootless (niente TAP,
    niente CAP_NET_ADMIN). Funziona se il servizio ascolta su 0.0.0.0.
    """
    dev = ARCH_TABLE[arch].net_device
    args: list[str] = []
    primary = plan.interfaces[0] if plan.interfaces else None
    n_slots = 1 if (arch == "armel" and plan.network_type is not NetworkType.NONE) else 4
    for i in range(n_slots):
        if i == 0 and (primary is not None or user_net):
            if plan.is_dhcp or user_net:
                # Rootless: slirp con la subnet del guest, così anche un IP LAN
                # statico (es. 192.168.0.1) è raggiungibile senza TAP/root.
                # hostfwd punta direttamente all'IP del guest; host port alto (<1024 no).
                opts = ""
                target = ""
                if primary is not None and not plan.is_dhcp:
                    a, b, c, _ = primary.ip.split(".")
                    hip = host_ip(primary.ip)
                    opts = f",net={a}.{b}.{c}.0/24,host={hip}"
                    target = primary.ip
                fwds = [f"hostfwd=tcp::{h}-{target}:{g}" for g, h in host_fwd_map(plan).items()]
                args += ["-device", f"{dev},netdev=net{i}",
                         "-netdev", f"user,id=net{i}{opts}," + ",".join(fwds)]
            else:
                # TAP pre-creato di proprietà dell'utente (rootless: `ip tuntap add
                # ... user $USER`). QEMU vi si attacca senza root.
                ifname = tap_name or f"{tap_prefix}_{i}"
                args += ["-device", f"{dev},netdev=net{i}",
                         "-netdev", f"tap,id=net{i},ifname={ifname},script=no,downscript=no"]
        else:
            # NIC placeholder: slirp isolato, nessun bind di porta host
            # (evita "Address already in use" tra run: bug #7 di stato globale).
            args += ["-device", f"{dev},netdev=net{i}",
                     "-netdev", f"user,id=net{i},restrict=on"]
    return args


def build_cmd(cfg: Config, arch: str, image: Path, serial_log: Path,
              plan: NetPlan, qemu_init: str, *, debug: bool = False,
              monitor_sock: Path | None = None, user_net: bool = False,
              tap_name: str | None = None, console_sock: Path | None = None) -> list[str]:
    spec = ARCH_TABLE[arch]
    endian_kernel = cfg.kernel_path(arch, debug=debug)
    tap_prefix = f"tap{serial_log.parent.name}"
    if arch == "armel":
        disk = ["-drive", f"if=none,file={image},format=raw,id=rootfs",
                "-device", "virtio-blk-device,drive=rootfs"]
    else:
        disk = ["-drive", f"if=ide,format=raw,file={image}"]
    append = (f"root={spec.rootfs} console=ttyS0 "
              "nandsim.parts=64,64,64,64,64,64,64,64,64,64 "
              f"{qemu_init} rw debug ignore_loglevel print-fatal-signals=1 "
              "FIRMAE_NET=true FIRMAE_NVRAM=true FIRMAE_KERNEL=true FIRMAE_ETC=true "
              f"firmadyne.syscall={'32' if debug else '1'} user_debug={'31' if debug else '0'}")
    # -serial #1 = ttyS0 (console kernel + serial log). #2 = ttyS1 su socket unix lato
    # host = console di root indipendente dal firmware (vedi _CONSOLE nel launcher).
    serial = ["-serial", f"file:{serial_log}"]
    if console_sock:
        serial += ["-serial", f"unix:{console_sock},server,nowait"]
    cmd = [spec.qemu, "-m", "256", "-M", spec.machine, "-kernel", str(endian_kernel),
           *disk, "-append", append, *serial,
           "-display", "none", *_qemu_netdevs(arch, plan, tap_prefix, user_net, tap_name)]
    if monitor_sock:
        cmd += ["-monitor", f"unix:{monitor_sock},server,nowait"]
    return cmd


class QemuProcess:
    """Context manager: garantisce il teardown del processo anche su eccezione."""

    def __init__(self, cmd: list[str], monitor_sock: Path | None = None,
                 stderr_path: Path | None = None):
        self.cmd = cmd
        self.monitor_sock = monitor_sock
        self.stderr_path = stderr_path      # stderr di QEMU (errori tap/kvm/arg) se serve
        self._stderr_fh = None
        self.proc: subprocess.Popen | None = None

    def __enter__(self) -> "QemuProcess":
        err = subprocess.DEVNULL
        if self.stderr_path:
            self._stderr_fh = open(self.stderr_path, "w")
            err = self._stderr_fh
        self.proc = subprocess.Popen(self.cmd, stdout=subprocess.DEVNULL,
                                     stderr=err, start_new_session=True)
        return self

    def wait(self, timeout: int) -> None:
        assert self.proc is not None
        with suppress(subprocess.TimeoutExpired):
            self.proc.wait(timeout=timeout)

    def _qmp(self, command: str) -> None:
        if not self.monitor_sock or not self.monitor_sock.exists():
            return
        with suppress(OSError), socket.socket(socket.AF_UNIX) as s:
            s.settimeout(2)
            s.connect(str(self.monitor_sock))
            s.sendall(command.encode() + b"\n")

    def _wait(self, timeout: int) -> None:
        # Immune al Ctrl-C ripetuto: durante il teardown l'utente spesso pesta Ctrl-C
        # più volte. Senza questo, un KeyboardInterrupt qui aborta il teardown e QEMU
        # resta orfano (bug: qemu continua a girare dopo Ctrl-C).
        while True:
            try:
                with suppress(subprocess.TimeoutExpired):
                    self.proc.wait(timeout=timeout)
                return
            except KeyboardInterrupt:
                continue

    def _signal_group(self, sig: int) -> None:
        # start_new_session=True ⇒ QEMU (e i suoi figli) sono in una process-group
        # dedicata col PID di QEMU come leader: killiamo l'intero gruppo.
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(self.proc.pid, sig)

    def __exit__(self, *exc) -> None:
        if self.proc and self.proc.poll() is None:
            self._qmp("system_powerdown")   # shutdown pulito via monitor, se disponibile
            self._wait(5)
            if self.proc.poll() is None:
                self._signal_group(signal.SIGTERM)
                self._wait(3)
            if self.proc.poll() is None:
                self._signal_group(signal.SIGKILL)
                self._wait(2)
        if self._stderr_fh:
            self._stderr_fh.close()
