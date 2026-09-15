#!/usr/bin/env python3
"""Read and edit Steam's binary shortcuts.vdf.

    steam-shortcut.py <shortcuts.vdf>                       # list every entry
    steam-shortcut.py <shortcuts.vdf> "<name>" "<prefix>"   # prefix its LaunchOptions

The name is matched as a substring: these titles carry trademark signs and
em-dashes that do not survive being typed through a shell.

It re-serialises what it parsed and compares against the original bytes before
writing anything, and refuses to edit if they differ — which is the only reason
it is safe to point at a live Steam config. A timestamped backup is kept.

Restart Steam afterwards: it holds shortcuts in memory and will write its own
copy over the file when it exits.
"""

import os
import shutil
import struct
import sys
import time


def parse(b):
    i = 1

    def rd_str():
        nonlocal i
        j = b.index(b"\x00", i)
        s = b[i:j]
        i = j + 1
        return s

    def rd_map():
        nonlocal i
        out = []
        while True:
            t = b[i]
            i += 1
            if t == 0x08:
                return out
            k = rd_str()
            if t == 0x00:
                out.append((t, k, rd_map()))
            elif t == 0x01:
                out.append((t, k, rd_str()))
            elif t == 0x02:
                v = struct.unpack_from("<i", b, i)[0]
                i += 4
                out.append((t, k, v))
            else:
                raise ValueError(f"unknown type {t:#x} at {i}")

    root = rd_str()
    m = rd_map()
    return root, m, b[i:]


def dump(root, m, tail):
    out = bytearray(b"\x00" + root + b"\x00")

    def wr_map(items):
        for t, k, v in items:
            out.append(t)
            out.extend(k)
            out.append(0)
            if t == 0x00:
                wr_map(v)
                out.append(0x08)
            elif t == 0x01:
                out.extend(v)
                out.append(0)
            elif t == 0x02:
                out.extend(struct.pack("<i", v))

    wr_map(m)
    out.append(0x08)
    out.extend(tail)
    return bytes(out)


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    path = sys.argv[1]
    raw = open(path, "rb").read()
    root, m, tail = parse(raw)
    if dump(root, m, tail) != raw:
        sys.exit("REFUSING to touch this file: it does not round-trip")
    print(f"round-trip ok ({len(raw)} bytes)")

    if len(sys.argv) == 2:
        for _t, idx, app in m:
            d = {k: v for _tt, k, v in app}
            name = (d.get(b"AppName") or d.get(b"appname") or b"?")
            print(f"[{idx.decode()}] {name.decode(errors='replace')}")
            for key in (b"Exe", b"exe", b"LaunchOptions"):
                val = d.get(key)
                if val:
                    print(f"      {key.decode()}: {val.decode(errors='replace')}")
        return 0

    want, prefix = sys.argv[2].encode(), sys.argv[3].encode()
    hits = 0
    for _t, _idx, app in m:
        d = {k: v for _tt, k, v in app}
        if want not in d.get(b"AppName", d.get(b"appname", b"")):
            continue
        for n, (ti, k, v) in enumerate(app):
            if k == b"LaunchOptions":
                if v.startswith(prefix):
                    print("already wired; nothing to do")
                    return 0
                app[n] = (ti, k, prefix + v)
                hits += 1
                print("old:", v.decode(errors="replace"))
                print("new:", (prefix + v).decode(errors="replace"))
    if hits != 1:
        sys.exit(f"expected exactly one match for {sys.argv[2]!r}, got {hits}")

    backup = path + f".bak-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(path, backup)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(dump(root, m, tail))
    os.replace(tmp, path)
    print("backup:", backup)
    print("restart Steam for it to take effect")
    return 0


if __name__ == "__main__":
    sys.exit(main())
