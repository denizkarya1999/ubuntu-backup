"""Native GTK interface; long-running work always stays off the UI thread."""
import datetime as dt
import json
import os
from pathlib import Path
import threading

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, Gio, GLib, Pango

from . import __version__
from . import apps, automation, core, updates

CSS = b"""
window, dialog { background: #202024; color: #f4f1f5; }
headerbar { min-height: 48px; background: #29292e; color: #f4f1f5; border-color: #414149; }
.hero { font-size: 28px; font-weight: 800; color: #faf7fb; }
.subtitle { color: #c5bdca; font-size: 14px; }
.section { font-size: 16px; font-weight: bold; }
.card { background: #2b2b31; border: 1px solid #47454e; border-radius: 10px; padding: 16px; }
.accent { background: #e95420; color: white; border-color: #cc4519; font-weight: bold; padding: 9px 18px; }
.accent:hover { background: #f66b36; }
.accent:disabled { background: #603c31; color: #b2a39e; border-color: #61473e; }
.quiet { color: #bcb4c2; }
.notice { background: #463c29; border-radius: 8px; padding: 12px; color: #f5d5a4; }
treeview, textview text { background: #26262b; color: #eeeaf0; }
treeview { padding: 5px; }
treeview:selected { background: #723c2a; color: #ffffff; }
notebook > header { background: #29292e; border-color: #45424b; }
notebook > stack { background: #222227; border-color: #45424b; }
viewport { background: transparent; }
button { background-image: none; background-color: #36353d; color: #f0edf2; border-color: #59535f; text-shadow: none; }
button:hover { background-color: #45424c; }
button:checked { background-color: #584238; color: #ffffff; }
button:disabled { color: #9b929f; }
entry { background: #29282f; color: #f0edf2; border-color: #59535f; }
button.link { background: transparent; border-color: transparent; box-shadow: none; }
button.link, button.link label { color: #ffb08a; }
progressbar progress { background-color: #e95420; }
"""


def visible_personal_path(relative):
    """Hidden entries are not browsed; selected folders still keep their contents."""
    return all(not part.startswith(".") for part in Path(relative).parts)


def configuration_label(relative):
    """Keep real paths in the model, but show readable configuration names."""
    groups = (
        (".config/", "App settings"),
        (".local/share/gnome-shell/extensions", "GNOME extensions"),
        (".local/share/", "Desktop data"),
        (".var/app/", "Flatpak"),
        ("snap/", "Snap"),
    )
    for prefix, group in groups:
        if relative == prefix:
            return group
        if relative.startswith(prefix):
            name = " / ".join(part.lstrip(".") for part in relative[len(prefix):].split("/") if part)
            return f"{name} · {group}" if name else group
    if not visible_personal_path(relative):
        return " / ".join(part.lstrip(".") for part in Path(relative).parts) + " · Saved settings"
    return relative


def label(text, style=None):
    widget = Gtk.Label(label=text, xalign=0)
    widget.set_line_wrap(True)
    widget.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
    widget.set_max_width_chars(85)
    if style:
        widget.get_style_context().add_class(style)
    return widget


def box(spacing=12, horizontal=False):
    return Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL if horizontal else Gtk.Orientation.VERTICAL, spacing=spacing)


def button(text, callback, accent=False):
    widget = Gtk.Button(label=text)
    widget.connect("clicked", callback)
    if accent:
        widget.get_style_context().add_class("accent")
    return widget


class Selection:
    def __init__(self, columns, readable_paths=False):
        self.model = Gtk.ListStore(bool, *([str] * len(columns)))
        self.view = Gtk.TreeView(model=self.model)
        self.view.set_headers_visible(True)
        renderer = Gtk.CellRendererToggle()
        renderer.connect("toggled", self.toggle)
        self.view.append_column(Gtk.TreeViewColumn("Use", renderer, active=0))
        for i, name in enumerate(columns, 1):
            renderer = Gtk.CellRendererText()
            renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
            column = Gtk.TreeViewColumn(name, renderer, text=i)
            if readable_paths and i == 1:
                column.set_cell_data_func(renderer, self.display_path)
            column.set_resizable(True)
            column.set_expand(True)
            column.set_min_width(110)
            self.view.append_column(column)
        self.scroll = Gtk.ScrolledWindow()
        self.scroll.set_shadow_type(Gtk.ShadowType.IN)
        self.scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.scroll.add(self.view)
        self.scroll.set_min_content_height(180)

    @staticmethod
    def display_path(column, renderer, model, iterator, data=None):
        renderer.set_property("text", configuration_label(model[iterator][1]))

    def toggle(self, _, path):
        self.model[path][0] = not self.model[path][0]

    def selected(self):
        return [row[1] for row in self.model if row[0]]

    def set_all(self, value):
        for row in self.model:
            row[0] = value


