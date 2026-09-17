import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from ubuntu_backup import apps, automation, core, updates

EMPTY = {"apt": [], "snap": [], "flatpak": []}


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.source = self.base / "lenovo-user"
        self.target = self.base / "dell-user"
        self.source.mkdir()
        self.target.mkdir()
        self.archive = self.base / "test.ubackup"

    def put(self, home, name, data=b"sample"):
        p = home / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def backup(self, roots, **kw):
        core.create_backup(self.archive, self.source, roots, EMPTY, include_gnome=False, **kw)
        bundle = core.Bundle(self.archive)
        self.addCleanup(bundle.close)
        return bundle

    def test_personal_roundtrip_selective_restore_and_undo(self):
        original = self.put(self.source, "Documents/report.txt", b"my important report")
        original.chmod(0o640)
        os.utime(original, (1700000000, 1700000000))
        self.put(self.source, "Pictures/new photo.jpg", b"\xff\xd8\x00photo")
        self.put(self.source, "Downloads/not-selected.txt", b"skip")
        self.put(self.target, "Documents/report.txt", b"previous Dell report")
        self.put(self.target, "Documents/unrelated.txt", b"keep")
        bundle = self.backup(["Documents/report.txt", "Pictures/new photo.jpg", "Downloads/not-selected.txt"])
        recovery = core.restore_bundle(bundle, self.target, ["Documents/report.txt", "Pictures/new photo.jpg"], settings=False)
        self.assertTrue(Path(recovery).exists())
        self.assertEqual((self.target / "Documents/report.txt").read_bytes(), b"my important report")
        self.assertEqual(stat.S_IMODE((self.target / "Documents/report.txt").stat().st_mode), 0o640)
        self.assertEqual((self.target / "Documents/report.txt").stat().st_mtime, 1700000000)
        self.assertFalse((self.target / "Downloads/not-selected.txt").exists())
        self.assertEqual((self.target / "Documents/unrelated.txt").read_bytes(), b"keep")
        core.undo_last(self.target)
        self.assertEqual((self.target / "Documents/report.txt").read_bytes(), b"previous Dell report")
        self.assertFalse((self.target / "Pictures/new photo.jpg").exists())

    def test_different_usernames_rewrite_config_only(self):
        value = (str(self.source) + "/Documents\n").encode()
        self.put(self.source, ".config/example/prefs", value)
        self.put(self.source, "Documents/notes.txt", value)
        bundle = self.backup([".config/example", "Documents/notes.txt"])
        core.restore_bundle(bundle, self.target, bundle.manifest["roots"], settings=False)
        self.assertEqual((self.target / ".config/example/prefs").read_bytes(), (str(self.target) + "/Documents\n").encode())
        self.assertEqual((self.target / "Documents/notes.txt").read_bytes(), value)

    def test_explicit_personal_hidden_files_never_rewritten_or_cache_filtered(self):
        value = (str(self.source) + "/exact/path").encode()
        self.put(self.source, ".personal/Cache/entry", value)
        self.put(self.source, ".personal/.git/config", value)
        bundle = self.backup([".personal"], personal_roots=[".personal"])
        core.restore_bundle(bundle, self.target, [".personal"], settings=False)
        self.assertEqual((self.target / ".personal/Cache/entry").read_bytes(), value)
        self.assertEqual((self.target / ".personal/.git/config").read_bytes(), value)

    def test_username_rewrite_does_not_match_a_longer_username(self):
        data = b"/home/ann/file /home/anna/file file:///home/ann/file"
        self.assertEqual(core.adapt_home(data, "/home/ann", "/home/bob"), b"/home/bob/file /home/anna/file file:///home/bob/file")

    def test_binary_configuration_preserved(self):
        value = b"\x00" + str(self.source).encode() + b"/private"
        self.put(self.source, ".config/app/binary", value)
        bundle = self.backup([".config/app"])
        core.restore_bundle(bundle, self.target, [".config/app"], settings=False)
        self.assertEqual((self.target / ".config/app/binary").read_bytes(), value)

    def test_backup_does_not_overwrite_existing_archive(self):
        self.archive.write_bytes(b"existing")
        with self.assertRaises(core.TransferError):
            core.create_backup(self.archive, self.source, [], EMPTY, False)
        self.assertEqual(self.archive.read_bytes(), b"existing")

    def test_selection_accepts_files_and_totals_above_64_gib(self):
        # Sparse files exercise the real size checks without allocating or
        # writing 130 GiB to the test runner's disk.
        for name in ("large-a.img", "large-b.img"):
            path = self.put(self.source, "Documents/" + name, b"")
            with path.open("r+b") as stream:
                stream.truncate(65 * 1024 ** 3)
        files = core.collect_files(self.source, ["Documents"], lambda _: None)
        self.assertEqual(len(files), 2)
        self.assertEqual(sum(st.st_size for _, _, st in files), 130 * 1024 ** 3)

    def test_selection_accepts_more_than_200000_files(self):
        # A virtual directory checks the count boundary without creating
        # hundreds of thousands of physical files in CI.
        class VirtualFile:
            def __init__(self, index): self.name = f"file-{index:06d}"
            def __lt__(self, other): return self.name < other.name
            def lstat(self): return os.stat_result((stat.S_IFREG | 0o600, 0, 0, 1, 0, 0, 1, 0, 0, 0))
        class VirtualDirectory:
            name = "Documents"
            def exists(self): return True
            def lstat(self): return os.stat_result((stat.S_IFDIR | 0o700, 0, 0, 1, 0, 0, 0, 0, 0, 0))
            def iterdir(self): return (VirtualFile(i) for i in range(200001))
        with patch.object(core, "target_path", return_value=VirtualDirectory()):
            files = core.collect_files(self.source, ["Documents"], lambda _: None)
        self.assertEqual(len(files), 200001)

    def test_finished_archive_is_published_without_copying_its_bytes(self):
        temporary = self.base / "partial"
        temporary.write_bytes(b"complete archive")
        inode = temporary.stat().st_ino
        with patch.object(core.shutil, "copyfileobj", side_effect=AssertionError("Archive must not be duplicated")):
            core.publish_archive(temporary, self.archive)
        self.assertEqual(self.archive.stat().st_ino, inode)
        self.assertEqual(self.archive.read_bytes(), b"complete archive")

    def test_publication_does_not_replace_a_racing_destination(self):
        temporary = self.base / "partial"
        temporary.write_bytes(b"new backup")
        self.archive.write_bytes(b"another backup")
        with self.assertRaises(FileExistsError):
            core.publish_archive(temporary, self.archive)
        self.assertEqual(self.archive.read_bytes(), b"another backup")

    def test_verification_does_not_extract_payload_to_temporary_storage(self):
        self.put(self.source, "Documents/data", b"content" * 300000)
        core.create_backup(self.archive, self.source, ["Documents"], EMPTY, False)
        with patch.object(core.tempfile, "TemporaryDirectory", side_effect=AssertionError("No staging directory")), patch.object(core.shutil, "disk_usage", side_effect=AssertionError("No verification disk allocation")):
            bundle = core.Bundle(self.archive)
        self.addCleanup(bundle.close)
        self.assertEqual(bundle.files["home/Documents/data"]["size"], 2100000)
        core.restore_bundle(bundle, self.target, ["Documents"], settings=False)
        self.assertEqual((self.target / "Documents/data").read_bytes(), b"content" * 300000)

    def test_archive_changed_after_review_is_rejected_before_restore(self):
        self.put(self.source, "Documents/a", b"new")
        self.put(self.target, "Documents/a", b"old")
        bundle = self.backup(["Documents"])
        with self.archive.open("ab") as stream:
            stream.write(b"modified")
        with self.assertRaisesRegex(core.TransferError, "changed after verification"):
            core.restore_bundle(bundle, self.target, ["Documents"], settings=False)
        self.assertEqual((self.target / "Documents/a").read_bytes(), b"old")

    def test_second_pass_checksum_failure_rolls_back_restored_files(self):
        for name in ("a", "b"):
            self.put(self.source, "Documents/" + name, b"new")
            self.put(self.target, "Documents/" + name, b"old")
        bundle = self.backup(["Documents"])
        with tarfile.open(self.archive, "r:gz") as archive:
            contents = [(member, archive.extractfile(member).read()) for member in archive]
        with tarfile.open(self.archive, "w:gz") as archive:
            for member, data in contents:
                archive.addfile(member, io.BytesIO(b"bad" if member.name == "home/Documents/b" else data))
        # Even if a filesystem's change detection were defeated, every selected
        # file is re-hashed before being published on the destination.
        with patch.object(bundle, "assert_unchanged"):
            with self.assertRaisesRegex(core.TransferError, "checksum mismatch"):
                core.restore_bundle(bundle, self.target, ["Documents"], settings=False)
        for name in ("a", "b"):
            self.assertEqual((self.target / "Documents" / name).read_bytes(), b"old")

    def test_insufficient_destination_space_is_still_reported(self):
        self.put(self.source, "Documents/a", b"new")
        bundle = self.backup(["Documents"])
        with patch.object(core.shutil, "disk_usage", return_value=core.shutil._ntuple_diskusage(100, 99, 1)):
            with self.assertRaisesRegex(core.TransferError, "Not enough space"):
                core.restore_bundle(bundle, self.target, ["Documents"], settings=False)
        self.assertFalse((self.target / "Documents/a").exists())

    def test_configuration_caches_symlinks_and_credentials_excluded(self):
        self.put(self.source, ".config/app/Cache/temp")
        self.put(self.source, ".config/app/settings", b"prefs")
        self.put(self.source, ".config/dconf/user", b"secret dconf")
        (self.source / ".config/app/link").symlink_to("/etc/passwd")
        bundle = self.backup([".config"])
        self.assertEqual(set(bundle.files), {"home/.config/app/settings"})

    def test_user_gnome_extensions_are_discovered_and_backed_up(self):
        extension = self.put(self.source, ".local/share/gnome-shell/extensions/example@test/extension.js", b"extension code")
        found = {item["path"]: item for item in core.discover_configs(self.source)}
        root = ".local/share/gnome-shell/extensions"
        self.assertTrue(found[root]["selected"])
        bundle = self.backup([root])
        self.assertIn("home/" + str(extension.relative_to(self.source)), bundle.files)

    def test_destination_symlink_is_rejected_before_any_changes(self):
        self.put(self.source, "Documents/report", b"new")
        elsewhere = self.base / "outside"
        elsewhere.mkdir()
        (self.target / "Documents").symlink_to(elsewhere)
        bundle = self.backup(["Documents"])
        with self.assertRaises(core.TransferError):
            core.restore_bundle(bundle, self.target, ["Documents"], settings=False)
        self.assertFalse((elsewhere / "report").exists())

    def test_snap_revision_is_mapped_to_destination_revision(self):
        self.put(self.source, "snap/example/12/settings", b"prefs")
        (self.source / "snap/example/current").symlink_to("12")
        (self.target / "snap/example/37").mkdir(parents=True)
        (self.target / "snap/example/current").symlink_to("37")
        bundle = self.backup(["snap/example/current"])
        core.restore_bundle(bundle, self.target, ["snap/example/current"], settings=False)
        self.assertEqual((self.target / "snap/example/37/settings").read_bytes(), b"prefs")
        self.assertFalse((self.target / "snap/example/12").exists())
        core.undo_last(self.target)
        self.assertFalse((self.target / "snap/example/37/settings").exists())

    def test_escaping_snap_current_is_rejected(self):
        (self.target / "snap/example").mkdir(parents=True)
        (self.target / "snap/example/current").symlink_to("../../../outside")
        with self.assertRaises(core.TransferError):
            core.target_path(self.target, "snap/example/current/a")

    def test_fresh_snap_can_restore_before_first_launch(self):
        self.put(self.source, "snap/example/12/settings", b"prefs")
        (self.source / "snap/example/current").symlink_to("12")
        bundle = self.backup(["snap/example/current"])
        with patch.object(core, "list_snaps", return_value=[{"name": "example", "revision": "37"}]):
            core.restore_bundle(bundle, self.target, ["snap/example/current"], settings=False)
        self.assertEqual((self.target / "snap/example/37/settings").read_bytes(), b"prefs")
        self.assertFalse((self.target / "snap/example/current").exists())

    def test_write_failure_rolls_back_prior_files(self):
        for name in ("a", "b"):
            self.put(self.source, "Documents/" + name, b"new")
            self.put(self.target, "Documents/" + name, b"old")
        bundle = self.backup(["Documents"])
        real_copy = core.atomic_copy
        count = 0
        def fail_second(*args, **kw):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("simulated disk failure")
            return real_copy(*args, **kw)
        with patch.object(core, "atomic_copy", side_effect=fail_second):
            with self.assertRaisesRegex(core.TransferError, "original settings recovered"):
                core.restore_bundle(bundle, self.target, ["Documents"], settings=False)
        for name in ("a", "b"):
            self.assertEqual((self.target / "Documents" / name).read_bytes(), b"old")

    def test_recovery_capture_failure_changes_nothing(self):
        self.put(self.source, "Documents/a", b"new")
        self.put(self.target, "Documents/a", b"old")
        bundle = self.backup(["Documents"])
        with patch.object(core.shutil, "copyfile", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                core.restore_bundle(bundle, self.target, ["Documents"], settings=False)
        self.assertEqual((self.target / "Documents/a").read_bytes(), b"old")

    def test_root_backup_links_and_traversal_rejected(self):
        for name in ("../escape", "/etc/passwd", "home/../escape", "home/.ssh/key"):
            with self.subTest(name=name):
                path = self.base / "malicious.ubackup"
                with tarfile.open(path, "w:gz") as archive:
                    info = tarfile.TarInfo(name)
                    info.size = 1
                    archive.addfile(info, io.BytesIO(b"x"))
                with self.assertRaises(core.TransferError):
                    core.Bundle(path)
        for type_ in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE):
            with tarfile.open(self.archive, "w:gz") as archive:
                info = tarfile.TarInfo("home/Documents/attack")
                info.type = type_
                info.linkname = "/etc/passwd"
                archive.addfile(info)
            with self.assertRaises(core.TransferError):
                core.Bundle(self.archive)

    def test_checksum_corruption_rejected(self):
        self.put(self.source, "Documents/a", b"original")
        bundle = self.backup(["Documents"])
        m = bundle.manifest
        bad = self.base / "corrupt.ubackup"
        with tarfile.open(bad, "w:gz") as archive:
            for name, data in (("home/Documents/a", b"tampered"), ("manifest.json", core.json_bytes(m))):
                info = tarfile.TarInfo(name)
                info.size, info.mode = len(data), 0o644
                archive.addfile(info, io.BytesIO(data))
        with self.assertRaisesRegex(core.TransferError, "mismatch"):
            core.Bundle(bad)

    def test_duplicate_archive_members_rejected(self):
        with tarfile.open(self.archive, "w:gz") as archive:
            for _ in range(2):
                info = tarfile.TarInfo("manifest.json")
                info.size = 2
                archive.addfile(info, io.BytesIO(b"{}"))
        with self.assertRaisesRegex(core.TransferError, "duplicate"):
            core.Bundle(self.archive)

    def test_simultaneous_restore_blocked(self):
        with core.restore_lock(self.target):
            with self.assertRaisesRegex(core.TransferError, "already running"):
                with core.restore_lock(self.target):
                    pass


