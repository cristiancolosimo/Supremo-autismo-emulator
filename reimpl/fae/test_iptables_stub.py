"""Check dello shim iptables (_IPTABLES_STUB): il wrapper deve forzare exit 0,
silenziare lo stderr e restare idempotente. Esegue lo snippet reale su host sh,
rimappando le dir di sistema su una tmpdir. Run: python -m fae.test_iptables_stub"""
import subprocess
import tempfile
from pathlib import Path

from .pipeline import _IPTABLES_STUB


def _run_stub(tmp: Path) -> None:
    # rimappa /sbin ... → <tmp>/sbin ..., e BB vuoto → usa mv/printf/chmod di host
    snippet = _IPTABLES_STUB.replace("/sbin /usr/sbin /bin /usr/bin",
                                     f"{tmp}/sbin {tmp}/usr/sbin")
    subprocess.run(["sh", "-c", "BB= ;\n" + snippet], check=True)


def demo() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        ipt = tmp / "sbin" / "iptables"
        ipt.parent.mkdir(parents=True)
        (tmp / "usr" / "sbin").mkdir(parents=True)
        # "vero" iptables che fallisce (come sul kernel emulato: niente netfilter)
        ipt.write_text("#!/bin/sh\necho boom >&2\nexit 1\n")
        ipt.chmod(0o755)

        _run_stub(tmp)
        assert (tmp / "sbin" / "iptables.sae_real").exists(), "reale non salvato"

        r = subprocess.run(["sh", str(ipt), "-A", "X"], capture_output=True, text=True)
        assert r.returncode == 0, f"il wrapper deve forzare exit 0 (rc={r.returncode})"
        assert r.stderr == "", f"stderr non silenziato: {r.stderr!r}"

        # idempotenza: seconda passata non deve ri-wrappare (il .sae_real resta il vero)
        _run_stub(tmp)
        assert "boom" in (tmp / "sbin" / "iptables.sae_real").read_text(), "ri-wrappato!"
    print("ok")


if __name__ == "__main__":
    demo()
