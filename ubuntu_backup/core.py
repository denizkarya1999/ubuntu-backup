"""Backup and restore engine. All home-directory writes run as the user."""
import contextlib
import datetime as dt
import hashlib
import http.client
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import socket
import stat
import subprocess
import tarfile
import tempfile
import urllib.parse
import uuid

FORMAT = "ubuntu-backup"
VERSION = 1
DCONF = {"gnome": "/org/gnome/", "gtk": "/org/gtk/", "ubuntu": "/com/ubuntu/"}
MAX_FILES = 200000
MAX_BYTES = 64 * 1024 ** 3
MAX_MANIFEST = 32 * 1024 ** 2
STATE = ".local/state/ubuntu-backup"
FORBIDDEN = (".ssh", ".gnupg", ".pki", ".git-credentials", ".config/dconf",
             ".local/share/keyrings", ".config/ubuntu-backup", STATE)
SKIP_NAMES = {"Cache", "Caches", "cache", ".cache", "GPUCache", "Code Cache",
              "ShaderCache", "Crashpad", "SingletonLock", "SingletonSocket",
              "SingletonCookie", "lock", ".lock", "LOCK", ".git"}
SENSITIVE = ("chrome", "chromium", "brave", "firefox", "mozilla", "thunderbird",
             "discord", "slack", "signal", "teams", "spotify", "electron",
             "codex", "credentials", "token", "auth", "1password", "keepass")


class TransferError(Exception):
    pass


def run(args, *, data=None, timeout=30):
    try:
        p = subprocess.run(args, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout, env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.TimeoutExpired) as e:
        raise TransferError(f"{args[0]}: {e}") from e
    if p.returncode:
        raise TransferError(f"{args[0]}: {p.stderr.decode(errors='replace').strip() or 'command failed'}")
    return p.stdout


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def system_info():
    fields = {}
    with contextlib.suppress(OSError):
        for line in Path("/etc/os-release").read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                fields[k] = v.strip('"')
    return {"os": fields.get("PRETTY_NAME", "Linux"), "release": fields.get("VERSION_ID", ""),
            "id": fields.get("ID", ""), "architecture": os.uname().machine,
            "host": os.uname().nodename}


