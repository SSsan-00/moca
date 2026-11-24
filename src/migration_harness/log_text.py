from codecs import BOM_UTF8
from os import SEEK_END
from pathlib import Path
import re


_CP932_RESERVED = re.compile(r"[\u0080\uf8f0-\uf8f3]")


def _decode_log_line(raw: bytes, *, partial: bool = False) -> str:
    candidates = [("utf-8", raw)]
    if partial and (cut := re.match(rb"^[\x80-\xbf]{1,3}", raw)):
        candidates.append(("utf-8", raw[cut.end():]))
    candidates.append(("cp932", raw))
    if partial:
        candidates.append(("cp932", raw[1:]))
    for encoding, data in candidates:
        try:
            decoded = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        if encoding != "cp932" or not _CP932_RESERVED.search(decoded):
            return decoded
    return raw.decode("utf-8", errors="replace")


def decode_log_text(raw: bytes) -> str:
    if raw.startswith(BOM_UTF8):
        return raw.decode("utf-8-sig", errors="replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return "".join(_decode_log_line(line) for line in raw.splitlines(keepends=True))


def read_log_tail(path: Path) -> str:
    limit = 65_536
    with path.open("rb") as stream:
        start = max(0, stream.seek(0, SEEK_END) - limit)
        stream.seek(max(0, start - 1))
        partial = start > 0 and stream.read(1) != b"\n"
        tail = stream.read(limit)
    if not partial:
        return decode_log_text(tail)
    first, newline, rest = tail.partition(b"\n")
    return _decode_log_line(first, partial=True) + newline.decode("ascii") + decode_log_text(rest)
