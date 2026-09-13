"""Updates from this project's public GitHub releases, verified before install."""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from . import __version__
from .apps import execute
from .core import TransferError, run

REPOSITORY = "denizkarya1999/ubuntu-backup"
RELEASES = f"https://github.com/{REPOSITORY}/releases"
MAX_DOWNLOAD = 100 * 1024 ** 2


def version_tuple(version):
    if not isinstance(version, str) or not re.fullmatch(r"v?\d+\.\d+\.\d+", version):
        raise TransferError("Unsupported release version")
    return tuple(int(x) for x in version.lstrip("v").split("."))


def request(url, limit):
    req = urllib.request.Request(url, headers={"User-Agent": "Ubuntu-Backup/" + __version__,
                                               "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            if urllib.parse.urlparse(response.geturl()).scheme != "https":
                raise TransferError("Update download must use HTTPS")
            data = response.read(limit + 1)
            if len(data) > limit:
                raise TransferError("Update response exceeds size limit")
            return data
    except (OSError, urllib.error.URLError) as e:
        raise TransferError(f"Could not contact GitHub: {e}") from e


def latest():
    release = json.loads(request(f"https://api.github.com/repos/{REPOSITORY}/releases/latest", 2 * 1024 ** 2))
    if release.get("draft") or release.get("prerelease"):
        return None
    version = release.get("tag_name", "").lstrip("v")
    if version_tuple(version) <= version_tuple(__version__):
        return None
    filename = f"ubuntu-backup_{version}_all.deb"
    asset = next((a for a in release.get("assets", []) if a.get("name") == filename), None)
    sums = next((a for a in release.get("assets", []) if a.get("name") == "SHA256SUMS"), None)
    if not asset or not sums:
        raise TransferError("The latest release is missing its installer or checksums")
    for item in (asset, sums):
        expected = f"https://github.com/{REPOSITORY}/releases/download/v{version}/{item['name']}"
        if item.get("browser_download_url") != expected:
            raise TransferError("Unexpected update download location")
    if not isinstance(asset.get("size"), int) or not 0 < asset["size"] <= MAX_DOWNLOAD:
        raise TransferError("Invalid update download size")
    return {"version": version, "filename": filename, "url": asset["browser_download_url"],
            "checksums": sums["browser_download_url"], "size": asset["size"], "digest": asset.get("digest")}


def install(update, log=lambda x: None):
    log(f"Downloading Ubuntu Backup {update['version']}…")
    sums = request(update["checksums"], 65536).decode()
    matches = [line.split()[0] for line in sums.splitlines()
               if len(line.split()) == 2 and line.split()[1].lstrip("*") == update["filename"]]
    if len(matches) != 1 or not re.fullmatch(r"[a-f0-9]{64}", matches[0]):
        raise TransferError("Missing or invalid installer checksum")
    data = request(update["url"], MAX_DOWNLOAD)
    digest = hashlib.sha256(data).hexdigest()
    if len(data) != update["size"] or digest != matches[0]:
        raise TransferError("Installer checksum failed; update was not installed")
    if update.get("digest") and update["digest"] != "sha256:" + digest:
        raise TransferError("GitHub asset checksum failed; update was not installed")
    with tempfile.TemporaryDirectory(prefix="ubuntu-backup-update-") as directory:
        # APT's unprivileged download worker must be able to read this public package.
        os.chmod(directory, 0o755)
        path = Path(directory) / update["filename"]
        path.write_bytes(data)
        path.chmod(0o644)
        metadata = run(["dpkg-deb", "-f", str(path), "Package", "Version", "Architecture"]).decode()
        fields = dict(line.split(": ", 1) for line in metadata.splitlines() if ": " in line)
        if fields != {"Package": "ubuntu-backup", "Version": update["version"], "Architecture": "all"}:
            raise TransferError("Downloaded package identity does not match the release")
        log("Verified update. Ubuntu may ask for your password.")
        execute(["pkexec", "/usr/bin/apt-get", "install", "--yes", "--no-remove", "--", str(path)], log)
    log("Update installed. Close and reopen Ubuntu Backup to use the new version.")
