from codecs import BOM_UTF8
import re


_CP932_RESERVED = re.compile(r"[\u0080\uf8f0-\uf8f3]")


def _decode_log_line(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            decoded = raw.decode("cp932")
        except UnicodeDecodeError:
            pass
        else:
            if not _CP932_RESERVED.search(decoded):
                return decoded
        return raw.decode("utf-8", errors="replace")


def decode_log_text(raw: bytes) -> str:
    if raw.startswith(BOM_UTF8):
        return raw.decode("utf-8-sig", errors="replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return "".join(_decode_log_line(line) for line in raw.splitlines(keepends=True))
