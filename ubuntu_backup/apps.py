"""Package inventory validation, review, and installation without shell commands."""
import os
import re
import shutil
import subprocess

from .core import TransferError, run, list_snaps

APT_NAME = re.compile(r"[a-z0-9][a-z0-9+.-]*(?::[a-z0-9][a-z0-9-]*)?\Z")
SNAP_NAME = re.compile(r"[a-z0-9][a-z0-9-]*(?:_[a-z0-9]+)?\Z")
FLATPAK_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*){2,}\Z")
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
CHANNEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*){0,2}\Z")
HARDWARE = re.compile(r"^(linux-|nvidia-|libnvidia-|xserver-xorg|grub|shim|firmware-|oem-|ubuntu-drivers|bcmwl|broadcom|intel-microcode|amd64-microcode|.*-dkms(?:$|:))")
FLATHUB_URL = "https://dl.flathub.org/repo/flathub.flatpakrepo"


def validate_inventory(inventory):
    if not isinstance(inventory, dict):
        raise TransferError("Invalid application list")
    for kind, pattern in (("apt", APT_NAME), ("snap", SNAP_NAME), ("flatpak", FLATPAK_NAME)):
        items = inventory.get(kind, [])
        if not isinstance(items, list) or len(items) > 20000:
            raise TransferError("Invalid application list")
        seen = set()
        for app in items:
            if not isinstance(app, dict) or not isinstance(app.get("name"), str) or len(app["name"]) > 250 or not pattern.fullmatch(app["name"]):
                raise TransferError(f"Invalid {kind} application name")
            if kind == "snap":
                channel = app.get("channel", "latest/stable")
                if channel is not None and (not isinstance(channel, str) or len(channel) > 100 or not CHANNEL.fullmatch(channel)):
                    raise TransferError("Invalid Snap channel; local/untracked snaps must be installed manually")
                if not isinstance(app.get("classic", False), bool):
                    raise TransferError("Invalid Snap confinement")
            if kind == "flatpak":
                for field in ("branch", "origin"):
                    if not isinstance(app.get(field), str) or len(app[field]) > 250 or not TOKEN.fullmatch(app[field]):
                        raise TransferError(f"Invalid Flatpak {field}")
                if app.get("scope") not in ("user", "system"):
                    raise TransferError("Invalid Flatpak installation scope")
            identity = (app["name"], app.get("scope"), app.get("branch"))
            if identity in seen:
                raise TransferError("Duplicate application entry")
            seen.add(identity)


def apt_status(names):
    result = {}
    try:
        import apt
        cache = apt.Cache()
        for name in names:
            if name in cache:
                pkg = cache[name]
                result[name] = "installed" if pkg.is_installed else "available" if pkg.candidate else "unavailable"
            else:
                result[name] = "unavailable"
    except (ImportError, OSError, SystemError) as e:
        raise TransferError(f"Could not read Ubuntu software sources: {e}") from e
    return result


