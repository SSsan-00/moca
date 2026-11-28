from ctypes import byref, create_unicode_buffer, windll
from ctypes.wintypes import DWORD, HANDLE, HWND
from pathlib import Path


def _supports_sixel(kernel):
    user = windll.user32
    kernel.GetConsoleWindow.restype = user.GetWindow.restype = HWND
    # The owner of the hidden console window identifies the terminal hosting this tab.
    owner = user.GetWindow(HWND(kernel.GetConsoleWindow()), 4)  # GW_OWNER
    pid = DWORD()
    if not owner or not user.GetWindowThreadProcessId(HWND(owner), byref(pid)):
        return False
    kernel.OpenProcess.restype = HANDLE
    process = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not process:
        return False
    try:
        name = create_unicode_buffer(128)
        length = DWORD(len(name))
        if kernel.GetPackageFullName(HANDLE(process), byref(length), name):
            return False
    finally:
        kernel.CloseHandle(HANDLE(process))
    family, version, *_ = name.value.split("_")
    return (family in {"Microsoft.WindowsTerminal", "Microsoft.WindowsTerminalPreview"}
            and name.value.endswith("_8wekyb3d8bbwe")
            and tuple(map(int, version.split(".")[:2])) >= (1, 22))


def write_terminal_image(stream, console, foreground, background, accent):
    kernel, handle, screen = console
    if screen.cursor[0] or screen.attributes & 0x4000 or not _supports_sixel(kernel):
        return False
    mode = DWORD()
    if not kernel.GetConsoleMode(handle, byref(mode)):
        return False

    colors = []
    for ink in (foreground, accent):
        for level in range(1, 9):
            channels = [round((bg + (fg - bg) * level / 8) * 100 / 255) for bg, fg in zip(background, ink)]
            colors.append(f"#{len(colors) + 1};2;" + ";".join(map(str, channels)))
    try:
        raster = "".join(Path(__file__).with_name("moca.six").read_text(encoding="ascii").splitlines())
    except OSError:
        return False

    vt_mode = mode.value | 0x0001 | 0x0004  # ENABLE_PROCESSED_OUTPUT | ENABLE_VIRTUAL_TERMINAL_PROCESSING
    if mode.value != vt_mode and not kernel.SetConsoleMode(handle, vt_mode):
        return False
    try:
        # Move past the last six pixel rows before returning to text at the next line.
        stream.write('\x1bP0;1q"1;1;296;84' + "".join(colors) + raster + "-\x1b\\\n")
        stream.flush()
    finally:
        if mode.value != vt_mode:
            kernel.SetConsoleMode(handle, mode.value)
    return True