class ApplicationTests(unittest.TestCase):
    def test_option_injection_is_rejected(self):
        for name in ("--allow-unauthenticated", "vim;reboot", "../bad.deb", "vim=bad", "$(id)"):
            with self.subTest(name=name), self.assertRaises(core.TransferError):
                apps.validate_inventory({"apt": [{"name": name}]})
        with self.assertRaises(core.TransferError):
            apps.validate_inventory({"snap": [{"name": "hello", "channel": "--dangerous"}]})

    def test_legitimate_names_and_channels(self):
        apps.validate_inventory({"apt": [{"name": "libstdc++6:amd64"}],
            "snap": [{"name": "firefox", "channel": "esr/stable", "classic": False}],
            "flatpak": [{"name": "org.gnome.TextEditor", "branch": "stable", "origin": "flathub", "scope": "user"}]})

    def test_snap_branches_are_not_truncated_by_inventory(self):
        sample = [{"name": "snap-store", "version": "1", "tracking-channel": "2/stable/ubuntu-26.04", "confinement": "strict", "type": "app"}]
        with patch.object(core, "list_snaps", return_value=sample), patch.object(core, "run", return_value=b""), patch.object(core.shutil, "which", return_value="/usr/bin/snap"):
            inventory = core.scan_inventory()
        self.assertEqual(inventory["snap"][0]["channel"], "2/stable/ubuntu-26.04")
        apps.validate_inventory(inventory)

    def test_local_snaps_do_not_prevent_file_restore_review(self):
        apps.validate_inventory({"snap": [{"name": "local-app", "channel": None, "classic": False}]})

    def test_lenovo_hardware_packages_are_not_selected_for_dell(self):
        names = ["vlc", "nvidia-driver-590", "linux-image-generic", "intel-microcode", "grub-efi-amd64", "oem-somedevice", "broadcom-sta-dkms"]
        with patch.object(apps, "apt_status", return_value={x: "available" for x in names}):
            plan = apps.make_plan({"apt": [{"name": x} for x in names]})
        self.assertEqual([x["app"]["name"] for x in plan if x["selected"]], ["vlc"])

    def test_existing_and_unavailable_apps_are_not_selected(self):
        with patch.object(apps, "apt_status", return_value={"vim": "installed", "unknown-app": "unavailable"}):
            plan = apps.make_plan({"apt": [{"name": "vim"}, {"name": "unknown-app"}]})
        self.assertFalse(any(x["selected"] for x in plan))

    def test_installation_uses_argv_and_no_removal(self):
        calls = []
        with patch.object(apps, "execute", side_effect=lambda args, log: calls.append(args)), patch.object(apps, "apt_status", return_value={"vlc": "installed"}):
            result = apps.install_apps([{"kind": "apt", "app": {"name": "vlc"}}])
        self.assertIn("--no-remove", calls[0])
        self.assertEqual(calls[0][-2:], ["--", "vlc"])
        self.assertEqual(result, [("vlc", "installed")])


class AutomaticBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.destination = Path(self.tmp.name) / "backup-drive"
        (self.home / "Documents").mkdir(parents=True)
        self.destination.mkdir()
        (self.home / "Documents/report.txt").write_text("weekly report")
        self.config = {
            "enabled": True, "frequency": "weekly", "weekday": "Sun",
            "hour": 2, "minute": 30, "retention_days": 30,
            "destination": str(self.destination),
            "roots": ["Documents/report.txt"], "personal_roots": ["Documents/report.txt"],
            "include_gnome": False, "include_apps": False,
        }

    def test_settings_round_trip_and_calendar(self):
        saved = automation.save_config(self.home, self.config)
        self.assertEqual(automation.load_config(self.home), saved)
        self.assertEqual(automation.schedule_text(saved), "Sun *-*-* 02:30:00")
        self.assertEqual(automation.schedule_text({**saved, "frequency": "daily"}), "*-*-* 02:30:00")
        self.assertEqual(stat.S_IMODE(automation.config_path(self.home).stat().st_mode), 0o600)

    def test_invalid_destination_retention_and_nested_destination_are_rejected(self):
        with self.assertRaises(core.TransferError):
            automation.validate_config({**self.config, "destination": "relative/folder"})
        with self.assertRaises(core.TransferError):
            automation.validate_config({**self.config, "retention_days": 0})
        nested = self.home / "Documents/Backups"
        nested.mkdir()
        with self.assertRaisesRegex(core.TransferError, "outside"):
            automation.check_destination(self.home, nested, ["Documents"])

    def test_timer_is_user_level_persistent_and_can_be_disabled(self):
        calls = []
        with patch.object(automation, "_systemctl", side_effect=lambda args: calls.append(args)):
            automation.configure_timer(self.home, self.config)
        timer = (self.home / ".config/systemd/user/ubuntu-backup-automatic.timer").read_text()
        service = (self.home / ".config/systemd/user/ubuntu-backup-automatic.service").read_text()
        self.assertIn("OnCalendar=Sun *-*-* 02:30:00", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("--run-scheduled-backup", service)
        self.assertEqual(calls[-1], ["enable", "--now", "ubuntu-backup-automatic.timer"])
        calls.clear()
        with patch.object(automation, "_systemctl", side_effect=lambda args: calls.append(args)):
            automation.configure_timer(self.home, {**self.config, "enabled": False})
        self.assertEqual(calls[-1], ["disable", "--now", "ubuntu-backup-automatic.timer"])

    def test_retention_deletes_only_expired_app_created_backups(self):
        old = self.destination / "ubuntu-backup-auto-laptop-20260801-020000.ubackup"
        recent = self.destination / "ubuntu-backup-auto-laptop-20260910-020000.ubackup"
        manual = self.destination / "ubuntu-backup-manual.ubackup"
        for path in (old, recent, manual):
            path.write_text("backup")
        os.utime(old, (1785559200, 1785559200))
        os.utime(recent, (1789012800, 1789012800))
        os.utime(manual, (1577836800, 1577836800))
        now = automation.dt.datetime(2026, 9, 17, tzinfo=automation.dt.timezone.utc)
        deleted = automation.prune(self.destination, 30, now=now)
        self.assertEqual(deleted, [old.name])
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(manual.exists())

    def test_background_run_writes_verified_backup_to_selected_folder(self):
        automation.save_config(self.home, self.config)
        result = automation.run_backup(self.home)
        archive = self.destination / result["filename"]
        self.assertTrue(archive.exists())
        bundle = core.Bundle(archive)
        self.addCleanup(bundle.close)
        self.assertIn("home/Documents/report.txt", bundle.files)
        self.assertEqual(automation.read_status(self.home)["state"], "success")

    def test_version_120_google_drive_schedule_is_removed(self):
        units = self.home / ".config/systemd/user"
        units.mkdir(parents=True)
        service = units / "ubuntu-backup-automatic.service"
        timer = units / "ubuntu-backup-automatic.timer"
        service.write_text("old")
        timer.write_text("old")
        core.atomic_write(automation.config_path(self.home), core.json_bytes({"remote_uri": "google-drive://old"}))
        calls = []
        with patch.object(automation, "_systemctl", side_effect=lambda args: calls.append(args)):
            with self.assertRaisesRegex(core.TransferError, "Google Drive schedule was removed"):
                automation.load_config(self.home)
        self.assertFalse(service.exists())
        self.assertFalse(timer.exists())
        self.assertFalse(automation.config_path(self.home).exists())
        self.assertEqual(calls[0], ["disable", "--now", "ubuntu-backup-automatic.timer"])


class UpdateTests(unittest.TestCase):
    def release(self):
        base = f"https://github.com/{updates.REPOSITORY}/releases/download/v1.3.0/"
        return {"tag_name": "v1.3.0", "assets": [
            {"name": "ubuntu-backup_1.3.0_all.deb", "browser_download_url": base + "ubuntu-backup_1.3.0_all.deb", "size": 3},
            {"name": "SHA256SUMS", "browser_download_url": base + "SHA256SUMS"}]}

    def test_new_release_is_detected(self):
        with patch.object(updates, "request", return_value=json.dumps(self.release()).encode()):
            self.assertEqual(updates.latest()["version"], "1.3.0")

    def test_cross_repository_update_is_rejected(self):
        release = self.release()
        release["assets"][0]["browser_download_url"] = "https://evil.example/update.deb"
        with patch.object(updates, "request", return_value=json.dumps(release).encode()):
            with self.assertRaises(core.TransferError):
                updates.latest()

    def test_corrupt_update_is_not_installed(self):
        data = b"bad"
        checksum = hashlib.sha256(b"good").hexdigest()
        update = {"version": "1.2.0", "filename": "ubuntu-backup_1.2.0_all.deb", "url": "unused", "checksums": "unused", "size": 3}
        with patch.object(updates, "request", side_effect=[(checksum + "  " + update["filename"]).encode(), data]), patch.object(updates, "execute") as execute:
            with self.assertRaisesRegex(core.TransferError, "checksum failed"):
                updates.install(update)
            execute.assert_not_called()

    def test_semantic_version_comparison(self):
        self.assertGreater(updates.version_tuple("1.10.0"), updates.version_tuple("1.9.0"))


if __name__ == "__main__":
    unittest.main()
