"""Check the built installer can be used without requiring Flatpak."""
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ubuntu_backup import __version__

package = Path(__file__).resolve().parents[1] / "dist" / f"ubuntu-backup_{__version__}_all.deb"
for field in ("Pre-Depends", "Depends", "Recommends"):
    value = subprocess.check_output(["dpkg-deb", "--field", str(package), field], text=True)
    assert not re.search(r"\bflatpak\b", value), f"{field} would require or auto-install Flatpak"
print("PASS: native .deb does not require or automatically install Flatpak")
