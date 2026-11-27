"""Register only this checkout's MoCA launcher on the Windows user PATH."""

import argparse
import ctypes
import os
import winreg
from pathlib import Path


BIN = Path(__file__).resolve().parents[1] / "bin"


def path_key(entry):
    return os.path.normcase(os.path.normpath(os.path.expandvars(entry.strip('"'))))


def updated_path(value, directory, *, remove):
    if any(char in str(directory) for char in ";%"):
        raise ValueError("PATH 登録には ; と % を含まないフォルダーを使ってください。")
    parts = value.split(";") if value else []
    key = path_key(str(directory))
    matches = [path_key(part) == key for part in parts]
    if remove:
        return ";".join(part for part, match in zip(parts, matches) if not match)
    if any(matches):
        return value
    return ";".join([*parts, str(directory)])


def conflicting_command(search_path, directory):
    extensions = {".ps1", *os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").lower().split(";")}
    for part in search_path.split(";"):
        if not part or path_key(part) == path_key(str(directory)):
            continue
        folder = Path(path_key(part))
        for extension in extensions:
            candidate = folder / ("moca" + extension)
            if candidate.is_file():
                return candidate
    return None


def update_registry(key, directory, *, remove):
    try:
        old, kind = winreg.QueryValueEx(key, "Path")
    except FileNotFoundError:
        old, kind = "", winreg.REG_EXPAND_SZ
    if kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
        raise ValueError("現在のユーザー PATH が文字列ではないため変更しません。")
    if not remove:
        conflict = conflicting_command(os.environ.get("PATH", "") + ";" + old, directory)
        if conflict:
            raise ValueError(f"同名コマンドが既にあります: {conflict}。既存のコマンドは変更しません。")
    new = updated_path(old, directory, remove=remove)
    if new == old:
        return False
    winreg.SetValueEx(key, "Path", 0, kind, new)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remove", action="store_true", help="Remove this launcher's PATH entry.")
    args = parser.parse_args()
    if not args.remove and not (BIN.parent / ".venv/Scripts/php-to-csharp-harness.exe").is_file():
        parser.exit(2, "先に MoCA の .venv を作成し、pip install -e . を実行してください。\n")
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as key:
            changed = update_registry(key, BIN, remove=args.remove)
    except (OSError, ValueError) as error:
        parser.exit(1, f"PATH を変更できませんでした: {error}\n")
    if changed:
        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x1A, 0, "Environment", 0x0002, 1000, None)
    print(f"ユーザー PATH {'解除' if args.remove else '登録'}: {BIN}")
    print("ターミナルをすべて閉じ、スタートメニューから PowerShell を開き直してください。")


if __name__ == "__main__":
    main()
