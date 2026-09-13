"""Run only in a fresh dbus-run-session; never touches the desktop user's dconf."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ubuntu_backup import core


with tempfile.TemporaryDirectory(prefix="ubuntu-backup-dconf-test-") as directory:
    base = Path(directory)
    source, target = base / "lenovo", base / "dell"
    source.mkdir()
    target.mkdir()
    # dconf's session service inherits XDG_CONFIG_HOME from the session bus,
    # so launch a fresh bus with the isolated environment for this test worker.
    if "UBUNTU_BACKUP_ISOLATED_TEST" not in os.environ:
        env = {**os.environ, "XDG_CONFIG_HOME": str(base / "dconf"),
               "UBUNTU_BACKUP_ISOLATED_TEST": "1"}
        subprocess.run(["dbus-run-session", "--", sys.executable, __file__], env=env, check=True)
        sys.exit(0)
    core.run(["dconf", "write", "/org/gnome/desktop/interface/clock-show-seconds", "true"])
    archive = base / "gnome.ubackup"
    core.create_backup(archive, source, [], {"apt": [], "snap": [], "flatpak": []}, True)
    core.run(["dconf", "write", "/org/gnome/desktop/interface/clock-show-seconds", "false"])
    bundle = core.Bundle(archive)
    core.restore_bundle(bundle, target, [], settings=True)
    assert core.run(["dconf", "read", "/org/gnome/desktop/interface/clock-show-seconds"]).strip() == b"true"
    core.undo_last(target)
    assert core.run(["dconf", "read", "/org/gnome/desktop/interface/clock-show-seconds"]).strip() == b"false"
    bundle.close()
    print("PASS: GNOME preferences backup, restore and undo in isolated D-Bus/dconf session")
