"""Orchestratore: extract → arch → image → boot/infer → verify.

Rimpiazza run.sh + makeNetwork.py:process(). Stato tipizzato in RunState,
context manager per il teardown QEMU. Due boot:
  1) inference boot (network_type=None): il vero init del firmware configura le
     interfacce, il kernel istrumentato le logga → netinfer le estrae.
  2) verify boot: rete inferita applicata, web check rootless (user-net).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import extract, arch as archmod, image, netinfer, verify
from .config import Config
from .models import RunState, NetPlan, NetworkType
from .qemu import build_cmd, QemuProcess

# candidati init del firmware, in ordine di preferenza (ex inferFile.sh)
_INIT_CANDIDATES = [
    "etc/preinit", "etc/preinit.sh", "etc/init.d/rcS", "etc/rc.d/rcS",
    "etc/rc", "sbin/preinit", "sbin/init", "init", "preinit",
]

# servizio web, in ordine (ex inferFile.sh); (path, comando, nome)
_WEB_SERVICES = [
    ("etc/init.d/uhttpd", "/etc/init.d/uhttpd start", "uhttpd"),
    ("usr/bin/httpd", "/usr/bin/httpd", "httpd"),
    ("usr/sbin/httpd", "/usr/sbin/httpd", "httpd"),
    ("bin/goahead", "/bin/goahead", "goahead"),
    ("bin/alphapd", "/bin/alphapd", "alphapd"),
    ("bin/boa", "/bin/boa", "boa"),
    ("usr/sbin/lighttpd", "/usr/sbin/lighttpd -f /etc/lighttpd/lighttpd.conf", "lighttpd"),
    ("sbin/httpd", "/sbin/httpd", "httpd"),
]


@dataclass
class Detected:
    init: str | None
    service: str | None
    service_name: str | None
    web_static: str | None = None   # docroot degli asset UI statici (fallback GUI)


def _find_web_root(rootfs: Path) -> str | None:
    """Docroot degli asset web statici del firmware, da servire col busybox quando il
    web daemon dinamico (dipendente da SoC/config, es. cos su MediaTek) non sale: così
    la GUI si vede comunque. Preferisce una dir con nome noto; altrimenti quella con più
    pagine .htm(l). ponytail: euristica sul conteggio pagine, ok per i firmware attuali."""
    for name in ("web", "www", "htdocs", "html", "home_page", "webpages"):
        d = rootfs / name
        if d.is_dir() and any(d.rglob("*.htm*")):
            return f"/{name}"
    best, best_n = None, 0
    for d in rootfs.iterdir():
        if d.is_dir():
            n = sum(1 for p in d.rglob("*.htm*") if p.is_file())
            if n > best_n:
                best, best_n = d, n
    return f"/{best.name}" if best and best_n >= 3 else None


def _infer_files(rootfs: Path) -> Detected:
    init = next((f"/{c}" for c in _INIT_CANDIDATES if (rootfs / c).exists()), None)
    svc = next(((cmd, name) for p, cmd, name in _WEB_SERVICES if (rootfs / p).exists()), None)
    return Detected(init, svc[0] if svc else None, svc[1] if svc else None,
                    _find_web_root(rootfs))


# Wrapper di init lanciato dal KERNEL via `init=/firmadyne/sae_init.sh`. Gira col nostro
# busybox STATICO (#!/firmadyne/sh), quindi non dipende dal busybox del guest né dal fatto
# che questo legga /etc/inittab (alcuni sono compilati senza FEATURE_USE_INITTAB → il nostro
# blocco iniettato in rcS non partirebbe mai). Backgrounda l'harness e poi `exec` l'init vero
# del firmware: i job in `&` sopravvivono all'exec (sono processi separati).
_LAUNCHER = """#!/firmadyne/sh
BB=/firmadyne/busybox
$BB mount -t proc proc /proc 2>/dev/null
$BB mount -t sysfs sysfs /sys 2>/dev/null
$BB mkdir -p /dev/pts
$BB mount -t devpts devpts /dev/pts 2>/dev/null
{ipt}{console}{bringup}/firmadyne/network.sh &
/firmadyne/run_service.sh &
/firmadyne/debug.sh &
exec {real_init}
"""

# Neutralizza iptables/ip6tables/ebtables PRIMA dell'init del firmware. I kernel
# istrumentati (malta) non hanno i moduli netfilter (nat/filter/mangle): ogni regola
# fallisce con "Table does not exist" e i demoni di rete che le programmano al boot
# (es. `cos` su MediaTek/Ralink) trattano l'errore come fatale e non avviano i servizi.
# Il wrapper CHIAMA il binario reale (se netfilter c'e', le regole si applicano davvero:
# nessuna regressione) ma forza exit 0 e silenzia lo stderr, così lo script chiamante
# prosegue. Agnostico all'arch (shell puro), idempotente (salta se già wrappato).
# ponytail: euristica "il boot-firewall non è emulabile → non deve bloccare il boot";
# se un firmware legge l'OUTPUT di iptables per prendere decisioni, lì servirebbe di più.
_IPTABLES_STUB = (
    "for _d in /sbin /usr/sbin /bin /usr/bin; do "
    "for _t in iptables ip6tables ebtables arptables "
    "iptables-restore ip6tables-restore; do _f=$_d/$_t; "
    "if [ -f \"$_f\" ] && [ ! -e \"$_f.sae_real\" ]; then "
    "$BB mv \"$_f\" \"$_f.sae_real\" && "
    "$BB printf '#!/firmadyne/sh\\n\"%s.sae_real\" \"$@\" 2>/dev/null\\nexit 0\\n' "
    "\"$_f\" > \"$_f\" && $BB chmod 755 \"$_f\"; "
    "fi; done; done\n"
)

# Console di root su ttyS1, avviata dal launcher PRIMA dell'init del firmware: shell
# indipendente dalla rete e dallo stato del firmware (funziona anche se l'init si incastra).
# Respawn in loop col nostro busybox statico. La seconda seriale di QEMU è un socket unix
# lato host (vedi qemu.build_cmd console_sock).
_CONSOLE = ("(while true; do /firmadyne/busybox setsid /firmadyne/busybox sh "
            "</dev/ttyS1 >/dev/ttyS1 2>&1; /firmadyne/busybox sleep 2; done) &\n")

# Bring-up ROBUSTO della NIC primaria: non dipende da brctl/env/timing di network.sh.
# Ri-asserisce l'IP ogni 5s per ~2min (il firmware può resettare l'iface), poi smette.
# ponytail: loop limitato (while+contatore, niente `seq` che manca in questa busybox).
_BRINGUP = ("(i=0; while [ $i -lt 24 ]; do "
            "/firmadyne/busybox ifconfig {iface} {ip} netmask 255.255.255.0 up; "
            "/firmadyne/busybox sleep 5; i=$((i+1)); done) &\n")

# path del wrapper (relativo a rootfs) e valore da passare a `init=` sul cmdline.
LAUNCHER_REL = "firmadyne/sae_init.sh"
LAUNCHER_INIT = "/firmadyne/sae_init.sh"


def _write_launcher(rootfs: Path, real_init: str, bringup: str, console: str = "") -> None:
    """Scrive il wrapper di init che avvia l'harness e poi exec-a l'init vero del firmware.

    real_init deve restare vivo come PID1 (busybox init: reap + respawn getty). Se il
    firmware non ha /sbin/init si passa lo script rcS: ponytail: se quello ritorna il kernel
    va in panic — ok per i firmware attuali (tutti con /sbin/init), da rivedere se ne emergono
    senza init persistente.
    """
    f = rootfs / LAUNCHER_REL
    f.write_text(_LAUNCHER.format(bringup=bringup, real_init=real_init, console=console,
                                  ipt=_IPTABLES_STUB))
    f.chmod(0o755)


# porta della shell di debug (telnetd senza login → shell root nel guest).
SHELL_PORT = 31338


def _write_runtime(rootfs: Path, det: Detected, network_type: NetworkType,
                   bridge: str, iface: str, ip: str = "", shell: bool = False) -> None:
    fd = rootfs / "firmadyne"
    (fd / "network_type").write_text(str(network_type.value))
    (fd / "net_bridge").write_text(bridge)
    (fd / "net_interface").write_text(iface)
    if det.service:
        (fd / "service").write_text(det.service)
        (fd / "service_name").write_text(det.service_name or "")
    # docroot statico per il fallback GUI (run_service.sh lo serve col busybox se il
    # web daemon dinamico non alza la porta) — vuoto se non trovato
    (fd / "web_static").write_text(det.web_static or "")
    # shell di debug: telnetd (busybox lo ha; nc -e no) su SHELL_PORT, nessun login.
    dbg = fd / "debug.sh"
    if shell:
        # telnetd ha bisogno di pty: monta devpts (idempotente) e respawna telnetd -F
        # in loop (il boot del firmware può ucciderlo). ponytail: loop di respawn
        # deliberato, è una sessione di debug interattiva (l'utente ferma con Ctrl-C).
        dbg.write_text(
            "#!/firmadyne/sh\n"
            "/firmadyne/busybox mkdir -p /dev/pts\n"
            "/firmadyne/busybox mount -t devpts devpts /dev/pts 2>/dev/null\n"
            "while true; do\n"
            "  /firmadyne/busybox pidof telnetd >/dev/null 2>&1 || "
            f"/firmadyne/busybox telnetd -p {SHELL_PORT} -l /firmadyne/sh\n"
            "  /firmadyne/busybox sleep 5\n"
            "done\n")
    else:
        dbg.write_text("#!/firmadyne/sh\n")
    dbg.chmod(0o755)
    bringup = _BRINGUP.format(iface=iface or "eth0", ip=ip) if ip else ""
    # init vero da eseguire come PID1 dopo aver avviato l'harness. Preferiamo /sbin/init
    # (busybox init: resta vivo, reap + respawn getty); altrimenti lo script rcS rilevato.
    real_init = "/sbin/init" if (rootfs / "sbin/init").exists() else (det.init or "/bin/sh")
    _write_launcher(rootfs, real_init, bringup, console=_CONSOLE if shell else "")


# righe di rete che ci interessano nel serial log: quando smettono di crescere,
# il firmware ha finito di configurare la rete → possiamo interrompere il boot.
_NET_MARKERS = (b"__inet_insert_ifa", b"inet_bind", b"br_add_if",
                b"ioctl_SIOCSIFHWADDR", b"register_vlan_dev")


def _wait_until_settled(q: QemuProcess, log: Path, cap: int,
                        min_boot: int = 25, quiet: int = 12, poll: int = 4) -> None:
    """Attende che le righe di rete smettano di comparire (assestamento), non il cap intero.

    Interrompe quando: (a) nessuna nuova riga di rete per `quiet`s dopo `min_boot`s,
    (b) QEMU esce da solo, o (c) si raggiunge `cap`. Riduce l'attesa da minuti a ~30-40s.
    """
    import time
    start = time.time()
    last_count, stale = -1, 0
    while time.time() - start < cap:
        if q.proc is None or q.proc.poll() is not None:
            return
        time.sleep(poll)
        if time.time() - start < min_boot:
            continue
        data = log.read_bytes() if log.exists() else b""
        count = sum(data.count(m) for m in _NET_MARKERS)
        stale = stale + 1 if count == last_count else 0
        last_count = count
        if count > 0 and stale * poll >= quiet:
            print(f"[*] rete assestata a ~{int(time.time() - start)}s ({count} eventi)")
            return


def _fingerprint(firmware: Path) -> str:
    """sha256 corto del contenuto: chiave di cache content-based.

    Per una dir rootfs già spacchettata: hash dell'albero (path,size,mtime) →
    invalida la cache quando editi i file. Per un .bin: hash del contenuto.
    """
    import hashlib
    if firmware.is_dir():
        h = hashlib.sha256()
        for p in sorted(firmware.rglob("*")):
            st = p.lstat()
            h.update(f"{p.relative_to(firmware)}|{st.st_size}|{int(st.st_mtime)}\n"
                     .encode())
        return h.hexdigest()[:16]
    return hashlib.sha256(firmware.read_bytes()).hexdigest()[:16]


def run(cfg: Config, firmware: Path, brand: str = "auto", iid: int = 1,
        reuse: bool = True, tap: str | None = None, keep_alive: bool = False,
        rebuild: bool = False) -> RunState:
    print(f"[*] firmware: {firmware}")
    slug = firmware.stem   # sotto-cartella scratch = nome del binario (senza .bin), non un id numerico
    work = cfg.scratch / slug

    # cache content-based: se il firmware è cambiato (o --rebuild), butta la work dir
    # così extract/immagine/log ripartono puliti invece di riusare roba stantia
    # (prima la cache era chiavata solo sul nome file → stale se il .bin cambiava).
    import shutil
    fp, fpfile = _fingerprint(firmware), work / ".fingerprint"
    if work.exists() and (rebuild or (fpfile.exists() and fpfile.read_text() != fp)):
        print(f"[*] cache invalidata ({'--rebuild' if rebuild else 'firmware cambiato'})")
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)
    fpfile.write_text(fp)

    if firmware.is_dir():
        # rootfs già spacchettato (es. da `sae extract`) e magari modificato:
        # emula una COPIA in scratch, così la dir originale resta pulita per
        # `sae build`/flash (niente /firmadyne o iptables-stub iniettati nel .bin).
        src = extract._find_rootfs(firmware)
        rootfs = work / "rootfs"
        if not rootfs.exists():
            shutil.copytree(src, rootfs, symlinks=True, ignore_dangling_symlinks=True)
        print(f"[*] rootfs:   {rootfs}  (copia di {src}, originale intatto per build/flash)")
    else:
        rootfs = extract.extract(cfg, firmware, iid)
        print(f"[*] rootfs:   {rootfs}")
    arch = archmod.detect(rootfs)
    endian = "eb" if arch.endswith("eb") else "el"
    print(f"[*] arch:     {arch} ({endian})")

    det = _infer_files(rootfs)
    print(f"[*] init:     {det.init}")
    print(f"[*] service:  {det.service} ({det.service_name})")

    state = RunState(iid=iid, firmware=str(firmware), brand=brand, arch=arch,
                     init=det.init or "", web_service=det.service)

    # ---- (1) inference boot: network_type=None ----
    image.prepare(cfg, rootfs, arch)   # popola /firmadyne nella dir (idempotente, serve anche al verify)
    init_log = work / "qemu.initial.serial.log"
    # il kernel esegue il NOSTRO wrapper (busybox statico): avvia l'harness e poi exec-a
    # l'init vero. Agnostico al busybox del guest / al supporto di /etc/inittab.
    qemu_init = f"init={LAUNCHER_INIT}"
    if reuse and init_log.exists() and init_log.stat().st_size > 4096:
        print(f"[*] inference boot SALTATO (log in cache: {init_log})")
    else:
        # sigilla l'immagine SOLO quando serve bootare davvero (altrimenti mke2fs sprecato)
        _write_runtime(rootfs, det, NetworkType.NONE, "", "")
        img = image.seal(cfg, slug, rootfs)
        print(f"[*] image:    {img} ({img.stat().st_size // 1024} KiB)")
        print(f"[*] inference boot (max {cfg.infer_timeout}s, early-exit ad assestamento)...")
        cmd = build_cmd(cfg, arch, img, init_log, NetPlan(), qemu_init)
        with QemuProcess(cmd) as q:
            _wait_until_settled(q, init_log, cfg.infer_timeout)

    data = init_log.read_bytes() if init_log.exists() else b""
    plan = netinfer.infer(data, endian)
    state.plan = plan
    print(f"[*] network:  {plan.network_type.value}  ips={plan.ips}  "
          f"ports={[p.port for p in plan.ports]}")

    # ---- (2) verify boot: applica rete inferita + check web (rootless) ----
    bridge = plan.interfaces[0].bridge or "" if plan.interfaces else ""
    iface = plan.interfaces[0].dev if plan.interfaces else "eth0"
    prim_ip = plan.ips[0] if plan.ips else "192.168.0.1"
    _write_runtime(rootfs, det, plan.network_type, bridge, iface, ip=prim_ip,
                   shell=keep_alive)
    img = image.seal(cfg, slug, rootfs)

    final_log = work / "qemu.final.serial.log"
    state.ip = plan.ips[0] if plan.ips else ""

    if keep_alive:
        _serve(cfg, arch, img, plan, qemu_init, final_log, tap, state.ip)
        state.save(work / "state.json")
        return state

    mode = f"TAP {tap}" if tap else "user-net rootless"
    print(f"[*] verify boot ({mode}, max {cfg.check_timeout}s)...")
    state.web = verify.verify_web(cfg, arch, img, plan, qemu_init, final_log, tap=tap)
    print(f"[*] web:      {state.web}")

    state.save(work / "state.json")
    return state


def _serve(cfg: Config, arch: str, img, plan: NetPlan, qemu_init: str,
           serial_log, tap: str | None, ip: str) -> None:
    """Tiene l'emulazione viva finché l'utente non interrompe (Ctrl-C)."""
    import time
    from .qemu import host_fwd_map, COMMON_TCP_PORTS
    console_sock = serial_log.parent / "console.sock"
    cmd = build_cmd(cfg, arch, img, serial_log, plan, qemu_init,
                    user_net=(tap is None), tap_name=tap, console_sock=console_sock)

    def _endpoint(guest: int, host_addr: str, host_port: int) -> str:
        label = {21: "ftp", 23: "telnet", 22: "ssh", 7547: "tr069",
                 1900: "upnp", 49152: "upnp"}.get(guest)
        if guest in (80, 8080, 8000, 8888, 443, 8443, 5000, 9000):
            return f"http{'s' if guest in (443, 8443) else ''}://{host_addr}:{host_port}/"
        return f"{host_addr}:{host_port}" + (f"  ({label})" if label else f"  (tcp/{guest})")

    inferred = {p.port for p in plan.ports if p.proto == "tcp"}
    guest_ports = sorted(inferred | set(COMMON_TCP_PORTS))
    if tap:
        # tutte le porte del guest sono raggiungibili direttamente sull'IP LAN
        lines = [_endpoint(g, ip, g) for g in guest_ports]
    else:
        m = host_fwd_map(plan)
        lines = [_endpoint(g, "127.0.0.1", m[g]) for g in guest_ports if g in m]

    if tap:
        shell_cmd = f"telnet {ip} {SHELL_PORT}"
    else:
        shell_cmd = f"telnet 127.0.0.1 {host_fwd_map(plan).get(SHELL_PORT, SHELL_PORT)}"
    qerr = serial_log.parent / "qemu.stderr.log"
    with QemuProcess(cmd, stderr_path=qerr) as q:
        print(f"[*] emulazione VIVA (serial log: {serial_log})")
        print(f"    host {'sul tap ' + tap if tap else 'via user-net'} → guest {ip}")
        for u in lines:
            print(f"      {u}")
        print(f"    >>> SHELL (rete):    {shell_cmd}   (root, senza login)")
        print(f"    >>> SHELL (seriale): socat -,raw,echo=0,escape=0x1d UNIX-CONNECT:{console_sock}")
        print("        (console di root su ttyS1, indipendente dal firmware e dalla rete;")
        print("         alternativa senza socat: nc -U " + str(console_sock) + "  —  esci con Ctrl-] )")
        print("    (porte inferite + comuni; i servizi avviati a runtime sono già mappati)")
        print("    Ctrl-C per fermare.")
        try:
            while q.proc and q.proc.poll() is None:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n[*] arresto emulazione (Ctrl-C).")
            return
        # se siamo qui, QEMU è uscito DA SOLO (non Ctrl-C): diagnostica
        rc = q.proc.returncode if q.proc else "?"
        print(f"\n[!] QEMU è uscito da solo (exit={rc}). Cause tipiche:")
        print("    - TAP inesistente/non tuo (crea con: sudo ./setup-tap.sh sae0)")
        print("    - reboot/panic del guest, o errore QEMU (arg/kvm/versione)")
        try:
            errtxt = qerr.read_text(errors="replace").strip()
            if errtxt:
                print("    --- QEMU stderr ---")
                for ln in errtxt.splitlines()[-8:]:
                    print("    " + ln[:120])
        except OSError:
            pass
        print("    --- ultime righe del serial log ---")
        try:
            tail = serial_log.read_text(errors="replace").splitlines()[-20:]
            for ln in tail:
                print("    " + ln[:120])
        except OSError:
            print("    (serial log non leggibile)")
