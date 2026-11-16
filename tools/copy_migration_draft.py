"""Copy a draft migration file to its configured destination in the Run worktree."""

import argparse
from pathlib import Path
from shutil import copyfile
from tempfile import TemporaryDirectory


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="absolute path to the draft file")
    parser.add_argument("destination", type=Path, help="path relative to the Run worktree")
    args = parser.parse_args()

    if not args.source.is_absolute() or not args.source.is_file():
        parser.error(f"仮移行ファイルの絶対パスを確認してください: {args.source}")

    relative = args.destination
    if relative.anchor or not relative.parts or any(part.lower() in {"..", ".git"} for part in relative.parts):
        parser.error(f"配置先にはworktree内の相対パスを指定してください: {relative}")

    destination = Path.cwd().resolve() / relative
    try:
        if destination.resolve() != destination or destination.is_symlink():
            parser.error(f"配置先にシンボリックリンクが含まれています: {relative}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=destination.parent) as directory:
            staged = Path(directory) / destination.name
            copyfile(args.source, staged)
            staged.replace(destination)
    except (OSError, RuntimeError) as exc:
        parser.error(f"仮移行ファイルを配置できませんでした: {exc}")

    print(f"配置しました: {relative}")


if __name__ == "__main__":
    main()
