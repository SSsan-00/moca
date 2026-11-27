from os import environ, get_terminal_size


# ダッシュボードと同じ矢印。端末用の細い字形を半マスで描き、中央列の o だけを着色する。
_BRAND = (
    ("                       ▄▄        ██▄     ▄██ ", "       ", "   ▄█▀▀█▄      ▄█▄"),
    ("         ▄██▄          ███▄      ██ █   █ ██ ", "  ▄▄▄  ", " ▄█▀    ▀▀    ▄█ █▄"),
    ("      ▄███████▄       ▄█████▄    ██  █ █  ██ ", "▄█▀▀▀█▄", " ██          ▄█   █▄"),
    ("    ▄███▀  ▀███▄   ▄█████████▀   ██   █   ██ ", "█     █", " ██          █▀   ▀█"),
    ("  ▄███▀      ▀██████▀▀ ███▀      ██       ██ ", "█▄   ▄█", " ▀█▄    ▄▄  ██▀▀▀▀▀██"),
    ("  ▀▀▀          ▀█▀▀    ▀         ██       ██ ", " ▀███▀ ", "   ▀█▄▄█▀  ██       ██"),
)
_ASCII = (
    ("    /\\      |\\", "", ""),
    ("   /  \\    /|>  M", "o", "CA"),
    (r"  /    \__/ |/", "", ""),
)
_WORD = (("M", "o", "CA"),)


def _layout(columns, encoding):
    for rows in (_BRAND, _ASCII, _WORD):
        if max(sum(map(len, row)) for row in rows) >= columns:
            continue
        try:
            "".join(part for row in rows for part in row).encode(encoding or "ascii")
        except (UnicodeEncodeError, LookupError):
            if rows is _BRAND:
                continue
        return rows
    return ()


def _accent_index(table, attributes):
    def rgb(color):
        return tuple((color >> channel) & 255 for channel in (0, 8, 16))

    def luminance(channels):
        values = (value / 255 for value in channels)
        return sum(weight * (value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4)
                   for weight, value in zip((.2126, .7152, .0722), values))

    if attributes & 0x4000:  # Inverse video makes those foreground bits the displayed background.
        return None
    bg = luminance(rgb(table[(attributes >> 4) & 15]))
    target = (226, 164, 124) if bg < .18 else (135, 77, 49)
    candidates = []
    for index, color in enumerate(table):
        r, g, b = channels = rgb(color)
        if color == table[attributes & 15] or r < g or g + 35 < b or r - b < 24:
            continue
        ink = luminance(channels)
        if (max(bg, ink) + .05) / (min(bg, ink) + .05) >= 3:
            candidates.append((sum((actual - desired) ** 2 for actual, desired in zip(channels, target)), index))
    return min(candidates)[1] if candidates else None


def _console_state(stream):
    from ctypes import Structure, byref, c_short, sizeof, windll
    from ctypes.wintypes import BOOL, DWORD, HANDLE, WORD
    from msvcrt import get_osfhandle

    class Screen(Structure):
        _fields_ = [
            ("size", DWORD), ("buffer", c_short * 2), ("cursor", c_short * 2), ("attributes", WORD),
            ("window", c_short * 4), ("maximum", c_short * 2), ("popup", WORD), ("fullscreen", BOOL),
            ("palette", DWORD * 16),
        ]

    handle = HANDLE(get_osfhandle(stream.fileno()))
    screen = Screen(size=sizeof(Screen))
    kernel = windll.kernel32
    return (kernel, handle, screen) if kernel.GetConsoleScreenBufferInfoEx(handle, byref(screen)) else None


def _color_wordmark(console, rows):
    from ctypes import byref, c_uint32, create_unicode_buffer
    from ctypes.wintypes import DWORD, WORD

    kernel, handle, before = console
    accent = _accent_index(before.palette, before.attributes)
    if accent is None or before.cursor[0]:
        return

    after = type(before)(size=before.size)
    if not kernel.GetConsoleScreenBufferInfoEx(handle, byref(after)) or after.cursor[0]:
        return
    if after.cursor[1] != before.cursor[1] + len(rows) or after.buffer[:] != before.buffer[:]:
        return

    cells = []
    for row, (left, middle, _) in enumerate(rows):
        marks = middle.strip()
        if not marks:
            continue
        x = len(left) + len(middle) - len(middle.lstrip())
        position = c_uint32(((before.cursor[1] + row) << 16) | x)
        found, count = create_unicode_buffer(len(marks) + 1), DWORD()
        if (not kernel.ReadConsoleOutputCharacterW(handle, found, len(marks), position, byref(count))
                or count.value != len(marks) or found.value != marks):
            return
        cells.append((position, marks))
    # Only change verified cells. Leave the mode, palette, cursor and following output alone.
    for position, marks in cells:
        colors = (WORD * len(marks))(*(
            before.attributes if char == " " else (before.attributes & ~15) | accent for char in marks
        ))
        if not kernel.WriteConsoleOutputAttribute(handle, colors, len(marks), position, byref(DWORD())):
            return


def write_banner(stream):
    try:
        columns = get_terminal_size(stream.fileno()).columns
    except OSError:
        columns = 5
    rows = _layout(columns, stream.encoding)
    if not rows:
        return
    try:
        console = None if environ.get("NO_COLOR") else _console_state(stream)
    except (OSError, ValueError):
        console = None
    print("\n".join("".join(row) for row in rows), file=stream, flush=True)
    if console is not None:
        _color_wordmark(console, rows)
