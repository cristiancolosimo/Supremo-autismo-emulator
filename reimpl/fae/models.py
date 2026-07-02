"""Blackboard tipizzata — rimpiazza i file singolo-valore in scratch/<iid>/ e il DB.

Una sola sorgente di verità per lo stato di un run, serializzabile in state.json.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
import json
from pathlib import Path


class NetworkType(str, Enum):
    NONE = "None"
    DEFAULT = "default"
    NORMAL = "normal"
    RELOAD = "reload"
    BRIDGE = "bridge"
    BRIDGE_RELOAD = "bridgereload"


@dataclass(frozen=True)
class Interface:
    """Interfaccia di rete inferita dal serial log."""
    ip: str
    dev: str                 # es. eth0 (senza suffisso vlan)
    vlan: int | None = None
    mac: str | None = None
    bridge: str | None = None


@dataclass(frozen=True)
class Port:
    proto: str               # tcp|udp
    ip: str
    port: int


@dataclass
class NetPlan:
    """Risultato dell'inferenza: cosa dare a QEMU e come alzare la rete nel guest."""
    interfaces: list[Interface] = field(default_factory=list)
    ports: list[Port] = field(default_factory=list)
    network_type: NetworkType = NetworkType.NONE
    is_dhcp: bool = False

    @property
    def ips(self) -> list[str]:
        # ordine stabile, deduplicato (ex ip_num / ip.<n>)
        seen: dict[str, None] = {}
        for i in self.interfaces:
            seen.setdefault(i.ip, None)
        return list(seen)


@dataclass
class RunState:
    iid: int
    firmware: str
    brand: str = "auto"
    arch: str = ""           # es. mipseb
    init: str = ""
    web_service: str | None = None
    plan: NetPlan | None = None
    ping: bool = False
    web: bool = False
    ip: str = ""

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=2, default=str))

    @classmethod
    def load(cls, path: Path) -> "RunState":
        d = json.loads(path.read_text())
        d.pop("plan", None)  # plan si ricalcola; non lo reidratiamo qui
        return cls(**{k: v for k, v in d.items() if k in cls.__annotations__})
