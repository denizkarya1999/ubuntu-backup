"""Keep native backup and restore usable without the optional Flatpak backend."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ubuntu_backup import apps, core


FLATPAK_APP = {
    "name": "org.gnome.TextEditor", "branch": "stable",
    "origin": "flathub", "scope": "system",
}


class OptionalFlatpakTests(unittest.TestCase):
    def test_inventory_keeps_apt_and_snap_when_flatpak_is_absent(self):
        def run(command):
            if command[0] == "apt-mark":
                return b"vim\n"
            if command[0] == "dpkg-query":
                return b"vim\t1.0\tinstalled\nlibexample\t1.0\tinstalled\n"
            self.fail(f"Unexpected command: {command}")

        snap = {"name": "firefox", "tracking-channel": "latest/stable"}
        with patch.object(core, "run", side_effect=run), \
                patch.object(core.shutil, "which", side_effect=lambda name: None if name == "flatpak" else "/usr/bin/" + name), \
                patch.object(core, "list_snaps", return_value=[snap]):
            inventory = core.scan_inventory()
        self.assertEqual(inventory["apt"], [{"name": "vim", "version": "1.0"}])
        self.assertEqual(inventory["snap"][0]["name"], "firefox")
        self.assertEqual(inventory["flatpak"], [])
        self.assertEqual(inventory["remotes"], [])
        self.assertEqual(inventory["warnings"], [])

    def test_old_backup_restores_files_and_offers_apt_without_flatpak(self):
        inventory = {"apt": [{"name": "vim"}], "flatpak": [FLATPAK_APP]}
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, target = base / "source", base / "target"
            source.mkdir()
            target.mkdir()
            (source / "report.txt").write_text("Keep this file")
            archive = base / "legacy.ubackup"
            core.create_backup(archive, source, ["report.txt"], inventory, include_gnome=False)
            bundle = core.Bundle(archive)
            try:
                with patch.object(apps.shutil, "which", return_value=None), \
                        patch.object(apps, "run") as run, \
                        patch.object(apps, "apt_status", return_value={"vim": "available"}):
                    plan = apps.make_plan(bundle.manifest["inventory"])
                    run.assert_not_called()
                self.assertEqual([row["app"]["name"] for row in plan if row["selected"]], ["vim"])
                self.assertEqual(plan[1]["status"], "unavailable")
                core.restore_bundle(bundle, target, ["report.txt"], settings=False)
                self.assertEqual((target / "report.txt").read_text(), "Keep this file")
            finally:
                bundle.close()

    def test_selected_flatpak_without_backend_never_requests_authentication(self):
        for scope in ("user", "system"):
            with self.subTest(scope=scope), \
                    patch.object(apps.shutil, "which", return_value=None), \
                    patch.object(apps, "execute") as execute:
                result = apps.install_apps([
                    {"kind": "flatpak", "app": {**FLATPAK_APP, "scope": scope}},
                ])
                execute.assert_not_called()
                self.assertEqual(result[0][0], FLATPAK_APP["name"])
                self.assertIn("Flatpak is not installed", result[0][1])

    def test_installed_flatpak_backend_still_installs_apps(self):
        for scope in ("user", "system"):
            with self.subTest(scope=scope), \
                    patch.object(apps.shutil, "which", return_value="/usr/bin/flatpak"), \
                    patch.object(apps, "execute") as execute:
                result = apps.install_apps([
                    {"kind": "flatpak", "app": {**FLATPAK_APP, "scope": scope}},
                ])
                self.assertEqual(result, [(FLATPAK_APP["name"], "installed")])
                commands = [call.args[0] for call in execute.call_args_list]
                self.assertIn("remote-add", commands[0])
                self.assertIn("install", commands[1])
                self.assertIn("--" + scope, commands[1])
                self.assertEqual(commands[1][0] == "pkexec", scope == "system")