def list_snaps():
    """Use snapd's local JSON API; the CLI abbreviates tracked channel branches."""
    class SnapConnection(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(15)
            self.sock.connect("/run/snapd.socket")
    connection = SnapConnection("localhost", timeout=15)
    try:
        connection.request("GET", "/v2/snaps")
        response = connection.getresponse()
        data = response.read(8 * 1024 ** 2 + 1)
        if response.status != 200 or len(data) > 8 * 1024 ** 2:
            raise TransferError("Could not read the local Snap application list")
        result = json.loads(data)
        if not isinstance(result.get("result"), list):
            raise TransferError("Invalid response from the local Snap service")
        return result["result"]
    except (OSError, ValueError, http.client.HTTPException) as e:
        raise TransferError(f"Could not read the local Snap service: {e}") from e
    finally:
        connection.close()


def safe_relative(value):
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise TransferError("Invalid archive path")
    p = PurePosixPath(value)
    if p.is_absolute() or any(x in ("", ".", "..") for x in value.split("/")) or "\x00" in value:
        raise TransferError(f"Unsafe path: {value!r}")
    if any(value == x or value.startswith(x + "/") for x in FORBIDDEN):
        raise TransferError(f"Protected path: {value}")
    return p


def target_path(home, relative, *, snap_current=True, snap_revisions=None):
    """Reject symlink traversal, except a strictly validated Snap current revision."""
    parts = list(safe_relative(relative).parts)
    home = Path(home).resolve()
    if snap_current and len(parts) >= 3 and parts[0] == "snap" and parts[2] == "current":
        parent = target_path(home, "/".join(parts[:2]), snap_current=False)
        link = parent / "current"
        if link.is_symlink():
            revision = os.readlink(link)
            if not re.fullmatch(r"[0-9]+|x[0-9]+", revision):
                raise TransferError(f"Unexpected Snap revision link: {relative}")
            parts[2] = revision
        elif not link.is_dir():
            revision = (snap_revisions or {}).get(parts[1], "")
            if not re.fullmatch(r"[0-9]+|x[0-9]+", revision):
                raise TransferError(f"Install {parts[1]} first, then retry its configuration restore")
            parts[2] = revision
    current = home
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise TransferError(f"Symbolic link at destination: {relative}")
    return current


def private_dir(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def state_dir(home):
    # State is deliberately outside the transferable file scope.
    home = Path(home).resolve()
    path = home
    for part in STATE.split("/"):
        path /= part
        if path.is_symlink():
            raise TransferError("Recovery folder must not be a symbolic link")
    return private_dir(path)


def atomic_write(path, data, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".ubuntu-backup-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode & 0o777)
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def json_bytes(value):
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode()


def discover_configs(home):
    home = Path(home).resolve()
    found = []
    def add(relative, selected=True, label=None):
        try:
            path = target_path(home, relative)
        except TransferError:
            return
        if not path.exists() or path.is_symlink():
            return
        sensitive = any(x in relative.lower() for x in SENSITIVE)
        found.append({"path": relative, "label": label or relative,
                      "selected": selected and not sensitive, "sensitive": sensitive})
    config = home / ".config"
    if config.is_dir() and not config.is_symlink():
        for p in sorted(config.iterdir()):
            if p.name not in {"dconf", "ubuntu-backup", "monitors.xml", "monitors.xml~",
                              "pulse", "pipewire", "ibus", "user-dirs.dirs", "user-dirs.locale"}:
                add(".config/" + p.name, p.name not in {"autostart", "systemd"})
    for relative in (".local/share/gnome-shell/extensions", ".local/share/themes",
                     ".local/share/icons", ".local/share/fonts", ".themes", ".icons", ".fonts"):
        add(relative)
    for relative in (".mozilla", ".thunderbird", ".vscode", ".local/share/applications"):
        add(relative, False)
    flat = home / ".var/app"
    if flat.is_dir() and not flat.is_symlink():
        for app in sorted(flat.iterdir()):
            add(f".var/app/{app.name}/config")
            add(f".var/app/{app.name}/data", False)
    snap = home / "snap"
    if snap.is_dir() and not snap.is_symlink():
        for app in sorted(snap.iterdir()):
            if app.name != "bin" and app.is_dir():
                add(f"snap/{app.name}/current", False)
                add(f"snap/{app.name}/common", False)
    return found


def scan_inventory(log=lambda x: None):
    result = {"apt": [], "snap": [], "flatpak": [], "remotes": [], "warnings": []}
    def attempt(label, callback):
        log(f"Reading {label} apps…")
        try:
            callback()
        except (TransferError, ValueError) as e:
            result["warnings"].append(str(e))
            log(str(e))
    def apt():
        manual = set(run(["apt-mark", "showmanual"]).decode().splitlines())
        data = run(["dpkg-query", "-W", "-f=${binary:Package}\t${Version}\t${db:Status-Status}\n"])
        for line in data.decode().splitlines():
            p = line.split("\t")
            if len(p) == 3 and p[2] == "installed" and (p[0] in manual or p[0].split(":")[0] in manual):
                result["apt"].append({"name": p[0], "version": p[1]})
    def snap():
        if not shutil.which("snap"):
            return
        for app in list_snaps():
            notes = []
            if app.get("type") in ("base", "os", "snapd", "kernel", "gadget"):
                notes.append(app["type"])
            if app.get("devmode"):
                notes.append("devmode")
            result["snap"].append({"name": app["name"], "version": app.get("version", ""),
                "channel": app.get("tracking-channel") or None,
                "classic": app.get("confinement") == "classic", "notes": ",".join(notes)})
    def flatpak():
        if not shutil.which("flatpak"):
            return
        for scope in ("user", "system"):
            data = run(["flatpak", "list", "--" + scope, "--app", "--columns=application,branch,origin"])
            for line in data.decode().splitlines():
                p = line.split("\t")
                if len(p) == 3:
                    result["flatpak"].append({"name": p[0], "branch": p[1], "origin": p[2], "scope": scope})
            data = run(["flatpak", "remotes", "--" + scope, "--columns=name,url"])
            for line in data.decode().splitlines():
                p = line.split("\t")
                if len(p) == 2:
                    result["remotes"].append({"name": p[0], "url": p[1], "scope": scope})
    for label, callback in (("APT", apt), ("Flatpak", flatpak), ("Snap", snap)):
        attempt(label, callback)
    return result


def collect_files(home, roots, log, personal_roots=()):
    files, seen = [], set()
    for root in sorted(set(roots)):
        base = target_path(home, root)
        if not base.exists():
            raise TransferError(f"Selected path no longer exists: {root}")
        def visit(path, rel):
            if any(rel == x or rel.startswith(x + "/") for x in FORBIDDEN):
                log(f"Skipped protected location: {rel}")
                return
            safe_relative(rel)
            personal = any(rel == x or rel.startswith(x + "/") for x in personal_roots)
            if not personal and (path.name in SKIP_NAMES or path.name.endswith((".lock", ".sock"))):
                return
            st = path.lstat()
            if stat.S_ISLNK(st.st_mode):
                log(f"Skipped symbolic link: {rel}")
            elif stat.S_ISDIR(st.st_mode):
                for child in sorted(path.iterdir()):
                    visit(child, rel + "/" + child.name)
            elif stat.S_ISREG(st.st_mode):
                if rel not in seen:
                    seen.add(rel)
                    files.append((rel, path, st))
            else:
                log(f"Skipped special file: {rel}")
        visit(base, root)
    if len(files) > MAX_FILES or sum(st.st_size for _, _, st in files) > MAX_BYTES:
        raise TransferError("Selection exceeds the 200,000-file / 64 GiB archive limit")
    return files


def create_backup(destination, home, roots, inventory, include_gnome=True, log=lambda x: None, personal_roots=()):
    destination = Path(destination).absolute()
    if destination.exists():
        raise TransferError("Choose a new filename; an existing backup will not be overwritten")
    if not set(personal_roots) <= set(roots):
        raise TransferError("Personal-file selection must be part of the backup selection")
    files = collect_files(home, roots, log, personal_roots)
    for _, path, _ in files:
        if path.resolve() == destination.resolve():
            raise TransferError("The backup cannot include itself")
    manifest = {"format": FORMAT, "version": VERSION, "created": now(), "system": system_info(),
                "source_home": str(Path(home).resolve()), "roots": sorted(set(roots)),
                "inventory": inventory, "entries": {}, "settings": [], "personal_roots": list(personal_roots)}
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".ubuntu-backup-", dir=destination.parent)
    os.close(fd)
    try:
        with tarfile.open(temporary, "w:gz", compresslevel=4) as archive:
            def add_bytes(name, data, mode=0o600):
                info = tarfile.TarInfo(name)
                info.size, info.mode = len(data), mode
                archive.addfile(info, io.BytesIO(data))
                manifest["entries"][name] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "mode": mode}
            if include_gnome:
                for key, prefix in DCONF.items():
                    log(f"Saving {prefix} preferences…")
                    add_bytes(f"settings/{key}.ini", run(["dconf", "dump", prefix]))
                    manifest["settings"].append(key)
            for index, (relative, path, initial) in enumerate(files):
                # Open without following links and check for a changing live file.
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as source:
                    st = os.fstat(source.fileno())
                    if not stat.S_ISREG(st.st_mode) or st.st_ino != initial.st_ino:
                        raise TransferError(f"File changed during backup; close its app and retry: {relative}")
                    name = "home/" + relative
                    info = tarfile.TarInfo(name)
                    info.size, info.mode = st.st_size, stat.S_IMODE(st.st_mode) & 0o777
                    info.mtime = int(st.st_mtime)
                    digest = hashlib.sha256()
                    class Reader:
                        def read(self, size=-1):
                            data = source.read(size)
                            digest.update(data)
                            return data
                    archive.addfile(info, Reader())
                    end = os.fstat(source.fileno())
                    if (end.st_size, end.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
                        raise TransferError(f"File changed during backup; close its app and retry: {relative}")
                    manifest["entries"][name] = {"size": st.st_size, "sha256": digest.hexdigest(), "mode": info.mode, "mtime": info.mtime}
                if index % 100 == 0:
                    log(f"Saving configuration files: {index + 1} / {len(files)}")
            data = json_bytes(manifest)
            if len(data) > MAX_MANIFEST:
                raise TransferError("Archive file index is too large")
            info = tarfile.TarInfo("manifest.json")
            info.size, info.mode = len(data), 0o600
            archive.addfile(info, io.BytesIO(data))
        # Exclusive creation protects an existing file if another process created it meanwhile.
        with open(temporary, "rb") as source, open(destination, "xb") as output:
            os.chmod(destination, 0o600)
            try:
                shutil.copyfileobj(source, output)
                output.flush()
                os.fsync(output.fileno())
            except BaseException:
                destination.unlink(missing_ok=True)
                raise
        log(f"Backup saved: {destination}")
        return manifest
    finally:
        Path(temporary).unlink(missing_ok=True)


class Bundle:
    """Validate every member and checksum before making any user-home changes."""
    def __init__(self, filename, log=lambda x: None):
        self.temporary = tempfile.TemporaryDirectory(prefix="ubuntu-backup-")
        self.folder = Path(self.temporary.name)
        self.files = {}
        self.manifest = None
        try:
            total = 0
            seen = set()
            with tarfile.open(filename, "r:gz") as archive:
                for member in archive:
                    name = member.name
                    if name in seen or not member.isfile() or member.size < 0:
                        raise TransferError("Archive contains duplicate names or non-regular files")
                    seen.add(name)
                    if len(seen) > MAX_FILES + 4:
                        raise TransferError("Too many archive entries")
                    if name == "manifest.json":
                        if member.size > MAX_MANIFEST:
                            raise TransferError("Archive manifest is too large")
                    elif name.startswith("home/"):
                        safe_relative(name[5:])
                    elif name not in {f"settings/{x}.ini" for x in DCONF}:
                        raise TransferError(f"Unexpected archive member: {name}")
                    total += member.size
                    if total > MAX_BYTES or member.size > shutil.disk_usage(self.folder).free - 64 * 1024 ** 2:
                        raise TransferError("Not enough temporary disk space, or archive exceeds 64 GiB")
                    if name.startswith("settings/") and member.size > 16 * 1024 ** 2:
                        raise TransferError("Settings entry is too large")
                    target = self.folder / str(len(seen))
                    digest = hashlib.sha256()
                    with archive.extractfile(member) as source, open(target, "xb") as output:
                        while chunk := source.read(1024 * 1024):
                            output.write(chunk)
                            digest.update(chunk)
                    self.files[name] = {"path": target, "size": member.size, "mtime": member.mtime,
                                        "sha256": digest.hexdigest(), "mode": member.mode & 0o777}
                    if len(seen) % 250 == 0:
                        log(f"Verifying archive: {len(seen):,} files")
            self._validate()
        except Exception as e:
            self.close()
            if isinstance(e, TransferError):
                raise
            raise TransferError(f"Cannot read backup: {e}") from e

    def _validate(self):
        if "manifest.json" not in self.files:
            raise TransferError("Missing archive manifest")
        m = json.loads(self.files.pop("manifest.json")["path"].read_text())
        if not isinstance(m, dict) or m.get("format") != FORMAT or m.get("version") != VERSION:
            raise TransferError("Unsupported backup format or version")
        if not isinstance(m.get("entries"), dict) or set(m["entries"]) != set(self.files):
            raise TransferError("Archive does not match its file index")
        roots = m.get("roots")
        if not isinstance(roots, list) or len(roots) > 10000:
            raise TransferError("Invalid configuration selection")
        for root in roots:
            safe_relative(root)
        personal = m.get("personal_roots", [])
        if not isinstance(personal, list) or not all(isinstance(x, str) and x in roots for x in personal):
            raise TransferError("Invalid personal-file selection")
        for name, actual in self.files.items():
            expected = m["entries"][name]
            if not isinstance(expected, dict) or any(expected.get(k) != actual[k] for k in ("size", "sha256", "mode")):
                raise TransferError(f"Backup checksum or metadata mismatch: {name}")
            if name.startswith("home/") and expected.get("mtime") != actual["mtime"]:
                raise TransferError(f"Backup timestamp mismatch: {name}")
            if name.startswith("home/") and not any(name[5:] == root or name[5:].startswith(root + "/") for root in roots):
                raise TransferError("File outside selected configuration folders")
        settings = m.get("settings")
        if not isinstance(settings, list) or len(set(settings)) != len(settings) or any(x not in DCONF for x in settings):
            raise TransferError("Invalid settings selection")
        if {f"settings/{x}.ini" for x in settings} != {x for x in self.files if x.startswith("settings/")}:
            raise TransferError("Settings index does not match archive")
        old_home = m.get("source_home")
        if (not isinstance(old_home, str) or not old_home.startswith("/") or
                len(PurePosixPath(old_home).parts) < 3 or ".." in PurePosixPath(old_home).parts):
            raise TransferError("Invalid source home folder")
        if not isinstance(m.get("system"), dict) or not isinstance(m.get("inventory"), dict):
            raise TransferError("Invalid system or app inventory")
        self.manifest = m

    def close(self):
        self.temporary.cleanup()


def adapt_home(data, old_home, new_home):
    if old_home == new_home or b"\x00" in data or len(data) > 2 * 1024 ** 2:
        return data
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    for old, new in ((urllib.parse.quote(old_home), urllib.parse.quote(new_home)), (old_home, new_home)):
        text = re.sub(re.escape(old) + r"(?=$|[/\s'\"\];,)])", lambda _: new, text)
    return text.encode()


@contextlib.contextmanager
def restore_lock(home):
    import fcntl
    with open(state_dir(home) / "restore.lock", "a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise TransferError("Another restore is already running") from e
        yield


def _undo(home, recovery, log):
    journal = json.loads((recovery / "journal.json").read_text())
    if journal["home"] != str(Path(home).resolve()):
        raise TransferError("Recovery belongs to another home folder")
    for record in reversed(journal["files"]):
        dest = target_path(home, record["relative"], snap_current=False)
        if record["existed"]:
            with open(recovery / record["original"], "rb") as source:
                atomic_copy(dest, source, record["mode"])
            os.utime(dest, ns=(record["mtime_ns"], record["mtime_ns"]))
        elif dest.exists():
            if not dest.is_file():
                raise TransferError(f"Cannot undo changed file type: {dest}")
            dest.unlink()
    for key in journal["settings"]:
        run(["dconf", "reset", "-f", DCONF[key]])
        run(["dconf", "load", DCONF[key]], data=(recovery / f"{key}.ini").read_bytes())
    for relative in reversed(journal.get("directories", [])):
        dest = target_path(home, relative, snap_current=False)
        with contextlib.suppress(OSError):
            dest.rmdir()
    journal["undone"] = now()
    atomic_write(recovery / "journal.json", json_bytes(journal))
    log("Previous configuration and GNOME preferences restored. Installed apps were kept.")


def undo_last(home, log=lambda x: None):
    with restore_lock(home):
        state = state_dir(home)
        records = sorted((state / "recovery").glob("*/journal.json"), reverse=True)
        for record in records:
            journal = json.loads(record.read_text())
            if not journal.get("undone"):
                _undo(home, record.parent, log)
                return
        raise TransferError("There is no restore to undo")


def atomic_copy(destination, source, mode):
    destination = Path(destination)
    fd, temporary = tempfile.mkstemp(prefix=".ubuntu-backup-", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            shutil.copyfileobj(source, stream, 1024 * 1024)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode & 0o777)
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


def restore_bundle(bundle, home, roots, settings=True, rewrite=True, log=lambda x: None):
    home = Path(home).resolve()
    if not set(roots) <= set(bundle.manifest["roots"]):
        raise TransferError("Invalid restore selection")
    with restore_lock(home):
        plan = []
        snap_revisions = None
        for name, entry in bundle.files.items():
            if not name.startswith("home/"):
                continue
            relative = name[5:]
            if not any(relative == root or relative.startswith(root + "/") for root in roots):
                continue
            parts = PurePosixPath(relative).parts
            if (len(parts) >= 3 and parts[0] == "snap" and parts[2] == "current" and
                    not (home / "/".join(parts[:3])).exists() and snap_revisions is None):
                snap_revisions = {app["name"]: str(app.get("revision", "")) for app in list_snaps()}
            dest = target_path(home, relative, snap_revisions=snap_revisions)
            if dest.exists() and not dest.is_file():
                raise TransferError(f"Destination is not a regular file: {dest}")
            for ancestor in dest.parents:
                if ancestor == home:
                    break
                if ancestor.exists() and not ancestor.is_dir():
                    raise TransferError(f"Destination parent is not a folder: {ancestor}")
            plan.append((name, entry, dest))
        needed = sum(x[1]["size"] + (x[2].stat().st_size if x[2].exists() else 0) for x in plan)
        if needed + 64 * 1024 ** 2 > shutil.disk_usage(home).free:
            raise TransferError("Not enough space for restored files and the recovery copy")
        keys = bundle.manifest["settings"] if settings else []
        if not plan and not keys:
            log("No configuration files or GNOME preferences selected")
            return None
        recovery = private_dir(state_dir(home) / "recovery" / (dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]))
        journal = {"home": str(home), "created": now(), "files": [], "settings": [], "directories": []}
        def save():
            atomic_write(recovery / "journal.json", json_bytes(journal))
        save()
        try:
            # Capture ALL original state before making the first change.
            for i, (_, _, dest) in enumerate(plan):
                record = {"relative": str(dest.relative_to(home)), "existed": dest.exists()}
                if record["existed"]:
                    record.update(original=str(i), mode=stat.S_IMODE(dest.stat().st_mode), mtime_ns=dest.stat().st_mtime_ns)
                    shutil.copyfile(dest, recovery / str(i))
                    os.chmod(recovery / str(i), 0o600)
                journal["files"].append(record)
            for key in keys:
                atomic_write(recovery / f"{key}.ini", run(["dconf", "dump", DCONF[key]]))
                journal["settings"].append(key)
            save()
        except Exception:
            journal["undone"] = now()
            save()
            raise
        try:
            for index, (name, entry, dest) in enumerate(plan):
                target_path(home, str(dest.relative_to(home)), snap_current=False)
                missing = []
                parent = dest.parent
                while parent != home and not parent.exists():
                    missing.append(parent)
                    parent = parent.parent
                for directory in reversed(missing):
                    journal["directories"].append(str(directory.relative_to(home)))
                    save()
                    directory.mkdir(mode=0o700)
                personal = bundle.manifest.get("personal_roots", [])
                is_configuration = name[5:].startswith((".", "snap/")) and not any(name[5:] == x or name[5:].startswith(x + "/") for x in personal)
                if rewrite and is_configuration and entry["size"] <= 2 * 1024 ** 2:
                    data = adapt_home(entry["path"].read_bytes(), bundle.manifest["source_home"], str(home))
                    atomic_write(dest, data, entry["mode"])
                else:
                    with open(entry["path"], "rb") as source:
                        atomic_copy(dest, source, entry["mode"])
                os.utime(dest, (entry["mtime"], entry["mtime"]))
                if index % 100 == 0:
                    log(f"Restoring configurations: {index + 1} / {len(plan)}")
            for key in keys:
                data = bundle.files[f"settings/{key}.ini"]["path"].read_bytes()
                if rewrite:
                    data = adapt_home(data, bundle.manifest["source_home"], str(home))
                # Merge saved keys. Unrelated destination keys remain in place.
                run(["dconf", "load", DCONF[key]], data=data)
            journal["completed"] = now()
            save()
        except Exception as original:
            try:
                _undo(home, recovery, log)
            except Exception as rollback:
                raise TransferError(f"Restore failed: {original}. Recovery also failed: {rollback}. Recovery files: {recovery}") from original
            raise TransferError(f"Restore failed; original settings recovered: {original}") from original
        log(f"Recovery copy: {recovery}")
        return str(recovery)
