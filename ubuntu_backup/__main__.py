import argparse
import os
from pathlib import Path
import sys

from . import __version__
from .core import Bundle, TransferError


def main():
    parser = argparse.ArgumentParser(description="Back up your Ubuntu desktop and restore it on another computer")
    parser.add_argument("--version", action="version", version="Ubuntu Backup " + __version__)
    parser.add_argument("--inspect", metavar="BACKUP", help="Verify a backup and print a summary without restoring")
    parser.add_argument("--run-scheduled-backup", action="store_true",
                        help="run the configured automatic backup")
    args = parser.parse_args()
    if args.run_scheduled_backup:
        if os.geteuid() == 0:
            print("Open Ubuntu Backup as your normal desktop user, without sudo.", file=sys.stderr)
            return 1
        try:
            from .automation import run_backup
            run_backup(log=print)
            return 0
        except TransferError as e:
            print(str(e), file=sys.stderr)
            return 1
    if args.inspect:
        try:
            bundle = Bundle(args.inspect)
            m = bundle.manifest
            print(f"Ubuntu Backup archive · {m['created']}")
            print(f"Source: {m['system'].get('os', 'Linux')}")
            print(f"Files: {len([x for x in bundle.files if x.startswith('home/')]):,}")
            print(f"Selected files and folders: {len(m['roots'])}")
            print("All checksums verified.")
            bundle.close()
            return 0
        except TransferError as e:
            print(str(e), file=sys.stderr)
            return 1
    if os.geteuid() == 0:
        print("Open Ubuntu Backup as your normal desktop user, without sudo.", file=sys.stderr)
        return 1
    from .ui import Application
    return Application().run([sys.argv[0]])


if __name__ == "__main__":
    sys.exit(main())