def make_plan(inventory, log=lambda x: None):
    validate_inventory(inventory)
    log("Checking apps available on this computer…")
    statuses = apt_status([x["name"] for x in inventory.get("apt", [])])
    plan = []
    for app in inventory.get("apt", []):
        status = statuses[app["name"]]
        hardware = bool(HARDWARE.match(app["name"]))
        plan.append({"kind": "apt", "app": app, "status": status,
                     "selected": status == "available" and not hardware,
                     "detail": "Hardware / boot package; usually leave unchecked" if hardware else
                     "Already installed" if status == "installed" else
                     "Ready to install" if status == "available" else "Add its software source or install manually"})
    snap_installed = set()
    if inventory.get("snap") and shutil.which("snap"):
        try:
            snap_installed = {app["name"] for app in list_snaps()}
        except TransferError as e:
            log(str(e))
    for app in inventory.get("snap", []):
        installed = app["name"] in snap_installed
        supported = bool(shutil.which("snap"))
        special = not app.get("channel") or any(x in app.get("notes", "").split(",") for x in ("base", "os", "snapd", "kernel", "gadget", "devmode", "try", "dangerous"))
        plan.append({"kind": "snap", "app": app,
                     "status": "installed" if installed else "available" if supported else "unavailable",
                     "selected": supported and not installed and not special,
                     "detail": "Already installed" if installed else "Requires snapd" if not supported else
                     f"{app.get('channel', 'latest/stable')}" + (" · classic access" if app.get("classic") else "") +
                     (" · base/system/local snap; review manually" if special else "")})
    for scope in ("user", "system"):
        installed, remotes = set(), set()
        if inventory.get("flatpak") and shutil.which("flatpak"):
            try:
                installed = {tuple(line.split("\t")) for line in run(["flatpak", "list", "--" + scope, "--app", "--columns=application,branch"]).decode().splitlines()}
                remotes = set(run(["flatpak", "remotes", "--" + scope, "--columns=name"]).decode().splitlines())
            except TransferError as e:
                log(str(e))
        for app in inventory.get("flatpak", []):
            if app["scope"] != scope:
                continue
            exists = (app["name"], app["branch"]) in installed
            available = bool(shutil.which("flatpak")) and (app["origin"] in remotes or app["origin"] == "flathub")
            plan.append({"kind": "flatpak", "app": app,
                         "status": "installed" if exists else "available" if available else "unavailable",
                         "selected": available and not exists,
                         "detail": "Already installed" if exists else
                         f"{scope} · {app['origin']} · {app['branch']}" if available else
                         "Install Flatpak and configure its original remote first"})
    return plan


def execute(args, log, timeout=7200):
    """Keep package-manager output visible; never run a shell or use saved commands."""
    import threading
    log("Running: " + " ".join(args))
    p = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, errors="replace",
                         env={**os.environ, "LC_ALL": "C"}, bufsize=1)
    timed_out = threading.Event()
    def timeout_notice():
        timed_out.set()
        # Do not kill a package manager mid-transaction.
        log("Installation is taking longer than expected. Finish any authentication or package prompts.")
    timer = threading.Timer(timeout, timeout_notice)
    timer.daemon = True
    timer.start()
    try:
        for line in p.stdout:
            log(line.rstrip())
        code = p.wait()
        if code:
            raise TransferError(f"Installation command exited with status {code}; see the activity log")
    finally:
        timer.cancel()
        p.stdout.close()


def install_apps(selected, log=lambda x: None):
    check = {kind: [row["app"] for row in selected if row["kind"] == kind] for kind in ("apt", "snap", "flatpak")}
    validate_inventory(check)
    results = []
    apt_names = [a["name"] for a in check["apt"]]
    if apt_names:
        try:
            # no-remove ensures installing an app cannot silently remove existing packages.
            execute(["pkexec", "/usr/bin/apt-get", "-o", "Dpkg::Options::=--force-confold",
                     "install", "--yes", "--no-remove", "--", *apt_names], log)
            statuses = apt_status(apt_names)
            for name in apt_names:
                results.append((name, "installed" if statuses[name] == "installed" else "not installed"))
        except (TransferError, OSError) as e:
            log(str(e))
            # A failed batch may still have installed some packages. Report actual state.
            statuses = apt_status(apt_names)
            results.extend((name, "installed" if statuses[name] == "installed" else str(e)) for name in apt_names)
    for kind in ("snap", "flatpak"):
        for app in check[kind]:
            try:
                if kind == "snap":
                    if not app.get("channel"):
                        raise TransferError("Local/untracked Snap: install it from its original installer")
                    command = ["pkexec", "/usr/bin/snap", "install", app["name"], "--channel=" + app.get("channel", "latest/stable")]
                    if app.get("classic"):
                        command.append("--classic")
                    execute(command, log)
                else:
                    scope = "--" + app["scope"]
                    privilege = ["pkexec", "/usr/bin/flatpak"] if app["scope"] == "system" else ["flatpak"]
                    if app["origin"] == "flathub":
                        execute([*privilege, "remote-add", scope, "--if-not-exists", "flathub", FLATHUB_URL], log)
                    execute([*privilege, "install", scope, "--noninteractive", "--assumeyes", app["origin"],
                             app["name"] + "//" + app["branch"]], log)
                results.append((app["name"], "installed"))
            except (TransferError, OSError) as e:
                log(str(e))
                results.append((app["name"], str(e)))
    return results