class Window(Gtk.ApplicationWindow):
    def __init__(self, application, *, test_mode=False):
        super().__init__(application=application, title="Ubuntu Backup")
        self.set_default_size(1000, 740)
        self.set_size_request(780, 650)
        self.set_icon_name("ubuntu-backup")
        self.home = Path.home()
        self.bundle = None
        self.plan = []
        self.inventory = {}
        self.busy = False
        self.log_lines = []
        self.pending_update = None
        self.page_actions = {}
        self.test_mode = test_mode
        self.connect("delete-event", self.on_close)
        # Scoped to this application: do not change the user's desktop theme.
        Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", True)
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        switcher = Gtk.StackSwitcher(stack=self.stack)
        header = Gtk.HeaderBar(title="Ubuntu Backup", subtitle="Your desktop. Your files. Your next computer.")
        header.set_show_close_button(True)
        header.set_custom_title(switcher)
        self.set_titlebar(header)
        main = box(0)
        self.add(main)
        main.pack_start(self.stack, True, True, 0)
        self.build_backup()
        self.build_restore()
        self.build_automatic()
        self.build_updates()
        self.build_about()
        footer = box(8)
        footer.set_border_width(16)
        self.status = label("Ready", "quiet")
        self.progress = Gtk.ProgressBar()
        footer.pack_start(self.status, False, False, 0)
        footer.pack_start(self.progress, False, False, 0)
        expander = Gtk.Expander(label="Activity and report")
        self.log_buffer = Gtk.TextBuffer()
        view = Gtk.TextView(buffer=self.log_buffer, editable=False, monospace=True)
        view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        scroll = Gtk.ScrolledWindow()
        scroll.set_min_content_height(120)
        scroll.add(view)
        report = box(6)
        report.pack_start(scroll, True, True, 0)
        report.pack_start(button("Save report…", self.save_report), False, False, 0)
        expander.add(report)
        footer.pack_start(expander, False, False, 0)
        main.pack_start(footer, False, False, 0)
        self.show_all()
        if not test_mode:
            self.scan()
            GLib.timeout_add_seconds(25, self.auto_update)
            GLib.timeout_add_seconds(86400, self.daily_update)

    def page(self, name, title, subtitle):
        page = box(16)
        page.set_border_width(28)
        page.pack_start(label(title, "hero"), False, False, 0)
        page.pack_start(label(subtitle, "subtitle"), False, False, 0)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.add(page)
        frame = box(0)
        frame.pack_start(scroll, True, True, 0)
        actions = box(0)
        frame.pack_start(actions, False, False, 0)
        self.page_actions[name] = actions
        self.stack.add_titled(frame, name, name)
        return page

    def add_action_row(self, name, row):
        row.set_margin_start(28)
        row.set_margin_end(28)
        row.set_margin_top(12)
        row.set_margin_bottom(12)
        self.page_actions[name].pack_start(row, False, False, 0)

    def build_backup(self):
        self.backup_page = self.page("Back up", "Make this computer portable",
            "Save your desktop preferences, apps, configurations, and the personal files you choose.")
        options = box(8)
        self.backup_gnome = Gtk.CheckButton(label="GNOME preferences, shortcuts, appearance, and extension state")
        self.backup_gnome.set_active(True)
        self.backup_apps = Gtk.CheckButton(label="Installed app list · APT, Snap, and Flatpak")
        self.backup_apps.set_active(True)
        options.pack_start(self.backup_gnome, False, False, 0)
        options.pack_start(self.backup_apps, False, False, 0)
        options.get_style_context().add_class("card")
        self.backup_page.pack_start(options, False, False, 0)
        notebook = Gtk.Notebook()
        personal_box = box(10)
        personal_box.set_border_width(10)
        toolbar = box(8, True)
        toolbar.pack_start(button("Add files…", lambda _: self.choose_personal(False)), False, False, 0)
        toolbar.pack_start(button("Add folder…", lambda _: self.choose_personal(True)), False, False, 0)
        toolbar.pack_start(button("Select all", lambda _: self.backup_selection.set_all(True)), False, False, 0)
        toolbar.pack_start(button("Clear selection", lambda _: self.backup_selection.set_all(False)), False, False, 0)
        personal_box.pack_start(toolbar, False, False, 0)
        self.backup_selection = Selection(["File or folder (inside your home)", "Contents"])
        personal_box.pack_start(self.backup_selection.scroll, True, True, 0)
        notebook.append_page(personal_box, Gtk.Label(label="Personal files"))
        config_box = box(10)
        config_box.set_border_width(10)
        config_box.pack_start(label("Choose the application settings and desktop customizations to save.", "quiet"), False, False, 0)
        config_toolbar = box(8, True)
        self.config_selection = Selection(["Configuration", "Contents"], readable_paths=True)
        config_toolbar.pack_start(button("Select all", lambda _: self.config_selection.set_all(True)), False, False, 0)
        config_toolbar.pack_start(button("Clear selection", lambda _: self.config_selection.set_all(False)), False, False, 0)
        config_toolbar.pack_end(button("Rescan", lambda _: self.scan()), False, False, 0)
        config_box.pack_start(config_toolbar, False, False, 0)
        config_box.pack_start(self.config_selection.scroll, True, True, 0)
        notebook.append_page(config_box, Gtk.Label(label="App configurations"))
        self.backup_page.pack_start(notebook, True, True, 0)
        self.backup_page.pack_start(label("Close apps before backing up their configurations. Backups are not encrypted; selected app profiles may include signed-in sessions. Keep the file private.", "quiet"), False, False, 0)
        self.inventory_label = label("Reading this computer…", "quiet")
        self.backup_page.pack_start(self.inventory_label, False, False, 0)
        row = box(8, True)
        row.pack_end(button("Create backup…", self.backup, True), False, False, 0)
        self.add_action_row("Back up", row)

    def build_restore(self):
        self.restore_page = self.page("Restore", "Feel at home on your new computer",
            "Open a backup, choose what to bring over, then restore. Existing matching files get a recovery copy.")
        row = box(8, True)
        row.pack_start(button("Open backup…", self.open_backup, True), False, False, 0)
        row.pack_end(button("Undo last restore", self.undo), False, False, 0)
        self.restore_page.pack_start(row, False, False, 0)
        self.backup_summary = label("Choose an .ubackup file from your old computer.", "quiet")
        self.restore_page.pack_start(self.backup_summary, False, False, 0)
        self.restore_gnome = Gtk.CheckButton(label="Restore saved GNOME preferences")
        self.restore_gnome.set_active(True)
        self.rewrite = Gtk.CheckButton(label="Adapt home-folder paths in text configurations to this user")
        self.rewrite.set_active(True)
        self.restore_page.pack_start(self.restore_gnome, False, False, 0)
        self.restore_page.pack_start(self.rewrite, False, False, 0)
        notebook = Gtk.Notebook()
        self.restore_selection = Selection(["File, folder, or configuration", "Backup contents"], readable_paths=True)
        file_box = box(8)
        file_box.set_border_width(10)
        tools = box(8, True)
        tools.pack_start(button("Select all", lambda _: self.restore_selection.set_all(True)), False, False, 0)
        tools.pack_start(button("Clear selection", lambda _: self.restore_selection.set_all(False)), False, False, 0)
        file_box.pack_start(tools, False, False, 0)
        file_box.pack_start(self.restore_selection.scroll, True, True, 0)
        notebook.append_page(file_box, Gtk.Label(label="Files and settings"))
        app_box = box(8)
        app_box.set_border_width(10)
        app_box.pack_start(label("Only checked apps will be installed. Downloads need internet. Software sources and custom Flatpak remotes must already be configured; Flathub can be added automatically.", "quiet"), False, False, 0)
        self.app_selection = Selection(["App", "Type", "Destination status"])
        app_box.pack_start(self.app_selection.scroll, True, True, 0)
        app_tools = box(8, True)
        app_tools.pack_start(button("Select available", self.select_available), False, False, 0)
        app_tools.pack_start(button("Clear selection", lambda _: self.app_selection.set_all(False)), False, False, 0)
        app_tools.pack_start(button("Recheck availability", self.recheck_apps), False, False, 0)
        app_box.pack_start(app_tools, False, False, 0)
        notebook.append_page(app_box, Gtk.Label(label="Apps to install"))
        self.restore_page.pack_start(notebook, True, True, 0)
        self.restore_page.pack_start(label("Use a backup you trust, and close the apps being restored. Personal files keep their exact contents. Sign out and back in after restoring GNOME settings.", "quiet"), False, False, 0)
        row = box(8, True)
        row.pack_end(button("Restore selected items…", self.restore, True), False, False, 0)
        self.add_action_row("Restore", row)

    def build_automatic(self):
        page = self.page("Automatic", "Back up automatically",
                         "Choose a folder, then run a daily or weekly backup in the background.")
        try:
            saved = automation.load_config(self.home) if not self.test_mode else {}
        except core.TransferError:
            saved = {}
        destination = box(10)
        destination.get_style_context().add_class("card")
        destination.pack_start(label("Backup destination", "section"), False, False, 0)
        destination.pack_start(label("Choose a local folder or a folder on a connected external drive. The folder must be available when the timer runs.", "quiet"), False, False, 0)
        destination.pack_start(button("Choose destination folder…", self.choose_backup_destination), False, False, 0)
        self.backup_destination = saved.get("destination", "")
        self.backup_destination_label = label(self.destination_text(), "quiet")
        destination.pack_start(self.backup_destination_label, False, False, 0)
        page.pack_start(destination, False, False, 0)

        settings = Gtk.Grid(column_spacing=24, row_spacing=14)
        settings.get_style_context().add_class("card")
        self.automatic_enabled = Gtk.CheckButton(label="Enable background backups")
        self.automatic_enabled.set_active(saved.get("enabled", False))
        settings.attach(self.automatic_enabled, 0, 0, 2, 1)
        settings.attach(label("Frequency", "quiet"), 0, 1, 1, 1)
        self.automatic_frequency = Gtk.ComboBoxText()
        self.automatic_frequency.append("daily", "Every day")
        self.automatic_frequency.append("weekly", "Once a week")
        self.automatic_frequency.set_active_id(saved.get("frequency", "weekly"))
        self.automatic_frequency.connect("changed", self.frequency_changed)
        settings.attach(self.automatic_frequency, 1, 1, 1, 1)
        self.weekday_caption = label("Day", "quiet")
        settings.attach(self.weekday_caption, 0, 2, 1, 1)
        self.automatic_weekday = Gtk.ComboBoxText()
        for value, text in zip(automation.WEEKDAYS, ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")):
            self.automatic_weekday.append(value, text)
        self.automatic_weekday.set_active_id(saved.get("weekday", "Sun"))
        settings.attach(self.automatic_weekday, 1, 2, 1, 1)
        settings.attach(label("Start time", "quiet"), 0, 3, 1, 1)
        time_row = box(8, True)
        self.automatic_hour = Gtk.SpinButton.new_with_range(0, 23, 1)
        self.automatic_hour.set_value(saved.get("hour", 2))
        self.automatic_minute = Gtk.SpinButton.new_with_range(0, 59, 1)
        self.automatic_minute.set_value(saved.get("minute", 0))
        time_row.pack_start(self.automatic_hour, False, False, 0)
        time_row.pack_start(Gtk.Label(label=":"), False, False, 0)
        time_row.pack_start(self.automatic_minute, False, False, 0)
        settings.attach(time_row, 1, 3, 1, 1)
        settings.attach(label("Delete backups after", "quiet"), 0, 4, 1, 1)
        retention_row = box(8, True)
        self.automatic_retention = Gtk.SpinButton.new_with_range(1, 3650, 1)
        self.automatic_retention.set_value(saved.get("retention_days", 30))
        retention_row.pack_start(self.automatic_retention, False, False, 0)
        retention_row.pack_start(Gtk.Label(label="days"), False, False, 0)
        settings.attach(retention_row, 1, 4, 1, 1)
        page.pack_start(settings, False, False, 0)
        self.frequency_changed()

        page.pack_start(label("Automatic backups use the items currently checked on Back up, including GNOME changes, user-installed extensions under App configurations, and the app list. Only app-created automatic backups are removed by retention.", "quiet"), False, False, 0)
        self.automatic_status = label("", "quiet")
        page.pack_start(self.automatic_status, False, False, 0)
        self.refresh_automatic_status()
        actions = box(8, True)
        actions.pack_end(button("Save schedule", self.save_automatic, True), False, False, 0)
        actions.pack_end(button("Save and back up now", lambda _: self.save_automatic(None, run_now=True)), False, False, 0)
        self.add_action_row("Automatic", actions)

    def build_updates(self):
        page = self.page("Updates", "Ubuntu Backup stays up to date",
                         "Updates come directly from the public GitHub releases for this app.")
        page.pack_start(label("Installed version " + __version__, "section"), False, False, 0)
        self.update_label = label("Automatic update checks run while the app is open.", "quiet")
        page.pack_start(self.update_label, False, False, 0)
        self.auto_install = Gtk.CheckButton(label="Automatically download and install new versions")
        preferences = {}
        if not self.test_mode:
            try:
                preferences = json.loads((core.state_dir(self.home) / "preferences.json").read_text())
            except (OSError, ValueError):
                pass
        self.auto_install.set_active(preferences.get("auto_install_updates", True))
        self.auto_install.connect("toggled", self.save_preferences)
        page.pack_start(self.auto_install, False, False, 0)
        page.pack_start(label("Ubuntu may ask for your password to install an update. Updates wait until a backup or restore finishes. Close and reopen the app after an update.", "quiet"), False, False, 0)
        row = box(8, True)
        row.pack_start(button("Check for updates", lambda _: self.check_updates()), False, False, 0)
        self.update_button = button("Install update", self.install_update, True)
        self.update_button.set_sensitive(False)
        row.pack_start(self.update_button, False, False, 0)
        page.pack_start(row, False, False, 0)
        page.pack_start(Gtk.LinkButton(uri=updates.RELEASES, label="View releases and source code on GitHub"), False, False, 0)
        page.pack_start(label("Made for Ubuntu Desktop", "section"), False, False, 12)
        page.pack_start(label("Personal files and selected user configurations are portable. Package availability, GNOME extensions, hardware settings, and third-party app compatibility depend on the destination Ubuntu release. System services, /etc configuration, SSH keys, keyrings, and app passwords are not migrated automatically.", "quiet"), False, False, 0)

    def build_about(self):
        page = self.page("About Us", "Ubuntu Backup",
                         "Back up on one Ubuntu computer. Restore on another.")
        self.about_details = {
            "App name": "Ubuntu Backup",
            "Version": __version__,
            "Developer": "Deniz K. Acikbas (@denizkarya1999)",
            "Agent used": "OpenAI Codex",
            "Programming language": "Python",
            "Interface and assets": "GTK 3 · CSS styling · SVG graphics",
            "License": "MIT · Open source",
        }
        grid = Gtk.Grid(column_spacing=32, row_spacing=20)
        grid.get_style_context().add_class("card")
        for row, (name, value) in enumerate(self.about_details.items()):
            caption = label(name, "quiet")
            caption.set_valign(Gtk.Align.START)
            detail = label(value)
            detail.set_hexpand(True)
            grid.attach(caption, 0, row, 1, 1)
            grid.attach(detail, 1, row, 1, 1)
        page.pack_start(grid, False, False, 0)
        page.pack_start(Gtk.LinkButton(uri=f"https://github.com/{updates.REPOSITORY}",
                                     label="Source code and project on GitHub"), False, False, 0)
        page.pack_start(label("Ubuntu Backup is an independent project and is not an official Canonical product.", "quiet"), False, False, 0)

    def destination_text(self):
        return "Selected folder: " + (self.backup_destination if self.backup_destination else "None")

    def frequency_changed(self, *_):
        weekly = self.automatic_frequency.get_active_id() == "weekly"
        self.weekday_caption.set_sensitive(weekly)
        self.automatic_weekday.set_sensitive(weekly)

    def refresh_automatic_status(self):
        status = automation.read_status(self.home) if not self.test_mode else {}
        if not status:
            self.automatic_status.set_text("No automatic backup has run yet.")
        elif status.get("state") == "success":
            self.automatic_status.set_text(f"Last backup: {status.get('completed', 'unknown time')} · {status.get('filename', '')} · {status.get('deleted', 0)} expired deleted")
        elif status.get("state") == "running":
            self.automatic_status.set_text("An automatic backup is running in the background.")
        else:
            self.automatic_status.set_text("Last automatic backup failed: " + status.get("message", "Unknown error"))

    def choose_backup_destination(self, _):
        dialog = self.chooser("Choose automatic backup destination", Gtk.FileChooserAction.SELECT_FOLDER)
        if self.backup_destination and Path(self.backup_destination).is_dir():
            dialog.set_current_folder(self.backup_destination)
        response = dialog.run()
        filename = dialog.get_filename()
        dialog.destroy()
        if response == Gtk.ResponseType.OK and filename:
            try:
                self.backup_destination = automation.normalize_destination(filename)
            except core.TransferError as e:
                self.message("Choose another destination", str(e))
            else:
                self.backup_destination_label.set_text(self.destination_text())

    def automatic_config(self):
        roots = list(dict.fromkeys(self.backup_selection.selected() + self.config_selection.selected()))
        personal = [row[1] for row in self.backup_selection.model if row[0] and row[2].startswith("Personal")]
        value = {
            "enabled": self.automatic_enabled.get_active(),
            "frequency": self.automatic_frequency.get_active_id(),
            "weekday": self.automatic_weekday.get_active_id(),
            "hour": self.automatic_hour.get_value_as_int(),
            "minute": self.automatic_minute.get_value_as_int(),
            "retention_days": self.automatic_retention.get_value_as_int(),
            "destination": self.backup_destination,
            "roots": roots,
            "personal_roots": personal,
            "include_gnome": self.backup_gnome.get_active(),
            "include_apps": self.backup_apps.get_active(),
        }
        if not roots and not value["include_gnome"] and not value["include_apps"]:
            raise core.TransferError("Choose something on Back up before saving the automatic schedule")
        if value["enabled"] and not self.backup_destination:
            raise core.TransferError("Choose a destination before enabling automatic backups")
        return automation.validate_config(value)

    def save_automatic(self, _, run_now=False):
        try:
            value = self.automatic_config()
            if run_now and not self.backup_destination:
                raise core.TransferError("Choose a destination before starting a backup")
            if value["enabled"] or run_now:
                automation.check_destination(self.home, value["destination"], value["roots"])
        except core.TransferError as e:
            self.message("Automatic backup is not ready", str(e))
            return
        def worker():
            automation.save_config(self.home, value)
            automation.configure_timer(self.home, value)
            return automation.run_backup(self.home, force=True, log=self.log) if run_now else None
        def done(result):
            self.refresh_automatic_status()
            if result:
                self.message("Automatic backup saved", f"{result['destination']}/{result['filename']}\n\nExpired automatic backups older than {value['retention_days']} days were deleted.")
            elif value["enabled"]:
                self.message("Automatic backup enabled", "Ubuntu will run it in the background at the selected time, even while this app is closed. Keep the destination connected and available.")
            else:
                self.message("Automatic backup disabled", "The saved settings remain available if you enable it again.")
        self.work("Creating automatic backup…" if run_now else "Saving automatic-backup schedule…", worker, done)

    def log(self, text):
        def append():
            self.log_lines.append(str(text))
            self.log_buffer.insert(self.log_buffer.get_end_iter(), str(text) + "\n")
            self.status.set_text(str(text)[:180])
            return False
        GLib.idle_add(append)

    def message(self, title, text, question=False):
        dialog = Gtk.MessageDialog(transient_for=self, modal=True,
            message_type=Gtk.MessageType.QUESTION if question else Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK_CANCEL if question else Gtk.ButtonsType.OK, text=title)
        dialog.format_secondary_text(text)
        response = dialog.run()
        dialog.destroy()
        return response == Gtk.ResponseType.OK

    def work(self, title, worker, done=None):
        if self.busy:
            return
        self.busy = True
        self.stack.set_sensitive(False)
        self.status.set_text(title)
        def pulse():
            if not self.busy:
                self.progress.set_fraction(0)
                return False
            self.progress.pulse()
            return True
        GLib.timeout_add(120, pulse)
        def finish(result=None, error=None):
            self.busy = False
            self.stack.set_sensitive(True)
            if error:
                self.log(str(error))
                self.message("Operation could not finish", str(error))
            elif done:
                done(result)
            return False
        def perform():
            try:
                result = worker()
            except Exception as e:
                GLib.idle_add(finish, None, e)
            else:
                GLib.idle_add(finish, result, None)
        threading.Thread(target=perform, daemon=True).start()

    def scan(self):
        def worker():
            return core.discover_configs(self.home), core.scan_inventory(self.log)
        def done(result):
            configs, self.inventory = result
            existing = {row[1] for row in self.config_selection.model}
            for item in configs:
                if item["path"] not in existing:
                    self.config_selection.model.append([item["selected"], item["path"],
                        "App profile · may contain private data" if item["sensitive"] else "User configuration / appearance"])
            counts = " · ".join(f"{len(self.inventory[x])} {x.upper() if x == 'apt' else x.title()}" for x in ("apt", "snap", "flatpak"))
            self.inventory_label.set_text(counts + (" · Some inventory checks failed; see activity" if self.inventory["warnings"] else ""))
            self.status.set_text("Choose the files you want to back up")
        self.work("Reading this computer…", worker, done)

    def chooser(self, title, action, pattern=None, multiple=False):
        dialog = Gtk.FileChooserDialog(title=title, transient_for=self, action=action)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Select", Gtk.ResponseType.OK)
        dialog.set_select_multiple(multiple)
        dialog.set_show_hidden(False)
        if pattern:
            filter_ = Gtk.FileFilter()
            filter_.set_name("Ubuntu Backup (*.ubackup)")
            filter_.add_pattern(pattern)
            dialog.add_filter(filter_)
        return dialog

    def choose_personal(self, folder):
        action = Gtk.FileChooserAction.SELECT_FOLDER if folder else Gtk.FileChooserAction.OPEN
        dialog = self.chooser("Select personal folders" if folder else "Select personal files", action, multiple=True)
        dialog.set_current_folder(str(self.home))
        if dialog.run() == Gtk.ResponseType.OK:
            selected = dialog.get_filenames()
        else:
            selected = []
        dialog.destroy()
        existing = {row[1] for row in self.backup_selection.model}
        for filename in selected:
            try:
                relative = str(Path(filename).absolute().relative_to(self.home.resolve()))
                if not visible_personal_path(relative):
                    self.message("Hidden files are not listed", "Use App configurations to choose saved application settings.")
                    continue
                core.target_path(self.home, relative)
                if relative not in existing:
                    self.backup_selection.model.append([True, relative, "Personal folder" if folder else "Personal file"])
                    existing.add(relative)
            except (ValueError, core.TransferError) as e:
                self.message("Choose a file inside your home folder", str(e))

    def backup(self, _):
        roots = list(dict.fromkeys(self.backup_selection.selected() + self.config_selection.selected()))
        personal = [row[1] for row in self.backup_selection.model if row[0] and row[2].startswith("Personal")]
        gnome, include_apps = self.backup_gnome.get_active(), self.backup_apps.get_active()
        if not roots and not gnome and not include_apps:
            self.message("Choose something to back up", "Select files, GNOME preferences, or the app list.")
            return
        dialog = self.chooser("Save backup", Gtk.FileChooserAction.SAVE)
        dialog.set_current_name("ubuntu-backup-" + dt.datetime.now().strftime("%Y-%m-%d-%H%M") + ".ubackup")
        dialog.set_current_folder(str(self.home))
        response = dialog.run()
        filename = dialog.get_filename()
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return
        if not filename.endswith(".ubackup"):
            filename += ".ubackup"
        def worker():
            inventory = core.scan_inventory(self.log) if include_apps else {"apt": [], "snap": [], "flatpak": []}
            return core.create_backup(filename, self.home, roots, inventory, gnome, self.log, personal_roots=personal)
        def done(manifest):
            warning = "\nSome app inventories could not be read. Check the activity report before transferring." if manifest["inventory"].get("warnings") else ""
            self.message("Backup saved", f"{filename}\n\nCopy this file to the new computer. Install Ubuntu Backup there and use Restore → Open backup.{warning}")
        self.work("Creating your backup…", worker, done)

    def open_backup(self, _):
        dialog = self.chooser("Open backup from your old computer", Gtk.FileChooserAction.OPEN, "*.ubackup")
        response = dialog.run()
        filename = dialog.get_filename()
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return
        def worker():
            bundle = core.Bundle(filename, self.log)
            try:
                plan = apps.make_plan(bundle.manifest["inventory"], self.log)
            except Exception:
                bundle.close()
                raise
            return bundle, plan
        self.work("Verifying backup and checking apps…", worker, self.loaded)

    def loaded(self, result):
        if self.bundle:
            self.bundle.close()
        self.bundle, self.plan = result
        m = self.bundle.manifest
        self.restore_selection.model.clear()
        counts = dict.fromkeys(m["roots"], 0)
        for name in self.bundle.files:
            if not name.startswith("home/"):
                continue
            relative = name[5:]
            while relative:
                if relative in counts:
                    counts[relative] += 1
                relative, separator, _ = relative.rpartition("/")
                if not separator:
                    break
        for root in m["roots"]:
            count = counts[root]
            self.restore_selection.model.append([True, root, f"{count:,} file" + ("s" if count != 1 else "")])
        self.populate_apps()
        self.restore_gnome.set_sensitive(bool(m["settings"]))
        self.restore_gnome.set_active(bool(m["settings"]))
        self.backup_summary.set_text(f"{m['system'].get('os', 'Linux')} · {m['created']} · {len(m['roots'])} selected paths · Checksums verified")
        source = m["system"]
        current = core.system_info()
        if (source.get("release"), source.get("architecture")) != (current.get("release"), current.get("architecture")):
            self.message("Different Ubuntu release or processor", "Personal files can still be restored. Review apps and extensions carefully; some may need a version compatible with this computer.")
        self.status.set_text("Review the selection, then restore")

    def populate_apps(self):
        self.app_selection.model.clear()
        for row in self.plan:
            self.app_selection.model.append([row["selected"], row["app"]["name"], row["kind"].upper(), row["detail"]])

    def select_available(self, _):
        for row, item in zip(self.app_selection.model, self.plan):
            row[0] = item["selected"]

    def recheck_apps(self, _):
        if self.bundle:
            def done(plan):
                self.plan = plan
                self.populate_apps()
            self.work("Checking app availability…", lambda: apps.make_plan(self.bundle.manifest["inventory"], self.log), done)

    def restore(self, _):
        if not self.bundle:
            self.message("Open a backup first", "Choose the backup made on your old computer.")
            return
        roots = self.restore_selection.selected()
        selected = [item for row, item in zip(self.app_selection.model, self.plan) if row[0]]
        settings, rewrite = self.restore_gnome.get_active(), self.rewrite.get_active()
        if not roots and not selected and not settings:
            self.message("Nothing selected", "Select at least one file, app, or the GNOME preferences.")
            return
        if not self.message("Restore selected items?",
            f"Restore {len(roots)} selected files/folders and install {len(selected)} apps?\n"
            f"GNOME preferences: {'yes' if settings else 'no'}.\n\n"
            "Matching existing files will be replaced. A recovery copy will be saved first. "
            "Only continue with a backup you trust. Close affected apps. Ubuntu may ask for your password for app installation.", True):
            return
        def worker():
            results = apps.install_apps(selected, self.log)
            recovery = core.restore_bundle(self.bundle, self.home, roots, settings, rewrite, self.log)
            skipped = [x["app"]["name"] for x in self.plan if x["status"] == "unavailable"]
            failed = [name for name, status in results if status != "installed"]
            for name, status in results:
                self.log(name + ": " + status)
            for name in skipped:
                self.log(name + ": unavailable from current software sources")
            return recovery, failed, skipped
        def done(result):
            recovery, failed, skipped = result
            text = "Selected files and preferences were restored."
            if failed or skipped:
                text += f"\n{len(failed)} app installations failed; {len(skipped)} apps were unavailable. See the activity report."
            if recovery:
                text += "\nUse Undo last restore to recover overwritten files and preferences."
            if settings:
                text += "\nSign out and back in to finish applying GNOME settings."
            self.message("Restore finished" if not failed else "Restore finished with app issues", text)
        self.work("Restoring your computer…", worker, done)

    def undo(self, _):
        if self.message("Undo the last restore?", "This restores the previous files and GNOME preferences and removes files created by that restore. Changes you made to those files afterward will be replaced. Installed apps will stay installed.", True):
            self.work("Recovering previous settings…", lambda: core.undo_last(self.home, self.log),
                      lambda _: self.message("Previous files and settings recovered", "Sign out and back in if GNOME preferences changed."))

    def save_report(self, _):
        dialog = self.chooser("Save activity report", Gtk.FileChooserAction.SAVE)
        dialog.set_current_name("ubuntu-backup-report.txt")
        response = dialog.run()
        filename = dialog.get_filename()
        dialog.destroy()
        if response == Gtk.ResponseType.OK:
            try:
                core.atomic_write(filename, ("\n".join(self.log_lines) + "\n").encode())
            except OSError as e:
                self.message("Could not save report", str(e))

    def save_preferences(self, _):
        if not self.test_mode:
            core.atomic_write(core.state_dir(self.home) / "preferences.json",
                core.json_bytes({"auto_install_updates": self.auto_install.get_active()}))

    def auto_update(self):
        if self.busy:
            return True
        self.check_updates(automatic=True)
        return False

    def daily_update(self):
        GLib.timeout_add_seconds(5, self.auto_update)
        return True

    def check_updates(self, automatic=False):
        if self.busy:
            return
        def worker():
            try:
                return updates.latest(), None
            except Exception as e:
                return None, str(e)
        def done(result):
            self.pending_update, error = result
            if error:
                self.update_label.set_text(error)
            elif self.pending_update:
                self.update_label.set_text("Version " + self.pending_update["version"] + " is available")
                self.update_button.set_sensitive(True)
                if automatic and self.auto_install.get_active():
                    self.install_update(None)
            else:
                self.update_label.set_text("You are using the latest release")
            self.status.set_text("Ready")
        self.work("Checking for an app update…", worker, done)

    def install_update(self, _):
        if self.pending_update:
            def done(_):
                self.update_button.set_sensitive(False)
                self.pending_update = None
                self.update_label.set_text("Update installed. Close and reopen Ubuntu Backup.")
                self.message("Ubuntu Backup updated", "Close and reopen the app to use the new version.")
            self.work("Updating Ubuntu Backup…", lambda: updates.install(self.pending_update, self.log), done)

    def on_close(self, *_):
        if self.busy:
            self.message("An operation is still running", "Wait for it to finish before closing Ubuntu Backup.")
            return True
        if self.bundle:
            self.bundle.close()
        return False


class Application(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="io.github.denizkarya1999.UbuntuBackup", flags=Gio.ApplicationFlags.FLAGS_NONE)

    def do_activate(self):
        window = self.get_active_window()
        if not window:
            window = Window(self)
        window.present()
