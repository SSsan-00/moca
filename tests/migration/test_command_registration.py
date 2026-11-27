import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

if os.name == "nt":
    import winreg

    from tools import register_moca


@unittest.skipUnless(os.name == "nt", "Windows command registration")
class CommandRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        name = rf"Software\MoCA-test-{uuid4().hex}"
        self.key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, name)
        self.addCleanup(winreg.DeleteKey, winreg.HKEY_CURRENT_USER, name)
        self.addCleanup(self.key.Close)
        self.bin = self.temp / "checkout & tools" / "bin"
        self.bin.mkdir(parents=True)
        system = Path(os.environ["SystemRoot"]) / "System32"
        self.powershell = system / "WindowsPowerShell/v1.0/powershell.exe"
        self.enterContext(patch.dict(os.environ, {"PATH": str(system)}))

    def test_raw_path_type_registration_and_removal(self):
        for kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
            with self.subTest(kind=kind):
                old = r"%SystemRoot%\System32;C:\Tools;"
                winreg.SetValueEx(self.key, "Path", 0, kind, old)
                self.assertTrue(register_moca.update_registry(self.key, self.bin, remove=False))
                self.assertFalse(register_moca.update_registry(self.key, str(self.bin).upper() + "\\", remove=False))
                self.assertEqual(winreg.QueryValueEx(self.key, "Path"), (old + ";" + str(self.bin), kind))
                self.assertTrue(register_moca.update_registry(self.key, self.bin, remove=True))
                self.assertEqual(winreg.QueryValueEx(self.key, "Path"), (old, kind))
                self.assertFalse(register_moca.update_registry(self.key, self.bin, remove=True))

    def test_conflicting_command_is_left_untouched(self):
        other = self.temp / "other"
        other.mkdir()
        command = other / "moca.ps1"
        command.write_text("throw 'other'")
        winreg.SetValueEx(self.key, "Path", 0, winreg.REG_SZ, str(other))
        with self.assertRaisesRegex(ValueError, "moca.ps1"):
            register_moca.update_registry(self.key, self.bin, remove=False)
        self.assertEqual(winreg.QueryValueEx(self.key, "Path"), (str(other), winreg.REG_SZ))
        self.assertEqual(command.read_text(), "throw 'other'")

    def test_registered_path_resolves_checkout_from_unrelated_powershell_directory(self):
        shutil.copyfile(register_moca.BIN / "moca.cmd", self.bin / "moca.cmd")
        (self.bin.parent / "moca.cmd").write_text("@echo off\necho anchored-launcher\nexit /b 37\n")
        register_moca.update_registry(self.key, self.bin, remove=False)
        entry, _ = winreg.QueryValueEx(self.key, "Path")
        caller = self.temp / "caller"
        caller.mkdir()
        result = subprocess.run(
            [str(self.powershell), "-NoProfile", "-NonInteractive", "-Command",
             "(Get-Command moca).Path; moca; exit $LASTEXITCODE"],
            cwd=caller, env={**os.environ, "PATH": os.environ["PATH"] + ";" + entry},
            capture_output=True, text=True, errors="replace", timeout=20,
        )
        self.assertEqual(result.returncode, 37, result.stderr + result.stdout)
        self.assertEqual(result.stdout.splitlines(), [str(self.bin / "moca.cmd"), "anchored-launcher"])
