"""Regenerate the shortcut checker's table of static members of test library classes.

src/rootflake/gate/checker/library_members.json lists, for each test library class a test may
import on demand statically, the public static method and field names of one pinned release,
read from that release's class files. Each entry records the jar it came from. To move a pin,
edit the entry's version and source URL, then run from the repository root:

    uv run python tools/library_members.py          # rewrite the table
    uv run python tools/library_members.py --check  # exit 1 if the table is out of date

Each jar is downloaded once, checked against the SHA1 that Maven Central publishes beside it,
and read without running any of its code. Static methods come from the class and its
superclasses; fields come from superinterfaces too, since an interface's static methods are not
inherited (JLS 8.4.8) but its fields are.
"""

import hashlib
import json
import struct
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

TABLE = Path(__file__).resolve().parent.parent / "src/rootflake/gate/checker/library_members.json"

_PUBLIC, _STATIC, _BRIDGE, _INTERFACE, _SYNTHETIC = 0x0001, 0x0008, 0x0040, 0x0200, 0x1000
_SKIP = {3: 4, 4: 4, 5: 8, 6: 8, 8: 2, 9: 4, 10: 4, 11: 4, 12: 4, 15: 3, 16: 2, 17: 4, 18: 4,
         19: 2, 20: 2}  # fmt: skip


def parse_class(data: bytes) -> dict:
    """The access flags, supertypes and member names of one class file (JVMS 4.1)."""
    (count,) = struct.unpack_from(">H", data, 8)
    pool: list = [None] * count
    pos, i = 10, 1
    while i < count:
        tag = data[pos]
        pos += 1
        if tag == 1:
            (length,) = struct.unpack_from(">H", data, pos)
            pool[i] = data[pos + 2 : pos + 2 + length].decode("utf-8", "surrogatepass")
            pos += 2 + length
        elif tag == 7:
            (pool[i],) = struct.unpack_from(">H", data, pos)
            pos += 2
        elif tag in _SKIP:
            pos += _SKIP[tag]
        else:
            raise ValueError(f"unknown constant pool tag {tag}")
        i += 2 if tag in (5, 6) else 1  # a long or a double takes two entries

    def class_name(index: int) -> str | None:
        return None if index == 0 else pool[pool[index]]

    access, this_index, super_index, interface_count = struct.unpack_from(">HHHH", data, pos)
    pos += 8
    interfaces = []
    for _ in range(interface_count):
        (index,) = struct.unpack_from(">H", data, pos)
        interfaces.append(class_name(index))
        pos += 2

    def members() -> list[tuple[int, str]]:
        nonlocal pos
        (n,) = struct.unpack_from(">H", data, pos)
        pos += 2
        out = []
        for _ in range(n):
            flags, name_index, _descriptor, attributes = struct.unpack_from(">HHHH", data, pos)
            pos += 8
            for _ in range(attributes):
                (_name, length) = struct.unpack_from(">HI", data, pos)
                pos += 6 + length
            out.append((flags, pool[name_index]))
        return out

    fields = members()
    methods = members()
    return {
        "name": class_name(this_index),
        "access": access,
        "super": class_name(super_index),
        "interfaces": interfaces,
        "fields": fields,
        "methods": methods,
    }


def members_of(fqn: str, jar: zipfile.ZipFile) -> tuple[list[str], list[str]]:
    """The public static method and field names a static import on demand of fqn brings in."""
    methods, fields, seen = set(), set(), set()
    pending = [(fqn.replace(".", "/"), False)]
    while pending:
        internal, through_class = pending.pop()
        if internal in seen or internal == "java/lang/Object":
            continue
        seen.add(internal)
        try:
            parsed = parse_class(jar.read(internal + ".class"))
        except KeyError:
            if internal.startswith("java/"):
                continue  # a JDK supertype declares no test library member
            raise SystemExit(f"{fqn}: supertype {internal} is not in the jar") from None
        interface = bool(parsed["access"] & _INTERFACE)
        for flags, name in parsed["fields"]:
            if not flags & _SYNTHETIC and (interface or flags & _PUBLIC and flags & _STATIC):
                fields.add(name)
        for flags, name in parsed["methods"]:
            if flags & (_SYNTHETIC | _BRIDGE) or name.startswith("<"):
                continue
            if interface and through_class:
                continue
            if flags & _PUBLIC and flags & _STATIC:
                methods.add(name)
        if parsed["super"]:
            pending.append((parsed["super"], True))
        pending.extend((name, True) for name in parsed["interfaces"])
    return sorted(methods), sorted(fields)


def _download(url: str, folder: Path) -> Path:
    target = folder / url.rsplit("/", 1)[-1]
    if not target.exists():
        with urllib.request.urlopen(url, timeout=60) as response:
            target.write_bytes(response.read())
        with urllib.request.urlopen(url + ".sha1", timeout=60) as response:
            expected = response.read().decode("ascii").split()[0]
        actual = hashlib.sha1(target.read_bytes()).hexdigest()
        if actual != expected:
            raise SystemExit(f"{url}: SHA1 {actual} does not match the published {expected}")
    return target


def build(table: dict) -> dict:
    out = {}
    with tempfile.TemporaryDirectory() as folder:
        for fqn, entry in table.items():
            with zipfile.ZipFile(_download(entry["source"], Path(folder))) as jar:
                methods, fields = members_of(fqn, jar)
            out[fqn] = {
                "version": entry["version"],
                "source": entry["source"],
                "methods": methods,
                "fields": fields,
            }
    return out


def main(argv: list[str]) -> int:
    current = json.loads(TABLE.read_text(encoding="utf-8"))
    fresh = build(current)
    if "--check" in argv:
        if fresh != current:
            print(f"{TABLE} is out of date; run tools/library_members.py", file=sys.stderr)
            return 1
        return 0
    TABLE.write_text(json.dumps(fresh, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
