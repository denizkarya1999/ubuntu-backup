"""Scheduled Google Drive backups powered by GNOME and systemd user timers."""
import contextlib
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import socket
import subprocess

import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

from . import core

AUTOMATIC_NAME = re.compile(r"^ubuntu-backup-auto-[A-Za-z0-9._-]+-[0-9]{8}-[0-9]{6}\.ubackup$")
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def settings_dir(home):
    """Create the private app settings folder without following a symlink."""
    home = Path(home).resolve()
    path = home
    for part in (".config", "ubuntu-backup"):
        path /= part
        if path.is_symlink():
            raise core.TransferError("Automatic-backup settings folder must not be a symbolic link")
        path.mkdir(mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def config_path(home):
    return settings_dir(home) / "automatic-backup.json"


def normalize_drive_uri(value):
    if not isinstance(value, str) or not value.startswith("google-drive://"):
        raise core.TransferError("Choose a Google Drive folder")
    if "\x00" in value or len(value) > 4096:
        raise core.TransferError("Invalid Google Drive folder")
    return value


def validate_config(value):
    if not isinstance(value, dict):
        raise core.TransferError("Automatic-backup settings are invalid")
    roots = value.get("roots")
    personal = value.get("personal_roots", [])
    if not isinstance(roots, list) or not all(isinstance(x, str) for x in roots):
        raise core.TransferError("Automatic-backup file selection is invalid")
    if not isinstance(personal, list) or not set(personal) <= set(roots):
        raise core.TransferError("Automatic-backup personal-file selection is invalid")
    for relative in roots:
        core.safe_relative(relative)
    frequency = value.get("frequency")
    weekday = value.get("weekday", "Mon")
    if frequency not in ("daily", "weekly") or weekday not in WEEKDAYS:
        raise core.TransferError("Automatic-backup frequency is invalid")
    hour, minute = value.get("hour"), value.get("minute")
    retention = value.get("retention_days")
    if not isinstance(hour, int) or not 0 <= hour <= 23 or not isinstance(minute, int) or not 0 <= minute <= 59:
        raise core.TransferError("Automatic-backup time is invalid")
    if not isinstance(retention, int) or not 1 <= retention <= 3650:
        raise core.TransferError("Retention must be between 1 and 3,650 days")
    for name in ("enabled", "include_gnome", "include_apps"):
        if not isinstance(value.get(name), bool):
            raise core.TransferError("Automatic-backup settings are invalid")
    value = dict(value)
    value["roots"] = list(dict.fromkeys(roots))
    value["personal_roots"] = list(dict.fromkeys(personal))
    uri = value.get("remote_uri", "")
    value["remote_uri"] = normalize_drive_uri(uri) if uri else ""
    if value["enabled"] and not value["remote_uri"]:
        raise core.TransferError("Choose a Google Drive folder before enabling automatic backups")
    label = value.get("remote_label", "Google Drive folder")
    value["remote_label"] = label if isinstance(label, str) and 0 < len(label) <= 500 else "Google Drive folder"
    return value


def load_config(home):
    try:
        value = json.loads(config_path(home).read_text())
    except FileNotFoundError as e:
        raise core.TransferError("Set up automatic backup in Ubuntu Backup first") from e
    except (OSError, ValueError) as e:
        raise core.TransferError(f"Could not read automatic-backup settings: {e}") from e
    return validate_config(value)


def save_config(home, value):
    value = validate_config(value)
    core.atomic_write(config_path(home), core.json_bytes(value))
    return value


def open_online_accounts():
    """Open Ubuntu Settings without blocking the app."""
    try:
        subprocess.Popen(["gnome-control-center", "online-accounts"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        raise core.TransferError(f"Could not open Ubuntu Online Accounts: {e}") from e


def drive_folder_label(uri):
    uri = normalize_drive_uri(uri)
    try:
        info = Gio.File.new_for_uri(uri).query_info("standard::display-name", Gio.FileQueryInfoFlags.NONE, None)
        return info.get_display_name() or "Google Drive folder"
    except GLib.Error as e:
        raise core.TransferError(f"Could not open the selected Google Drive folder: {e.message}") from e


def upload(home, source, uri, log=lambda _: None):
    uri = normalize_drive_uri(uri)
    name = Path(source).name
    destination = Gio.File.new_for_uri(uri).get_child(name)
    log(f"Uploading {Path(source).name} to Google Drive…")
    reported = 0
    def progress(current, total, _):
        nonlocal reported
        if current - reported >= 256 * 1024 ** 2:
            reported = current
            log(f"Google Drive upload: {current / 1024 ** 3:.1f} / {total / 1024 ** 3:.1f} GiB")
    try:
        Gio.File.new_for_path(str(source)).copy(destination, Gio.FileCopyFlags.NONE, None, progress, None)
    except GLib.Error as e:
        raise core.TransferError(f"Google Drive upload failed: {e.message}") from e
    log("Google Drive upload finished")
    return destination.get_uri()


def list_drive_files(uri):
    uri = normalize_drive_uri(uri)
    try:
        folder = Gio.File.new_for_uri(uri)
        enumerator = folder.enumerate_children("standard::display-name,standard::type,time::modified",
                                              Gio.FileQueryInfoFlags.NONE, None)
        result = []
        while info := enumerator.next_file(None):
            if info.get_file_type() == Gio.FileType.REGULAR:
                modified = dt.datetime.fromtimestamp(info.get_attribute_uint64("time::modified"), dt.timezone.utc)
                result.append({"name": info.get_display_name(), "modified": modified,
                               "file": enumerator.get_child(info)})
        enumerator.close(None)
        return result
    except GLib.Error as e:
        raise core.TransferError(f"Could not read the selected Google Drive folder: {e.message}") from e


def trash_drive_file(file):
    try:
        file.trash(None)
    except GLib.Error as e:
        raise core.TransferError(f"Could not move an expired backup to Google Drive trash: {e.message}") from e


def prune(home, uri, retention_days, now=None, log=lambda _: None):
    """Move expired app-created automatic backups to Google Drive trash."""
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=retention_days)
    deleted = []
    for item in list_drive_files(uri):
        name, modified = item["name"], item["modified"]
        if AUTOMATIC_NAME.fullmatch(name) and modified < cutoff:
            trash_drive_file(item["file"])
            deleted.append(name)
            log(f"Moved expired backup to Google Drive trash: {name}")
    return deleted


def schedule_text(value):
    value = validate_config(value)
    prefix = "*-*-*" if value["frequency"] == "daily" else value["weekday"] + " *-*-*"
    return f"{prefix} {value['hour']:02d}:{value['minute']:02d}:00"


def _systemctl(args):
    try:
        result = subprocess.run(["systemctl", "--user", *args], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=30,
                                env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.TimeoutExpired) as e:
        raise core.TransferError(f"Could not configure Ubuntu's background scheduler: {e}") from e
    if result.returncode:
        message = result.stderr.decode(errors="replace").strip()
        raise core.TransferError(message or "Could not configure Ubuntu's background scheduler")


def configure_timer(home, value):
    value = validate_config(value)
    units = Path(home).resolve() / ".config/systemd/user"
    units.mkdir(parents=True, exist_ok=True)
    service = units / "ubuntu-backup-automatic.service"
    timer = units / "ubuntu-backup-automatic.timer"
    core.atomic_write(service, b"""[Unit]\nDescription=Ubuntu Backup automatic Google Drive backup\nAfter=network-online.target\n\n[Service]\nType=oneshot\nExecStart=/usr/bin/ubuntu-backup --run-scheduled-backup\nNice=10\nIOSchedulingClass=idle\nNoNewPrivileges=true\nPrivateTmp=true\n""", 0o644)
    timer_data = f"""[Unit]
Description=Run Ubuntu Backup automatically

[Timer]
OnCalendar={schedule_text(value)}
Persistent=true
AccuracySec=1min
Unit=ubuntu-backup-automatic.service

[Install]
WantedBy=timers.target
""".encode()
    core.atomic_write(timer, timer_data, 0o644)
    _systemctl(["daemon-reload"])
    if value["enabled"]:
        _systemctl(["enable", "--now", "ubuntu-backup-automatic.timer"])
    else:
        _systemctl(["disable", "--now", "ubuntu-backup-automatic.timer"])


def status_path(home):
    return core.state_dir(home) / "automatic-status.json"


def read_status(home):
    try:
        return json.loads(status_path(home).read_text())
    except (OSError, ValueError):
        return {}


def _write_status(home, **values):
    core.atomic_write(status_path(home), core.json_bytes(values))


def run_backup(home=None, *, force=False, log=print):
    home = Path(home or Path.home()).resolve()
    config = load_config(home)
    if not config["enabled"] and not force:
        log("Automatic backup is disabled")
        return None
    state = core.state_dir(home)
    lock_path = state / "automatic.lock"
    lock_path.touch(mode=0o600, exist_ok=True)
    with open(lock_path, "r+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise core.TransferError("An automatic backup is already running") from e
        started = core.now()
        _write_status(home, state="running", started=started, message="Creating backup")
        staging = core.private_dir(state / "automatic-staging")
        host = re.sub(r"[^A-Za-z0-9._-]+", "-", socket.gethostname()).strip("-") or "computer"
        filename = f"ubuntu-backup-auto-{host}-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.ubackup"
        local = staging / filename
        try:
            inventory = core.scan_inventory(log) if config["include_apps"] else {"apt": [], "snap": [], "flatpak": [], "warnings": []}
            core.create_backup(local, home, config["roots"], inventory, config["include_gnome"],
                               log, personal_roots=config["personal_roots"])
            upload(home, local, config["remote_uri"], log)
            deleted = prune(home, config["remote_uri"], config["retention_days"], log=log)
            completed = core.now()
            _write_status(home, state="success", started=started, completed=completed,
                          filename=filename, deleted=len(deleted), message="Backup uploaded to Google Drive")
            return {"filename": filename, "deleted": deleted, "completed": completed}
        except Exception as e:
            _write_status(home, state="failed", started=started, completed=core.now(), message=str(e))
            raise
        finally:
            with contextlib.suppress(OSError):
                local.unlink()
