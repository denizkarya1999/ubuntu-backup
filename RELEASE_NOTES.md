Ubuntu Backup 1.2.3 makes Flatpak optional for the native APT-installed application.

- Installing the `.deb` no longer requires or automatically installs Flatpak.
- Flatpak can be removed without uninstalling Ubuntu Backup.
- APT and Snap application backup, file and settings restore, scheduled backups, and recovery remain available.
- Existing backups with Flatpak entries still open. Flatpak apps stay unchecked when Flatpak is absent; manually selecting one reports that it needs Flatpak, without requesting administrator authentication.
- Flatpak inventory and installation remain available on computers that have it installed.

Install `ubuntu-backup_1.2.3_all.deb`, then close and reopen Ubuntu Backup.
