"""Scheduled backups to a user-selected local or mounted folder."""
import contextlib
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import uuid

from . import core

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def settings_dir(home):
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


def normalize_destination(value):
    if not isinstance(value, str) or not value or "\x00" in value or len(value) > 4096:
        raise core.TransferError("Choose a backup destination folder")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise core.TransferError("Backup destination must be an absolute folder path")
    return str(path)


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
    destination = value.get("destination", "")
    value["destination"] = normalize_destination(destination) if destination else ""
    if value["enabled"] and not value["destination"]:
        raise core.TransferError("Choose a destination before enabling automatic backups")
    return value


def _read_raw_config(home):
    try:
        return json.loads(config_path(home).read_text())
    except FileNotFoundError as e:
        raise core.TransferError("Set up automatic backup in Ubuntu Backup first") from e
    except (OSError, ValueError) as e:
        raise core.TransferError(f"Could not read automatic-backup settings: {e}") from e


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


def remove_legacy_drive_schedule(home):
    """Remove the Google Drive schedule format shipped only in version 1.2.0."""
    home = Path(home).resolve()
    try:
        value = json.loads(config_path(home).read_text())
    except (OSError, ValueError):
        return False
    if not isinstance(value, dict) or "remote_uri" not in value:
        return False
    with contextlib.suppress(core.TransferError):
        _systemctl(["disable", "--now", "ubuntu-backup-automatic.timer"])
    unit_dir = home / ".config/systemd/user"
    for path in (unit_dir / "ubuntu-backup-automatic.service",
                 unit_dir / "ubuntu-backup-automatic.timer", config_path(home)):
        with contextlib.suppress(OSError):
            path.unlink()
    with contextlib.suppress(core.TransferError):
        _systemctl(["daemon-reload"])
    return True


def load_config(home):
    value = _read_raw_config(home)
    if isinstance(value, dict) and "remote_uri" in value:
        remove_legacy_drive_schedule(home)
        raise core.TransferError("The old Google Drive schedule was removed. Choose a local backup folder.")
    return validate_config(value)


def save_config(home, value):
    value = validate_config(value)
    core.atomic_write(config_path(home), core.json_bytes(value))
    return value


def schedule_text(value):
    value = validate_config(value)
    prefix = "*-*-*" if value["frequency"] == "daily" else value["weekday"] + " *-*-*"
    return f"{prefix} {value['hour']:02d}:{value['minute']:02d}:00"


def configure_timer(home, value):
    value = validate_config(value)
    units = Path(home).resolve() / ".config/systemd/user"
    units.mkdir(parents=True, exist_ok=True)
    service = units / "ubuntu-backup-automatic.service"
    timer = units / "ubuntu-backup-automatic.timer"
    core.atomic_write(service, b"""[Unit]\nDescription=Ubuntu Backup automatic local backup\n\n[Service]\nType=oneshot\nExecStart=/usr/bin/ubuntu-backup --run-scheduled-backup\nNice=10\nIOSchedulingClass=idle\nNoNewPrivileges=true\nPrivateTmp=true\n""", 0o644)
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


def check_destination(home, destination, roots):
    destination = Path(normalize_destination(os.fspath(destination)))
    if not destination.exists() or not destination.is_dir():
        raise core.TransferError("The selected backup destination is unavailable. Connect or mount it, then retry.")
    if destination.is_symlink():
        raise core.TransferError("The backup destination must not be a symbolic link")
    resolved = destination.resolve()
    home = Path(home).resolve()
    for relative in roots:
        source = core.target_path(home, relative).resolve()
        if resolved == source or source in resolved.parents:
            raise core.TransferError("Choose a destination outside the folders being backed up")
    if not os.access(resolved, os.W_OK | os.X_OK):
        raise core.TransferError("The selected backup destination is not writable")
    return resolved


def backup_owner(home):
    """Persistent per-user identity; hostnames need not be unique or stable."""
    path = core.state_dir(home) / "automatic-owner"
    try:
        owner = path.read_text().strip()
    except FileNotFoundError:
        owner = uuid.uuid4().hex
        core.atomic_write(path, owner.encode())
    if not re.fullmatch(r"[a-f0-9]{32}", owner):
        raise core.TransferError("Invalid automatic-backup owner identity")
    return owner


def prune(destination, retention_days, *, owner, now=None, log=lambda _: None):
    if not re.fullmatch(r"[a-f0-9]{32}", owner):
        raise core.TransferError("Invalid automatic-backup owner identity")
    owned_name = re.compile(r"^ubuntu-backup-auto-[A-Za-z0-9._-]+-" + owner +
                            r"-[0-9]{8}-[0-9]{6}\.ubackup$")
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now.timestamp() - retention_days * 24 * 60 * 60
    deleted = []
    for path in Path(destination).iterdir():
        try:
            entry = path.lstat()
        except OSError:
            continue
        if (owned_name.fullmatch(path.name) and stat.S_ISREG(entry.st_mode)
                and not stat.S_ISLNK(entry.st_mode) and entry.st_mtime < cutoff):
            path.unlink()
            deleted.append(path.name)
            log(f"Deleted expired automatic backup: {path.name}")
    return deleted


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
        try:
            destination = check_destination(home, config["destination"], config["roots"])
            host = re.sub(r"[^A-Za-z0-9._-]+", "-", socket.gethostname()).strip("-") or "computer"
            owner = backup_owner(home)
            filename = f"ubuntu-backup-auto-{host}-{owner}-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.ubackup"
            output = destination / filename
            inventory = core.scan_inventory(log) if config["include_apps"] else {"apt": [], "snap": [], "flatpak": [], "warnings": []}
            core.create_backup(output, home, config["roots"], inventory, config["include_gnome"],
                               log, personal_roots=config["personal_roots"])
            deleted = prune(destination, config["retention_days"], owner=owner, log=log)
            completed = core.now()
            _write_status(home, state="success", started=started, completed=completed,
                          filename=filename, destination=str(destination), deleted=len(deleted),
                          message="Backup saved")
            return {"filename": filename, "destination": str(destination),
                    "deleted": deleted, "completed": completed}
        except Exception as e:
            _write_status(home, state="failed", started=started, completed=core.now(), message=str(e))
            raise
