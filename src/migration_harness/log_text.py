from codecs import BOM_UTF8
from os import SEEK_END
from pathlib import Path
import re


_CP932_RESERVED = re.compile(r"[\u0080\uf8f0-\uf8f3]")


def _decode_strict(raw: bytes, encoding: str) -> str | None:
    try:
        decoded = raw.decode(encoding)
    except UnicodeDecodeError:
        return None
    if encoding == "cp932" and _CP932_RESERVED.search(decoded):
        return None
    return decoded


def _split_utf8_bytes(before: bytes, raw: bytes) -> int:
    continuation = re.match(rb"[\x80-\xbf]{1,3}", raw)
    if continuation:
        for start in range(len(before)):
            joined = _decode_strict(before[start:] + continuation[0], "utf-8")
            if joined is not None and len(joined) == 1:
                return continuation.end()
    return 0


def _decode_log_line(raw: bytes, *, before: bytes = b"") -> str:
    utf8 = _decode_strict(raw, "utf-8")
    if utf8 is not None:
        return utf8
    cp932 = _decode_strict(raw, "cp932")
    if before and (cut := _split_utf8_bytes(before, raw)):
        # CP932 can also form a valid UTF-8 sequence across the boundary. Keep intact text when ambiguous.
        if cp932 is not None and (
            _decode_strict(before, "cp932") is not None
            or (len(before) == 3 and _decode_strict(before[1:], "cp932") is not None)
        ):
            return cp932
        utf8 = _decode_strict(raw[cut:], "utf-8")
        if utf8 is not None:
            return utf8
    if before and cp932 is None:
        joined = _decode_strict(before[-1:] + raw[:1], "cp932")
        if joined is not None and len(joined) == 1:
            cp932 = _decode_strict(raw[1:], "cp932")
    if cp932 is not None:
        return cp932
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
        lookbehind = min(start, 3)
        stream.seek(start - lookbehind)
        before = stream.read(lookbehind).rsplit(b"\n", 1)[-1]
        tail = stream.read(limit)
    if not before:
        return decode_log_text(tail)
    first, newline, rest = tail.partition(b"\n")
    return _decode_log_line(first, before=before) + newline.decode("ascii") + decode_log_text(rest)
