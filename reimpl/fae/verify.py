"""Verifica web rootless via user-net (hostfwd) — rimpiazza check_emulation.sh.

FirmAE usava TAP (richiede root/CAP_NET_ADMIN) e ping+curl sull'IP del guest.
Qui la NIC primaria è slirp con port-forward su host port alti: curl in loopback,
niente root, niente `kill $(ps|grep qemu)` (handle di processo reale, bug #5).

Limite noto: raggiunge il servizio solo se ascolta su 0.0.0.0 (non solo sull'IP
LAN del guest). Per firmware che bindano solo l'IP LAN serve la modalità TAP.
"""
from __future__ import annotations

import time
import urllib.request
import ssl
from pathlib import Path

from .config import Config
from .models import NetPlan
from .qemu import build_cmd, QemuProcess, host_fwd_map


def _probe_ip(ip: str, port: int, scheme: str, deadline: float) -> bool:
    ctx = ssl._create_unverified_context()
    url = f"{scheme}://{ip}:{port}/"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3, context=ctx) as r:
                if r.status < 500:
                    return True
        except urllib.error.HTTPError:
            return True                      # risposta HTTP (anche 401/403) = web up
        except Exception:
            time.sleep(3)
    return False


def verify_web(cfg: Config, arch: str, image: Path, plan: NetPlan,
               qemu_init: str, serial_log: Path, tap: str | None = None) -> bool:
    """Boota e verifica che il web server risponda. Ritorna web ok.

    tap=None → user-net rootless (slirp, hostfwd su host port alti).
    tap="sae0" → TAP pre-creato di proprietà utente: si raggiunge direttamente
    l'IP LAN statico del guest (via robusta di FirmAE, senza root a runtime).
    """
    cmd = build_cmd(cfg, arch, image, serial_log, plan, qemu_init,
                    user_net=(tap is None), tap_name=tap)
    with QemuProcess(cmd) as q:
        deadline = time.time() + cfg.check_timeout
        if tap:
            # curl diretto sull'IP del guest, porte reali (host è .2 sul tap)
            targets = [(p.port, p.port) for p in plan.ports if p.proto == "tcp"] or [(80, 80)]
            ip = plan.ips[0] if plan.ips else "192.168.0.1"
            for guest, _host in targets:
                scheme = "https" if guest == 443 else "http"
                if _probe_ip(ip, guest, scheme, deadline):
                    return True
        else:
            for guest, host in host_fwd_map(plan).items():
                scheme = "https" if guest == 443 else "http"
                if _probe_ip("127.0.0.1", host, scheme, deadline):
                    return True
    return False
