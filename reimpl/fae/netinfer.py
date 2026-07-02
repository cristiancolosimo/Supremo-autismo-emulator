"""Inferenza topologia di rete dal serial log del kernel istrumentato.

Porting tipizzato di makeNetwork.py (i 5 estrattori regex + checkNetwork), separato
dal lancio di QEMU (che vive in qemu.py). Testabile offline sui log in scratch/.

Formato riga del kernel patchato:  "[   ts] firmadyne: <fn>[pid]: <campi>"
"""
from __future__ import annotations

import re
import socket
import struct

from .models import Interface, Port, NetPlan, NetworkType

_TS = re.compile(rb"^\[[^\]]*\] firmadyne: ")


def _lines(data: bytes) -> list[bytes]:
    return [_TS.sub(b"", ln) for ln in data.split(b"\n")]


def _fmt(endian: str) -> str:
    if endian == "eb":
        return ">I"
    if endian == "el":
        return "<I"
    raise ValueError(f"endianness non valida: {endian!r}")


_IFA = re.compile(rb"^__inet_insert_ifa\[[^\]]+\]: device:([^ ]+) ifa:0x([0-9a-f]+)")
_MAC = re.compile(rb"^ioctl_SIOCSIFHWADDR\[[^\]]+\]: dev:([^ ]+) mac:0x([0-9a-f]+) 0x([0-9a-f]+)")
# due formati di kernel istrumentato: vecchio "ip:port: 0xIP:PORT", nuovo "port:PORT".
_BIND = re.compile(rb"^inet_bind\[[^\]]+\]: proto:SOCK_(DGRAM|STREAM),(?: ip:port: 0x([0-9a-f]+):| port:)([0-9]+)")
_VLAN = re.compile(rb"register_vlan_dev\[[^\]]+\]: dev:(?P<dev>[^ ]+) vlan_id:([0-9]+)")


def interfaces_with_ip(data: bytes, endian: str) -> list[tuple[str, str]]:
    fmt, out = _fmt(endian), []
    for ln in _lines(data):
        m = _IFA.match(ln)
        if not m:
            continue
        dev = m.group(1).decode()
        ip = socket.inet_ntoa(struct.pack(fmt, int(m.group(2), 16)))
        if ip not in ("127.0.0.1", "0.0.0.0"):
            out.append((dev, ip))
    return out


def mac_changes(data: bytes, endian: str) -> dict[str, str]:
    fmt, out = _fmt(endian), {}
    for ln in _lines(data):
        m = _MAC.match(ln)
        if not m:
            continue
        dev = m.group(1).decode()
        m0 = struct.pack(fmt, int(m.group(2), 16))[2:]
        m1 = struct.pack(fmt, int(m.group(3), 16))
        out[dev] = "%02x:%02x:%02x:%02x:%02x:%02x" % struct.unpack("BBBBBB", m0 + m1)
    return out


def ports(data: bytes, endian: str) -> list[Port]:
    fmt, out, seen = _fmt(endian), [], set()
    for ln in _lines(data):
        m = _BIND.match(ln)
        if not m:
            continue
        proto = "tcp" if m.group(1) == b"STREAM" else "udp"
        ip = socket.inet_ntoa(struct.pack(fmt, int(m.group(2), 16))) if m.group(2) else "0.0.0.0"
        port = int(m.group(3))
        if port and port not in seen:
            seen.add(port)
            out.append(Port(proto, ip, port))
    return out


def bridge_members(data: bytes, brif: str) -> list[str]:
    progs = [re.compile(p % re.escape(brif).encode())
             for p in (rb"^br_dev_ioctl\[[^\]]+\]: br:%s dev:(.*)",
                       rb"^br_add_if\[[^\]]+\]: br:%s dev:(.*)")]
    out = []
    for ln in _lines(data):
        for p in progs:
            m = p.match(ln)
            if m:
                dev = m.group(1).decode().strip()
                if dev != brif:          # ignora "brctl addif br0 br0" (img 5152)
                    out.append(dev)
    return out


def vlan_ids(data: bytes, dev: str) -> list[int]:
    out = []
    for ln in _lines(data):
        m = _VLAN.match(ln)
        if m and m.group("dev").decode() == dev:
            out.append(int(m.group(2)))
    return out


def _is_dhcp_ip(ip: str) -> bool:
    # ponytail: euristiche per-vendor ereditate da FirmAE (10.0.2.x = user-net, .190 = Netgear).
    # upgrade path: dedurre da lease DHCP nel log invece che da prefissi hardcoded.
    return ip.startswith("10.0.2.") or ip.endswith(".190")


def build_interfaces(data: bytes, endian: str) -> list[Interface]:
    """Combina interfacce↔bridge↔vlan↔mac (ex getNetworkList)."""
    macs = mac_changes(data, endian)
    result: list[Interface] = []
    for dev, ip in interfaces_with_ip(data, endian):
        if dev == "lo":
            continue
        members = bridge_members(data, dev)
        targets = members or [dev]
        bridge = dev if members else None
        for member in targets:
            base = member.split(".")[0]          # eth2.2 -> eth2
            vlans = vlan_ids(data, member)
            iface = Interface(
                ip=ip, dev=base,
                vlan=vlans[0] if vlans else None,
                mac=macs.get(bridge or base),
                bridge=bridge or base,
            )
            if iface not in result:
                result.append(iface)
    return result


