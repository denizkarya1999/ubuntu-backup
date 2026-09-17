"""Exercise actual GTK widgets in an isolated virtual display."""
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib
from ubuntu_backup.ui import Application, Window, visible_personal_path, configuration_label
from ubuntu_backup import core, __version__

app = Application()
app.register(None)
window = Window(app, test_mode=True)
window.stack.set_transition_type(Gtk.StackTransitionType.NONE)
window.message = lambda *args, **kwargs: True
assert Gtk.Settings.get_default().get_property("gtk-application-prefer-dark-theme")
assert window.about_details["Version"] == __version__
assert window.about_details["Developer"] == "Deniz K. Acikbas (@denizkarya1999)"
assert window.about_details["Agent used"] == "OpenAI Codex"
assert window.about_details["Programming language"] == "Python"
assert not visible_personal_path(".secret")
assert not visible_personal_path("Documents/.hidden/private.txt")
assert visible_personal_path("Documents/report.v2.txt")
assert configuration_label(".config/example") == "example · App settings"
assert window.automatic_frequency.get_active_id() == "weekly"
assert window.automatic_retention.get_value_as_int() == 30
dialog = window.chooser("Select personal files", Gtk.FileChooserAction.OPEN)
assert not dialog.get_show_hidden()
dialog.destroy()
with tempfile.TemporaryDirectory(prefix="ubuntu-backup-ui-test-") as directory:
    base = Path(directory)
    source = base / "lenovo"
    source.mkdir()
    (source / "Documents").mkdir()
    (source / "Documents/report.txt").write_text("Personal document")
    (source / ".config/example").mkdir(parents=True)
    (source / ".config/example/preferences").write_text("Saved app preferences")
    archive = base / "sample.ubackup"
    core.create_backup(archive, source, ["Documents/report.txt"], {"apt": [], "snap": [], "flatpak": []}, False)
    window.loaded((core.Bundle(archive), []))
    assert window.restore_selection.selected() == ["Documents/report.txt"]
    window.restore_selection.set_all(False)
    assert window.restore_selection.selected() == []
    window.restore_selection.set_all(True)
    window.backup_selection.model.append([True, "Documents/report.txt", "Personal file"])
    window.config_selection.model.append([True, ".config/example", "User configuration / appearance"])
    window.home = source
    window.backup_gnome.set_active(False)
    window.backup_apps.set_active(False)
    class ChosenFile:
        def set_current_name(self, *_): pass
        def set_current_folder(self, *_): pass
        def run(self): return Gtk.ResponseType.OK
        def get_filename(self): return str(base / "combined.ubackup")
        def destroy(self): pass
    with patch.object(window, "chooser", return_value=ChosenFile()), patch.object(window, "work", side_effect=lambda title, worker, done: worker()):
        window.backup(None)
    combined = core.Bundle(base / "combined.ubackup")
    assert set(combined.manifest["roots"]) == {"Documents/report.txt", ".config/example"}
    assert combined.manifest["personal_roots"] == ["Documents/report.txt"]
    saved = {name: reader.read() for name, reader in combined.iter_files(["home/.config/example/preferences"])}
    assert saved["home/.config/example/preferences"] == b"Saved app preferences"
    combined.close()
    window.backup_gnome.set_active(True)
    window.backup_apps.set_active(True)
    window.inventory_label.set_text("24 APT · 3 Snap · 2 Flatpak · Example preview")
    window.drive_status.set_text("Google Drive folder selected")
    window.drive_uri = "google-drive://example/folder"
    window.drive_label = "Computer Backups / Ubuntu Backup"
    window.drive_folder_label.set_text(window._drive_folder_text())
    window.automatic_enabled.set_active(True)
    window.automatic_status.set_text("Last backup: 2026-09-17 · ubuntu-backup-auto-example.ubackup · 1 expired removed")
    for page in ("Back up", "Restore", "Automatic", "Updates", "About Us"):
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
print("PASS: dark GTK interface, automatic Drive settings, hidden-file browsing, About Us, and combined personal/configuration backup")
