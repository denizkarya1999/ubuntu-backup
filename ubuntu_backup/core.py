"""Backup and restore engine. All home-directory writes run as the user."""
import contextlib
import ctypes
import datetime as dt
import errno
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
COPY_CHUNK = 1024 * 1024
PROGRESS_BYTES = 128 * 1024 ** 2
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
    personal_roots = set(personal_roots)
    for root in sorted(set(roots)):
        base = target_path(home, root)
        if not base.exists():
            raise TransferError(f"Selected path no longer exists: {root}")
        def visit(path, rel):
            if any(rel == x or rel.startswith(x + "/") for x in FORBIDDEN):
                log(f"Skipped protected location: {rel}")
                return
            safe_relative(rel)
            personal = selected_path(rel, personal_roots)
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
    return files


def selected_path(relative, roots):
    """Check selected ancestors without scanning every root for every file."""
    while relative:
        if relative in roots:
            return True
        relative, separator, _ = relative.rpartition("/")
        if not separator:
            break
    return False


def publish_archive(temporary, destination):
    """Publish without a second full copy or overwriting an existing backup."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is not None:
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(-100, os.fsencode(temporary), -100, os.fsencode(destination), 1) == 0:
            return
        error = ctypes.get_errno()
        if error not in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
            raise OSError(error, os.strerror(error), str(destination))
    # Older/network filesystems may lack rename flags. Hard linking also creates
    # the destination exclusively without duplicating its contents.
    try:
        os.link(temporary, destination)
    except OSError as e:
        if e.errno not in (errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS):
            raise
        # Last-resort compatibility: copy only when neither atomic operation is
        # supported. A failed copy removes only the new file created here.
        with open(temporary, "rb") as source, open(destination, "xb") as output:
            os.chmod(destination, 0o600)
            try:
                shutil.copyfileobj(source, output, COPY_CHUNK)
                output.flush()
                os.fsync(output.fileno())
            except BaseException:
                Path(destination).unlink(missing_ok=True)
                raise


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
                        count = 0
                        reported = 0
                        def read(self, size=-1):
                            data = source.read(size)
                            digest.update(data)
                            self.count += len(data)
                            if self.count - self.reported >= PROGRESS_BYTES:
                                self.reported = self.count
                                log(f"Saving {relative}: {self.count / 1024 ** 3:.1f} / {st.st_size / 1024 ** 3:.1f} GiB")
                            return data
                    archive.addfile(info, Reader())
                    end = os.fstat(source.fileno())
                    if (end.st_size, end.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
                        raise TransferError(f"File changed during backup; close its app and retry: {relative}")
                    manifest["entries"][name] = {"size": st.st_size, "sha256": digest.hexdigest(), "mode": info.mode, "mtime": info.mtime}
                if index % 100 == 0:
                    log(f"Saving files: {index + 1} / {len(files)}")
            data = json_bytes(manifest)
            info = tarfile.TarInfo("manifest.json")
            info.size, info.mode = len(data), 0o600
            archive.addfile(info, io.BytesIO(data))
        with open(temporary, "rb+") as output:
            os.fsync(output.fileno())
        publish_archive(temporary, destination)
        log(f"Backup saved: {destination}")
        return manifest
    finally:
        Path(temporary).unlink(missing_ok=True)


class VerifiedReader:
    """Check streamed contents before atomic_copy can publish a restored file."""
    def __init__(self, source, entry, name, log=lambda _: None):
        self.source, self.entry, self.name = source, entry, name
        self.digest = hashlib.sha256()
        self.count = 0
        self.reported = 0
        self.log = log

    def read(self, size=-1):
        data = self.source.read(size)
        self.count += len(data)
        self.digest.update(data)
        if self.count - self.reported >= PROGRESS_BYTES:
            self.reported = self.count
            self.log(f"Restoring {self.name[5:]}: {self.count / 1024 ** 3:.1f} / {self.entry['size'] / 1024 ** 3:.1f} GiB")
        if not data or self.count >= self.entry["size"]:
            self.verify()
        return data

    def verify(self):
        if self.count != self.entry["size"] or self.digest.hexdigest() != self.entry["sha256"]:
            raise TransferError(f"Backup checksum mismatch or incomplete entry: {self.name}")


class Bundle:
    """Verify in a streaming pass; read selected contents again when restoring."""
    def __init__(self, filename, log=lambda x: None):
        self.source = None
        self.files = {}
        self.manifest = None
        try:
            self.source = open(filename, "rb")
            self.signature = self._signature()
            seen = set()
            index_budget = 16 * 1024 ** 2
            with tarfile.open(fileobj=self.source, mode="r|gz") as archive:
                for member in archive:
                    name = member.name
                    if name in seen or not member.isfile() or member.size < 0:
                        raise TransferError("Archive contains duplicate names or non-regular files")
                    seen.add(name)
                    if name == "manifest.json":
                        # The index budget grows with actual payload entries;
                        # this is not a total-data or file-count cap. Generated
                        # archives put the manifest last, after all file headers.
                        if member.size > index_budget:
                            raise TransferError("Archive file index is disproportionate to its contents")
                        with archive.extractfile(member) as source:
                            self.manifest = json.load(source)
                        continue
                    elif name.startswith("home/"):
                        safe_relative(name[5:])
                    elif name not in {f"settings/{x}.ini" for x in DCONF}:
                        raise TransferError(f"Unexpected archive member: {name}")
                    if name.startswith("settings/") and member.size > 16 * 1024 ** 2:
                        raise TransferError("Settings entry is too large")
                    digest = hashlib.sha256()
                    count = 0
                    reported = 0
                    with archive.extractfile(member) as source:
                        while chunk := source.read(COPY_CHUNK):
                            count += len(chunk)
                            digest.update(chunk)
                            if count - reported >= PROGRESS_BYTES:
                                reported = count
                                log(f"Verifying {name[5:]}: {count / 1024 ** 3:.1f} / {member.size / 1024 ** 3:.1f} GiB")
                    if count != member.size:
                        raise TransferError(f"Incomplete archive entry: {name}")
                    self.files[name] = {"size": member.size, "mtime": member.mtime,
                                        "sha256": digest.hexdigest(), "mode": member.mode & 0o777}
                    index_budget += len(name.encode("utf-8")) * 6 + 1024
                    # Avoid retaining a second copy of every TarInfo object.
                    archive.members.clear()
                    if len(seen) % 250 == 0:
                        log(f"Verifying archive: {len(seen):,} files")
            self._validate()
            self.assert_unchanged()
        except Exception as e:
            self.close()
            if isinstance(e, TransferError):
                raise
            raise TransferError(f"Cannot read backup: {e}") from e

    def _validate(self):
        if self.manifest is None:
            raise TransferError("Missing archive manifest")
        m = self.manifest
        if not isinstance(m, dict) or m.get("format") != FORMAT or m.get("version") != VERSION:
            raise TransferError("Unsupported backup format or version")
        if not isinstance(m.get("entries"), dict) or set(m["entries"]) != set(self.files):
            raise TransferError("Archive does not match its file index")
        roots = m.get("roots")
        if not isinstance(roots, list):
            raise TransferError("Invalid configuration selection")
        for root in roots:
            safe_relative(root)
        root_set = set(roots)
        personal = m.get("personal_roots", [])
        if not isinstance(personal, list) or not all(isinstance(x, str) and x in root_set for x in personal):
            raise TransferError("Invalid personal-file selection")
        for name, actual in self.files.items():
            expected = m["entries"][name]
            if not isinstance(expected, dict) or any(expected.get(k) != actual[k] for k in ("size", "sha256", "mode")):
                raise TransferError(f"Backup checksum or metadata mismatch: {name}")
            if name.startswith("home/") and expected.get("mtime") != actual["mtime"]:
                raise TransferError(f"Backup timestamp mismatch: {name}")
            if name.startswith("home/") and not selected_path(name[5:], root_set):
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
        self.files = m["entries"]

    def _signature(self):
        st = os.fstat(self.source.fileno())
        return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns

    def assert_unchanged(self):
        if self.source is None or self.source.closed or self._signature() != self.signature:
            raise TransferError("The backup changed after verification. Open the backup again before restoring.")

    def iter_files(self, names, log=lambda _: None):
        """Yield checked readers in archive order with no extracted staging copy."""
        self.assert_unchanged()
        remaining = set(names)
        if not remaining <= self.files.keys():
            raise TransferError("Selected file is missing from the backup")
        self.source.seek(0)
        with tarfile.open(fileobj=self.source, mode="r|gz") as archive:
            for member in archive:
                if member.name in remaining:
                    expected = self.files[member.name]
                    if (not member.isfile() or member.size != expected["size"] or
                            member.mode & 0o777 != expected["mode"] or
                            member.mtime != expected.get("mtime", 0)):
                        raise TransferError("The backup changed after verification")
                    with archive.extractfile(member) as source:
                        reader = VerifiedReader(source, expected, member.name, log)
                        yield member.name, reader
                        reader.verify()
                    remaining.remove(member.name)
                archive.members.clear()
                if not remaining:
                    break
        if remaining:
            raise TransferError("Selected files are missing from the backup")
        self.assert_unchanged()

    def close(self):
        if self.source is not None:
            self.source.close()


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


def recovery_records(state):
    records = []
    for path in (state / "recovery").glob("*/journal.json"):
        journal = json.loads(path.read_text())
        # Legacy journals have no sequence; use their filesystem timestamp.
        order = journal.get("sequence", path.stat().st_mtime_ns)
        records.append((order, path, journal))
    return sorted(records, key=lambda record: (record[0], str(record[1])), reverse=True)


def undo_last(home, log=lambda x: None):
    with restore_lock(home):
        state = state_dir(home)
        records = recovery_records(state)
        for _, record, journal in records:
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
    bundle.assert_unchanged()
    roots = set(roots)
    personal = set(bundle.manifest.get("personal_roots", []))
    if not roots <= set(bundle.manifest["roots"]):
        raise TransferError("Invalid restore selection")
    with restore_lock(home):
        plan = []
        snap_revisions = None
        for name, entry in bundle.files.items():
            if not name.startswith("home/"):
                continue
            relative = name[5:]
            if not selected_path(relative, roots):
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
        state = state_dir(home)
        previous = recovery_records(state)
        sequence = previous[0][0] + 1 if previous else 1
        recovery = private_dir(state / "recovery" / (dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]))
        journal = {"home": str(home), "created": now(), "sequence": sequence,
                   "files": [], "settings": [], "directories": []}
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
            plan_by_name = {name: (entry, dest) for name, entry, dest in plan}
            wanted = set(plan_by_name) | {f"settings/{key}.ini" for key in keys}
            settings_data = {}
            completed = 0
            with contextlib.closing(bundle.iter_files(wanted, log)) as contents:
                for name, source in contents:
                    if name.startswith("settings/"):
                        settings_data[name] = source.read()
                        continue
                    entry, dest = plan_by_name[name]
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
                    is_configuration = name[5:].startswith((".", "snap/")) and not selected_path(name[5:], personal)
                    if rewrite and is_configuration and entry["size"] <= 2 * 1024 ** 2:
                        data = adapt_home(source.read(), bundle.manifest["source_home"], str(home))
                        atomic_write(dest, data, entry["mode"])
                    else:
                        atomic_copy(dest, source, entry["mode"])
                    os.utime(dest, (entry["mtime"], entry["mtime"]))
                    completed += 1
                    if completed % 100 == 1:
                        log(f"Restoring files: {completed} / {len(plan)}")
            for key in keys:
                data = settings_data[f"settings/{key}.ini"]
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
