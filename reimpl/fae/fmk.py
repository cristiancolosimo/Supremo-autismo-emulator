"""fmk.py — firmware pack/unpack, sostituto stdlib-only di firmware-mod-kit.

Ciclo extract → modifica → build delegando ai tool di sistema (binwalk v3,
sasquatch, mksquashfs/unsquashfs) e reimplementando in Python SOLO taglio,
padding 0xFF e i checksum vendor (uImage / TRX / DLOB / TP-Link).

Nessuna dipendenza pip. Algoritmi portati da analisi-fmk/reference/
(crcalc/patch.c, tpl-tool/tpl-tool.c) e verificati byte-a-byte su mod10.bin.

API importabile (funzioni pure input→output, nessuno stato globale):
    from fae.fmk import info, extract, build
    info(path) -> dict                      # scan+parse layout
    extract(path, workdir) -> dict           # -> workdir/{header.bin,rootfs/,tail.bin,config.json}
    build(workdir, out=None, nopad=False) -> Path
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import subprocess
import zlib
from pathlib import Path
from shutil import which

# --- chiavi MD5 TP-Link (da tpl-tool.c) ------------------------------------
_TPL_KEY = bytes([0xdc, 0xd7, 0x3a, 0xa5, 0xc3, 0x95, 0x98, 0xfb,
                  0xdd, 0xf9, 0xe7, 0xf4, 0x0e, 0xae, 0x47, 0x38])
_TPL_KEY_BOOTLDR = bytes([0x8c, 0xef, 0x33, 0x5b, 0xd5, 0xc5, 0xce, 0xfa,
                          0xa7, 0x9c, 0x28, 0xda, 0xb2, 0xe9, 0x0f, 0x42])
_TPL_CKSUM_OFF = 76        # image_checksum[16] dentro struct image_header
_TPL_BOOTLDR_LEN_OFF = 148  # bootldr_length (u32 BE)

# nomi binwalk (v3) che portano un checksum da riparare, in ordine di priorità
_VENDOR_HEADERS = ("tplink", "trx", "dlob", "buffalo")
_FS_NAMES = ("squashfs", "cramfs", "jffs2", "yaffs")


def _tool(name: str) -> str:
    p = which(name)
    if not p:
        raise RuntimeError(f"tool richiesto non trovato nel PATH: {name}")
    return p


def _binwalk_map(firmware: Path) -> list[dict]:
    """Scansiona con binwalk v3, ritorna il file_map (lista di segnature)."""
    bw = which("binwalk") or str(Path.home() / ".cargo/bin/binwalk")
    out = Path(firmware).with_suffix(firmware.suffix + ".bwlog")
    try:
        subprocess.run([bw, "-l", str(out), "-q", str(firmware)],
                       check=True, capture_output=True)
        data = json.loads(out.read_text())
    finally:
        out.unlink(missing_ok=True)
    return data[0]["Analysis"]["file_map"]


def _grab(desc: str, pat: str, cast=str, default=None):
    m = re.search(pat, desc)
    return cast(m.group(1)) if m else default


def info(firmware, tail_offset: int | None = None) -> dict:
    """Scan+parse: ritorna il layout JSON-serializzabile del firmware.

    tail_offset: override manuale dell'inizio della coda preservata (ART/config).
    Utile su dump full-chip quando conosci la mtd layout del device.
    """
    firmware = Path(firmware)
    data = firmware.read_bytes()
    fw_size = len(data)
    fmap = _binwalk_map(firmware)

    fs = next((e for e in fmap if e["name"] in _FS_NAMES), None)
    if fs is None:
        raise RuntimeError("nessun filesystem (squashfs/cramfs/jffs2) trovato")
    fs_offset = fs["offset"]
    desc = fs["description"]
    fs_type = fs["name"]
    compression = _grab(desc, r"compression:\s*(\w+)")
    blocksize = _grab(desc, r"block size:\s*(\d+)", int)
    endianness = "big" if "big endian" in desc else "little"
    fs_image_size = _grab(desc, r"image size:\s*(\d+)", int)

    # header di checksum PRIMA del filesystem (uImage kernel, wrapper vendor…)
    headers = [{"type": e["name"], "offset": e["offset"]}
               for e in fmap
               if e["offset"] < fs_offset
               and e["name"] in ("uimage", *_VENDOR_HEADERS)]
    header_type = next((h["type"] for h in headers
                        if h["type"] in _VENDOR_HEADERS),
                       headers[0]["type"] if headers else "raw")

    # coda preservata (ART/caldata radio, config, u-boot-env) dopo lo slot
    # rootfs. Su un dump full-chip (SOIC-8) va tenuta byte-identica o brick.
    fs_end = fs_offset + fs_image_size if fs_image_size else fs_offset
    if tail_offset is None:
        tail_offset = _detect_tail(data, fs_end, fw_size)
    tail_size = fw_size - tail_offset

    cfg = {
        "firmware": str(firmware),
        "fw_size": fw_size,
        "fs_offset": fs_offset,
        "fs_type": fs_type,
        "fs_image_size": fs_image_size,
        "compression": compression,
        "blocksize": blocksize,
        "endianness": endianness,
        "header_type": header_type,
        "headers": headers,
        "tail_offset": tail_offset,
        "tail_size": tail_size,
        "slot_size": tail_offset - fs_offset,   # spazio max per il nuovo rootfs
    }
    cfg["partitions"] = _partitions(fmap, cfg)
    return cfg


# dimensione erase-block SPI tipica: 64 KB (ART/caldata vivono su blocco proprio)
_ERASE = 0x10000


def _detect_tail(data: bytes, fs_end: int, fw_size: int, erase: int = _ERASE) -> int:
    """Offset d'inizio della coda da preservare (ART/config) dopo il rootfs.

    Le partizioni di coda (ART/caldata radio, config, u-boot-env) sono allineate
    all'erase-block da 64 KB e i loro dati iniziano al bordo del blocco. Cerca il
    primo bordo di blocco dopo il rootfs il cui primo byte è non-0xFF: quella è
    la coda, preservata verbatim fino a EOF. Padding e residui NON allineati
    (zeri di fine-fs, blob sparsi) restano nello slack riutilizzabile.
    Se nessun bordo di blocco porta dati → nessuna coda (ritorna fw_size).

    ponytail: ceiling = ART che inizia con 0xFF (slot non calibrato) o boundary
    non standard → usa --tail-offset (da /proc/mtd del device).
    """
    b = ((fs_end + erase - 1) // erase) * erase   # primo bordo blocco >= fs_end
    while b < fw_size:
        if data[b] != 0xFF:
            return b
        b += erase
    return fw_size


def _partitions(fmap: list[dict], cfg: dict) -> list[dict]:
    """Breakdown a partizioni per l'analisi (derivato dai punti strutturali).

    NON è la tabella mtd del device (binwalk non la conosce): è la segmentazione
    osservabile dalle segnature — bootloader, header vendor, kernel, rootfs, coda.
    Il build usa comunque header.bin monolitico, quindi è solo informativo.
    """
    fw_size, fs_off = cfg["fw_size"], cfg["fs_offset"]
    tail_off = cfg["tail_offset"]
    # bordi noti: 0, offset di ogni header vendor/kernel, fs, fine-fs, coda, EOF
    bounds = {0, fs_off, tail_off, fw_size}
    if cfg["fs_image_size"]:      # separa il squashfs reale dal padding 0xFF
        bounds.add(fs_off + cfg["fs_image_size"])
    kernel_off = next((e["offset"] for e in fmap
                       if e["name"] == "lzma" and e["offset"] < fs_off), None)
    vendor_off = next((h["offset"] for h in cfg["headers"]
                       if h["type"] in _VENDOR_HEADERS), None)
    if vendor_off is not None:
        bounds.add(vendor_off)
    if kernel_off is not None:
        bounds.add(kernel_off)
    edges = sorted(bounds)

    def name_for(start: int) -> str:
        if start == fs_off:
            return "rootfs"
        if cfg["fs_image_size"] and start == fs_off + cfg["fs_image_size"]:
            return "padding"        # 0xFF tra rootfs e coda (slack riutilizzabile)
        if start == tail_off and cfg["tail_size"]:
            return "tail"           # ART/caldata/config/env — PRESERVARE verbatim
        if kernel_off is not None and start == kernel_off:
            return "kernel"
        if vendor_off is not None and start == vendor_off:
            return f"{cfg['header_type']}-header"
        if start == 0:
            return "bootloader"      # u-boot + eventuale uImage annidato
        return f"seg@{start:#x}"

    return [{"name": name_for(a), "offset": a, "size": b - a}
            for a, b in zip(edges, edges[1:]) if b > a]


def extract(firmware, workdir, tail_offset: int | None = None) -> dict:
    """Taglia header/rootfs/tail, scompatta il rootfs, salva config.json.

    Preserva byte-identico tutto tranne il rootfs: header.bin = [0, fs_offset),
    tail.bin = [tail_offset, EOF) (ART/config/env). Su dump full-chip questo
    garantisce che ART/caldata non venga toccato al rebuild.
    """
    firmware, workdir = Path(firmware), Path(workdir)
    cfg = info(firmware, tail_offset=tail_offset)
    data = firmware.read_bytes()
    workdir.mkdir(parents=True, exist_ok=True)

    (workdir / "header.bin").write_bytes(data[:cfg["fs_offset"]])
    if cfg["tail_size"]:
        (workdir / "tail.bin").write_bytes(data[cfg["tail_offset"]:])
    rootfs_img = workdir / "rootfs.img"
    rootfs_img.write_bytes(data[cfg["fs_offset"]:cfg["tail_offset"]])

    # partizioni nominate per l'analisi (bootloader/kernel/…); solo ispezione,
    # il build ricompone da header.bin. ponytail: dump grezzo, niente parsing.
    parts = workdir / "parts"
    parts.mkdir(exist_ok=True)
    for p in cfg["partitions"]:
        (parts / f"{p['name']}.bin").write_bytes(
            data[p["offset"]:p["offset"] + p["size"]])

    if cfg["fs_type"] != "squashfs":
        raise RuntimeError(f"scompattazione {cfg['fs_type']} non implementata "
                           "(solo squashfs); rootfs.img è comunque estratto")

    rootfs = workdir / "rootfs"
    if rootfs.exists():
        subprocess.run(["rm", "-rf", str(rootfs)], check=True)
    subprocess.run([_tool("sasquatch"), "-d", str(rootfs), str(rootfs_img)],
                   check=True, capture_output=True)

    # -no-xattrs se il fs non ha xattr (compatibilità col rebuild)
    sb = subprocess.run([_tool("unsquashfs"), "-s", str(rootfs_img)],
                        capture_output=True, text=True).stdout
    cfg["no_xattrs"] = "Number of xattr ids 0" in sb

    (workdir / "config.json").write_text(json.dumps(cfg, indent=2))
    return cfg


def build(workdir, out=None, nopad: bool = False) -> Path:
    """Ricompone header+rootfs+tail, padda lo slot a 0xFF, ripara i checksum.

    L'output ha ESATTAMENTE la dimensione del chip (fw_size) e preserva verbatim
    tutto tranne lo slot rootfs → sicuro per riscrittura full-chip via SOIC-8.
    Con --nopad l'output copre solo header+rootfs (immagine OTA parziale, NON
    adatta a flash full-chip: perderebbe la coda ART).
    """
    workdir = Path(workdir)
    cfg = json.loads((workdir / "config.json").read_text())
    out = Path(out) if out else workdir / "new.bin"

    header = (workdir / "header.bin").read_bytes()
    tail_p = workdir / "tail.bin"
    tail = tail_p.read_bytes() if tail_p.exists() else b""

    new_fs = workdir / "new-fs.img"
    new_fs.unlink(missing_ok=True)
    mk = [_tool("mksquashfs"), str(workdir / "rootfs"), str(new_fs),
          "-comp", cfg["compression"], "-b", str(cfg["blocksize"]),
          "-noappend", "-all-root"]
    if cfg.get("no_xattrs"):
        mk.append("-no-xattrs")
    subprocess.run(mk, check=True, capture_output=True)
    fs_bytes = new_fs.read_bytes()
    new_fs.unlink(missing_ok=True)

    buf = bytearray(header + fs_bytes)
    fw_size, tail_size = cfg["fw_size"], cfg["tail_size"]
    if nopad:
        _repair_checksums(buf, cfg)
        out.write_bytes(buf)
        return out

    slot_end = fw_size - tail_size          # = tail_offset originale
    if len(buf) > slot_end:
        raise RuntimeError(
            f"rootfs troppo grande per lo slot: {len(buf)} > {slot_end} byte "
            f"(sovrascriverebbe la coda ART/config → brick). "
            f"Riduci i file in rootfs/ (slack: {slot_end - len(header)} byte).")
    buf += b"\xff" * (slot_end - len(buf))  # padding slot
    buf += tail                             # coda preservata verbatim

    _repair_checksums(buf, cfg)
    assert len(buf) == fw_size, f"dimensione {len(buf)} != chip {fw_size}"
    # safety full-chip: la coda deve restare byte-identica all'originale
    assert bytes(buf[slot_end:]) == tail, "coda ART/config alterata!"
    out.write_bytes(buf)
    return out


# --- checksum engine (porting da reference/, big-endian dove serve) --------

def _repair_checksums(buf: bytearray, cfg: dict) -> None:
    for h in cfg.get("headers", []):
        fn = _FIXERS.get(h["type"])
        if fn:
            fn(buf, h["offset"])


def _fix_uimage(buf: bytearray, off: int) -> None:
    """uImage: ih_dcrc = crc32(dati, ih_size); ih_hcrc = crc32(header 64B)."""
    ih_size = struct.unpack_from(">I", buf, off + 12)[0]
    data = bytes(buf[off + 64:off + 64 + ih_size])
    dcrc = zlib.crc32(data) & 0xffffffff       # zlib applica già lo XOR finale
    struct.pack_into(">I", buf, off + 24, dcrc)
    struct.pack_into(">I", buf, off + 4, 0)    # azzera hcrc prima del calcolo
    hcrc = zlib.crc32(bytes(buf[off:off + 64])) & 0xffffffff
    struct.pack_into(">I", buf, off + 4, hcrc)


def _fix_trx(buf: bytearray, off: int) -> None:
    """TRX: crc32 da off+12 per len-12, little-endian (senza XOR extra)."""
    length = struct.unpack_from("<I", buf, off + 4)[0]
    struct.pack_into("<I", buf, off + 8, 0)
    crc = zlib.crc32(bytes(buf[off + 12:off + length])) & 0xffffffff
    struct.pack_into("<I", buf, off + 8, crc)


def _fix_dlob(buf: bytearray, off: int) -> None:
    """D-Link DLOB: MD5 sul blocco dati (header annidati, cfr patch_dlob)."""
    _, hsize, dsize = struct.unpack_from(">III", buf, off)
    ck = off + 12 + hsize + dsize
    _, chsize, cdsize = struct.unpack_from(">III", buf, ck)
    data_off = ck + 12 + chsize + 16   # DLOB_TYPE_STRING_LENGTH = 16
    digest = hashlib.md5(bytes(buf[data_off:data_off + cdsize])).digest()
    buf[ck + 12:ck + 28] = digest


def _fix_tplink(buf: bytearray, off: int) -> None:
    """TP-Link: copia la chiave fissa nel campo checksum, MD5 su [off, EOF),
    scrive il digest (algoritmo di tpl-tool: checksum(buf, size))."""
    bootldr_len = struct.unpack_from(">I", buf, off + _TPL_BOOTLDR_LEN_OFF)[0]
    key = _TPL_KEY if bootldr_len == 0 else _TPL_KEY_BOOTLDR
    ci = off + _TPL_CKSUM_OFF
    buf[ci:ci + 16] = key
    digest = hashlib.md5(bytes(buf[off:])).digest()
    buf[ci:ci + 16] = digest


def tplink_valid(buf: bytes, off: int) -> bool:
    """Verifica come tpl-tool -s: ricalcola e confronta col campo memorizzato."""
    b = bytearray(buf)
    ci = off + _TPL_CKSUM_OFF
    stored = bytes(b[ci:ci + 16])
    _fix_tplink(b, off)
    return bytes(b[ci:ci + 16]) == stored


_FIXERS = {
    "uimage": _fix_uimage,
    "trx": _fix_trx,
    "dlob": _fix_dlob,
    "tplink": _fix_tplink,
}


# --- self-test (gira su analisi-fmk/mod10.bin) -----------------------------

def _selftest() -> int:
    import tempfile
    mod10 = Path(__file__).resolve().parents[2] / "analisi-fmk" / "mod10.bin"
    if not mod10.exists():
        print(f"FAIL: {mod10} non trovato")
        return 1
    ok = True

    def check(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")

    with tempfile.TemporaryDirectory() as td:
        wd = Path(td)
        cfg = extract(mod10, wd)
        print("1) extract")
        check("fs_offset==0x120000", cfg["fs_offset"] == 0x120000)
        check("fs_type==squashfs", cfg["fs_type"] == "squashfs")
        check("compression==lzma", cfg["compression"] == "lzma")
        check("blocksize==131072", cfg["blocksize"] == 131072)
        check("header_type==tplink", cfg["header_type"] == "tplink")

        print("2) build (nessuna modifica)")
        out = build(wd)
        check("dimensione output == originale",
              out.stat().st_size == mod10.stat().st_size)

        print("3) re-binwalk stessi offset")
        orig = {h["offset"] for h in info(mod10)["headers"]} | {info(mod10)["fs_offset"]}
        new = {h["offset"] for h in info(out)["headers"]} | {info(out)["fs_offset"]}
        check("offset invariati", orig == new)

        print("4) TP-Link MD5 valido")
        tpl = next(h["offset"] for h in cfg["headers"] if h["type"] == "tplink")
        check("checksum valido", tplink_valid(out.read_bytes(), tpl))

    # 5) caso SOIC-8: dump full-chip con ART reale a 0x3f0000 → deve essere
    #    preservato byte-identico dopo un rebuild che modifica il rootfs.
    print("5) dump full-chip: ART @0x3f0000 preservato")
    with tempfile.TemporaryDirectory() as td:
        wd = Path(td)
        chip = bytearray(mod10.read_bytes())
        art = bytes(range(256)) * 256          # 64 KB di "caldata" riconoscibile
        chip[0x3f0000:0x400000] = art
        chipf = wd / "chip.bin"
        chipf.write_bytes(chip)

        c2 = extract(chipf, wd / "wk")
        check("tail rilevata a 0x3f0000", c2["tail_offset"] == 0x3f0000)
        # libera spazio nello slot (rootfs quasi pieno + mksquashfs comprime ~1%
        # peggio del vendor): rimuovi il file più grande, poi modifica il rootfs.
        rootfs = wd / "wk" / "rootfs"
        files = sorted((p for p in rootfs.rglob("*") if p.is_file()),
                       key=lambda p: p.stat().st_size, reverse=True)
        files[0].unlink()
        (rootfs / "etc" / "selftest_marker").write_text("x")
        out2 = build(wd / "wk", wd / "rebuilt.bin")
        b2 = out2.read_bytes()
        check("dimensione == chip", len(b2) == len(chip))
        check("ART byte-identico", b2[0x3f0000:0x400000] == art)
        check("bootloader byte-identico", b2[:0x20000] == bytes(chip[:0x20000]))

    print("\n" + ("TUTTO PASS" if ok else "QUALCOSA FALLITO"))
    return 0 if ok else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="fmk", description=__doc__.splitlines()[0])
    p.add_argument("--selftest", action="store_true", help="gira i test su mod10.bin")
    sub = p.add_subparsers(dest="cmd")
    pe = sub.add_parser("extract", help="estrai firmware -> workdir")
    pe.add_argument("firmware", type=Path)
    pe.add_argument("-o", "--workdir", type=Path, default=None)
    pe.add_argument("--tail-offset", type=lambda x: int(x, 0), default=None,
                    help="inizio coda preservata (ART/config), es. 0x3f0000")
    pb = sub.add_parser("build", help="ricomponi workdir -> new.bin")
    pb.add_argument("workdir", type=Path)
    pb.add_argument("-o", "--out", type=Path, default=None)
    pb.add_argument("--nopad", action="store_true")
    pi = sub.add_parser("info", help="scan+parse, stampa layout JSON")
    pi.add_argument("firmware", type=Path)
    pi.add_argument("--tail-offset", type=lambda x: int(x, 0), default=None)
    args = p.parse_args(argv)

    if args.selftest:
        return _selftest()
    if args.cmd == "info":
        print(json.dumps(info(args.firmware, tail_offset=args.tail_offset), indent=2))
    elif args.cmd == "extract":
        wd = args.workdir or args.firmware.with_suffix(".fmk")
        cfg = extract(args.firmware, wd, tail_offset=args.tail_offset)
        print(f"estratto in {wd}/  (rootfs/, header.bin, tail.bin, config.json)")
        print(json.dumps(cfg, indent=2))
    elif args.cmd == "build":
        out = build(args.workdir, args.out, args.nopad)
        print(f"scritto {out} ({out.stat().st_size} byte)")
    else:
        p.print_help()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
