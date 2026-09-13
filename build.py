#!/usr/bin/python3
"""Build an architecture-independent .deb using only Python and dpkg-deb."""
import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile

from ubuntu_backup import __version__

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
DIST.mkdir(exist_ok=True)

with tempfile.TemporaryDirectory(prefix="ubuntu-backup-build-") as directory:
    stage = Path(directory)
    package = stage / "usr/share/ubuntu-backup/ubuntu_backup"
    shutil.copytree(ROOT / "ubuntu_backup", package, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    binary = stage / "usr/bin/ubuntu-backup"
    binary.parent.mkdir(parents=True)
    binary.write_text('''#!/usr/bin/python3 -I
import sys
sys.path.insert(0, "/usr/share/ubuntu-backup")
from ubuntu_backup.__main__ import main
raise SystemExit(main())
''')
    binary.chmod(0o755)
    for source, target in (("assets/ubuntu-backup.desktop", "usr/share/applications/ubuntu-backup.desktop"),
                           ("assets/ubuntu-backup.svg", "usr/share/icons/hicolor/scalable/apps/ubuntu-backup.svg"),
                           ("README.md", "usr/share/doc/ubuntu-backup/README.md"),
                           ("LICENSE", "usr/share/doc/ubuntu-backup/copyright")):
        dest = stage / target
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / source, dest)
    control = stage / "DEBIAN"
    control.mkdir()
    size = sum(p.stat().st_size for p in stage.rglob("*") if p.is_file()) // 1024 + 1
    (control / "control").write_text(f'''Package: ubuntu-backup
Version: {__version__}
Section: utils
Priority: optional
Architecture: all
Maintainer: denizkarya1999 <denizkarya1999@outlook.com>
Depends: python3 (>= 3.10), python3-gi, gir1.2-gtk-3.0, python3-apt, dconf-cli, pkexec | policykit-1, flatpak, snapd
Installed-Size: {size}
Homepage: https://github.com/denizkarya1999/ubuntu-backup
Description: Back up and migrate an Ubuntu desktop
 Save GNOME preferences, selected personal files and user configurations,
 and APT, Snap and Flatpak application lists. Review and restore on another
 Ubuntu desktop, recover overwritten files, and receive in-app updates.
''')
    sums = []
    for path in sorted(stage.rglob("*")):
        if path.is_file() and "DEBIAN" not in path.parts:
            path.chmod(0o755 if path == binary else 0o644)
            sums.append(hashlib.md5(path.read_bytes()).hexdigest() + "  " + str(path.relative_to(stage)))
        elif path.is_dir():
            path.chmod(0o755)
    (control / "md5sums").write_text("\n".join(sums) + "\n")
    output = DIST / f"ubuntu-backup_{__version__}_all.deb"
    subprocess.run(["dpkg-deb", "--root-owner-group", "--build", str(stage), str(output)], check=True)
    (DIST / "SHA256SUMS").write_text(hashlib.sha256(output.read_bytes()).hexdigest() + "  " + output.name + "\n")
    print(output)
