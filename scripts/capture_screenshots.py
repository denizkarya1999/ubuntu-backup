"""Capture real app screens using generated demonstration files, not user data.

Run with xvfb-run -a /usr/bin/python3 scripts/capture_screenshots.py.
"""
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk
from ubuntu_backup.ui import Application, Window
from ubuntu_backup import core


def settle():
    for _ in range(30):
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        time.sleep(0.01)


def capture(window, name):
    settle()
    image = Gdk.pixbuf_get_from_window(window.get_window(), 0, 0,
                                     window.get_allocated_width(), window.get_allocated_height())
    image.savev(str(OUTPUT / name), "png", [], [])


OUTPUT = ROOT / "docs/screenshots"
OUTPUT.mkdir(parents=True, exist_ok=True)
app = Application()
app.register(None)
window = Window(app, test_mode=True)
window.resize(1080, 920)
window.stack.set_transition_type(Gtk.StackTransitionType.NONE)
window.message = lambda *args, **kwargs: True

with tempfile.TemporaryDirectory(prefix="ubuntu-backup-demo-") as temporary:
    home = Path(temporary) / "example-user"
    home.mkdir()
    examples = {
        "Documents/Project notes.txt": "Demonstration document",
        "Documents/Recipes.txt": "Demonstration document",
        "Pictures/Travel/readme.txt": "Demonstration folder",
        ".config/Code/User/settings.json": '{"editor.fontSize": 14}',
        ".config/gtk-3.0/settings.ini": "[Settings]\ngtk-application-prefer-dark-theme=1",
    }
    for name, contents in examples.items():
        path = home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
    personal = ["Documents/Project notes.txt", "Documents/Recipes.txt", "Pictures/Travel"]
    configurations = [".config/Code", ".config/gtk-3.0"]
    archive = Path(temporary) / "example.ubackup"
    core.create_backup(archive, home, personal + configurations,
                       {"apt": [], "snap": [], "flatpak": []}, False,
                       personal_roots=personal)
    window.home = home
    window.loaded((core.Bundle(archive), []))
    for name in personal:
        window.backup_selection.model.append([True, name, "Personal folder" if name == "Pictures/Travel" else "Personal file"])
    for name in configurations:
        window.config_selection.model.append([True, name, "Application preferences"])
    window.inventory_label.set_text("Personal files and app configurations selected · Demonstration")
    window.status.set_text("Ready to back up your selected files and settings")
    window.stack.set_visible_child_name("Back up")
    window.backup_page.get_parent().get_parent().get_vadjustment().set_value(0)
    capture(window, "backup.png")
    notebook = next(widget for widget in window.backup_page.get_children() if isinstance(widget, Gtk.Notebook))
    notebook.set_current_page(1)
    capture(window, "configurations.png")
    window.stack.set_visible_child_name("Restore")
    window.backup_summary.set_text("Ubuntu 26.04.1 LTS · Example backup · 5 selected paths · Checksums verified")
    window.restore_gnome.set_sensitive(True)
    window.restore_gnome.set_active(True)
    window.status.set_text("Review your selection, then restore on the new computer")
    capture(window, "restore.png")
    window.stack.set_visible_child_name("Automatic")
    window.drive_status.set_text("Google Drive folder selected")
    window.drive_uri = "google-drive://example/folder"
    window.drive_label = "Computer Backups / Ubuntu Backup"
    window.drive_folder_label.set_text(window._drive_folder_text())
    window.automatic_enabled.set_active(True)
    window.automatic_status.set_text("Last backup: September 17, 2026 · 1 expired backup moved to trash")
    window.status.set_text("Automatic backup is enabled")
    capture(window, "automatic.png")
    window.stack.set_visible_child_name("Updates")
    window.status.set_text("Ready")
    capture(window, "updates.png")
    window.stack.set_visible_child_name("About Us")
    capture(window, "about.png")
    window.bundle.close()

window.destroy()
print(f"Saved six app screenshots to {OUTPUT}")
