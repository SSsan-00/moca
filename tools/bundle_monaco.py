"""管理者向け。Node.js/npm を使って配布用 Monaco を再生成する。"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

MONACO = "0.57.0"
ESBUILD = "0.27.0"
DESTINATION = Path(__file__).resolve().parents[1] / "src" / "migration_harness" / "monaco"


def main() -> None:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        subprocess.run(
            ["npm", "install", "--no-save", "--no-package-lock", f"monaco-editor@{MONACO}", f"esbuild@{ESBUILD}"],
            cwd=root, check=True,
        )
        package = root / "node_modules" / "monaco-editor"
        source = "./node_modules/monaco-editor/esm/vs"
        features = (package / "esm/vs/editor/editor.main.js").read_text(encoding="utf-8")
        # エディタ機能は公式エントリと同じものを使い、C#・Razor・PHP を登録する。
        contributions = features[features.index("import './contrib/anchorSelect"):].splitlines()
        imports = [
            line.replace(" './", f" '{source}/editor/").replace(" '../", f" '{source}/")
            for line in contributions if line.startswith("import ")
        ]
        entry = root / "editor.js"
        entry.write_text("\n".join((
            f'import "{source}/nls/lang/ja.js";',
            f'import "{source}/languages/definitions/csharp/register.js";',
            f'import "{source}/languages/definitions/razor/register.js";',
            f'import "{source}/languages/definitions/php/register.js";',
            *imports,
            f'export * from "{source}/editor/editor.api.js";',
        )), encoding="utf-8")
        esbuild = ["npx", "--no-install", "esbuild", "--bundle", "--minify", "--legal-comments=eof", "--format=iife"]
        subprocess.run(
            [*esbuild, "editor.js", "--global-name=monaco", "--loader:.ttf=dataurl",
             f"--banner:js=/*! Monaco Editor {MONACO} | MIT */", "--outdir=output"],
            cwd=root, check=True,
        )
        subprocess.run(
            [*esbuild, source + "/editor/editor.worker.js", "--outfile=output/worker.js"],
            cwd=root, check=True,
        )
        # file:// で開いたデモからも使えるよう、同じ worker を Blob にできる形で配布する。
        worker = root / "output/worker.js"
        worker.write_text(
            "globalThis.harnessMonacoWorker = " + json.dumps(worker.read_text(encoding="utf-8")) + ";\n",
            encoding="utf-8",
        )
        DESTINATION.mkdir(exist_ok=True)
        for name in ("editor.js", "editor.css", "worker.js"):
            shutil.copyfile(root / "output" / name, DESTINATION / name)
        for name in ("LICENSE", "ThirdPartyNotices.txt"):
            shutil.copyfile(package / name, DESTINATION / name)


if __name__ == "__main__":
    main()
