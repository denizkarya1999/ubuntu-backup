"""Exercise actual GTK widgets in an isolated virtual display."""
import os
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib
from ubuntu_backup.ui import Application, Window
from ubuntu_backup import core

app = Application()
app.register(None)
window = Window(app, test_mode=True)
window.stack.set_transition_type(Gtk.StackTransitionType.NONE)
window.message = lambda *args, **kwargs: True
with tempfile.TemporaryDirectory(prefix="ubuntu-backup-ui-test-") as directory:
    base = Path(directory)
    source = base / "lenovo"
    source.mkdir()
    (source / "Documents").mkdir()
    (source / "Documents/report.txt").write_text("Personal document")
    archive = base / "sample.ubackup"
    core.create_backup(archive, source, ["Documents/report.txt"], {"apt": [], "snap": [], "flatpak": []}, False)
    window.loaded((core.Bundle(archive), []))
    assert window.restore_selection.selected() == ["Documents/report.txt"]
    window.restore_selection.set_all(False)
    assert window.restore_selection.selected() == []
    window.restore_selection.set_all(True)
    window.backup_selection.model.append([True, "Documents/report.txt", "Personal file"])
    window.inventory_label.set_text("24 APT · 3 Snap · 2 Flatpak · Example preview")
    for page in ("Back up", "Restore", "Updates"):
        window.stack.set_visible_child_name(page)
        for _ in range(20):
            while Gtk.events_pending():
                Gtk.main_iteration_do(False)
            time.sleep(0.01)
        assert window.stack.get_visible_child_name() == page
        if output := os.environ.get("UBUNTU_BACKUP_SCREENSHOTS"):
            Path(output).mkdir(parents=True, exist_ok=True)
            pixbuf = Gdk.pixbuf_get_from_window(window.get_window(), 0, 0, window.get_allocated_width(), window.get_allocated_height())
            pixbuf.savev(str(Path(output) / (page.lower().replace(" ", "-") + ".png")), "png", [], [])
    window.bundle.close()
window.destroy()
print("PASS: GTK window, three screens, archive review and selection controls")
