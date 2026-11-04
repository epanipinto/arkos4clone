"""Write a board file (.dtb) out as a /proc/device-tree style folder: one folder per node, one file per property.

python3 dtb2fs.py <board.dtb> <out-dir>
"""

import struct
import sys
from pathlib import Path

FDT_BEGIN_NODE, FDT_END_NODE, FDT_PROP, FDT_NOP, FDT_END = 1, 2, 3, 4, 9


def dtb_to_fs(dtb: bytes, out: Path) -> int:
    magic, _total, off_struct, off_strings = struct.unpack_from(">4I", dtb, 0)
    if magic != 0xD00DFEED:
        raise SystemExit("not a device tree blob")
    pos, path, count = off_struct, [], 0
    while True:
        (token,) = struct.unpack_from(">I", dtb, pos)
        pos += 4
        if token == FDT_BEGIN_NODE:
            end = dtb.index(b"\0", pos)
            path.append(dtb[pos:end].decode())
            pos = (end + 4) & ~3
            Path(out, *[p for p in path if p]).mkdir(parents=True, exist_ok=True)
        elif token == FDT_END_NODE:
            path.pop()
        elif token == FDT_PROP:
            length, name_off = struct.unpack_from(">II", dtb, pos)
            pos += 8
            name = dtb[off_strings + name_off : dtb.index(b"\0", off_strings + name_off)].decode()
            Path(out, *[p for p in path if p], name).write_bytes(dtb[pos : pos + length])
            pos = (pos + length + 3) & ~3
            count += 1
        elif token == FDT_NOP:
            continue
        elif token == FDT_END:
            return count
        else:
            raise SystemExit(f"bad token {token}")


if __name__ == "__main__":
    print(dtb_to_fs(Path(sys.argv[1]).read_bytes(), Path(sys.argv[2])), "properties")