def classify(interfaces: list[Interface]) -> tuple[list[Interface], NetworkType]:
    """Porting di checkNetwork: sceglie la topologia e scarta gli IP DHCP.

    Ritorna (interfacce_finali, tipo). Se non c'è nulla → default 192.168.0.1/eth0/br0.
    """
    if not interfaces:
        return ([Interface("192.168.0.1", "eth0", bridge="br0")], NetworkType.DEFAULT)

    devs = {i.dev for i in interfaces}
    ips = {i.ip for i in interfaces}
    net = list(interfaces)

    # router: mix eth* e bridge → tieni il bridge (scarta le eth DHCP)
    if len(devs) > 1 and any(d.startswith("eth") for d in devs) and any(not d.startswith("eth") for d in devs):
        net = [i for i in net if not i.dev.startswith("eth")]
    elif len(ips) > 1 and any(ip.startswith("10.0.2.") for ip in ips) and any(not ip.startswith("10.0.2.") for ip in ips):
        net = [i for i in net if not i.ip.startswith("10.0.2.")]

    def valid(i: Interface) -> bool:
        return not i.ip.endswith(".0.0.0")

    eth_vlan = [i for i in net if valid(i) and i.dev.startswith("eth") and i.vlan is not None]
    eth = [i for i in net if valid(i) and i.dev.startswith("eth")]
    eth_bad = [i for i in net if not valid(i) and i.dev.startswith("eth")]
    br = [i for i in net if valid(i) and not i.dev.startswith("eth")]
    br_bad = [i for i in net if not valid(i) and not i.dev.startswith("eth")]

    devlist = ["eth0", "eth1", "eth2", "eth3"]
    if eth_vlan or eth:
        return ((eth_vlan or eth), NetworkType.NORMAL)
    if eth_bad:
        return ([Interface("192.168.0.1", i.dev, i.vlan, i.mac, i.bridge) for i in eth_bad], NetworkType.RELOAD)
    if br:
        return ([Interface(i.ip, devlist[n], i.vlan, i.mac, i.bridge) for n, i in enumerate(br) if n < 4], NetworkType.BRIDGE)
    if br_bad:
        return ([Interface("192.168.0.1", devlist[n], i.vlan, i.mac, i.bridge) for n, i in enumerate(br_bad) if n < 4], NetworkType.BRIDGE_RELOAD)
    return ([Interface("192.168.0.1", "eth0", bridge="br0")], NetworkType.DEFAULT)


def infer(data: bytes, endian: str) -> NetPlan:
    raw = build_interfaces(data, endian)
    ifaces, ntype = classify(raw)
    plan = NetPlan(interfaces=ifaces, ports=ports(data, endian), network_type=ntype)
    plan.is_dhcp = any(_is_dhcp_ip(ip) for ip in plan.ips)
    return plan


def host_ip(guest_ip: str) -> str:
    """IP host per il TAP: guest .1 -> host .2, altrimenti -1 (ex convertToHostIp)."""
    t = [int(x) for x in guest_ip.split(".")]
    t[3] = t[3] - 1 if t[3] > 1 else t[3] + 1
    return ".".join(map(str, t))


def _demo() -> None:
    """Self-check offline: nessun QEMU, solo il parser sul formato di log reale."""
    log = (
        b"[    1.0] firmadyne: __inet_insert_ifa[100]: device:eth0 ifa:0x0101a8c0\n"   # 192.168.1.1 (el)
        b"[    1.1] firmadyne: br_add_if[101]: br:br0 dev:eth0\n"
        b"[    1.2] firmadyne: inet_bind[102]: proto:SOCK_STREAM, ip:port: 0x00000000:80\n"
        b"[    1.3] firmadyne: ioctl_SIOCSIFHWADDR[103]: dev:eth0 mac:0x23120000 0xab896745\n"
    )
    plan = infer(log, "el")
    assert plan.ips == ["192.168.1.1"], plan.ips
    assert plan.network_type is NetworkType.NORMAL, plan.network_type
    assert any(p.port == 80 and p.proto == "tcp" for p in plan.ports)
    assert plan.interfaces[0].mac == "12:23:45:67:89:ab", plan.interfaces[0].mac
    assert plan.interfaces[0].vlan is None

    # nessuna interfaccia -> default network (caso originale.bin/dlink in scratch/1)
    empty = infer(b"[ 0.0] firmadyne: something else\n", "eb")
    assert empty.network_type is NetworkType.DEFAULT
    assert empty.interfaces[0].ip == "192.168.0.1"

    assert host_ip("192.168.0.1") == "192.168.0.2"
    assert host_ip("192.168.0.0") == "192.168.0.1"  # .0 -> .1
    print("netinfer self-check OK")


if __name__ == "__main__":
    _demo()
