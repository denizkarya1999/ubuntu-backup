Ubuntu Backup 1.1.1 fixes large backups and adds a screenshot gallery.

- Removes the hardcoded 64 GiB archive limit and the 200,000-file selection cap.
- Verifies archives by streaming their contents without extracting a complete temporary copy.
- Restores selected files in a second pass and checks each file again before publishing it.
- Saves completed archives without duplicating the entire file on supported Ubuntu filesystems.
- Shows progress while reading, verifying, and restoring large files.
- Adds five real app screenshots to the repository and README using demonstration files.

Existing backups remain compatible. Keep the backup drive connected while restoring.
Actual free disk space, filesystem limits, and available memory still apply.

Validation includes 130 GiB of sparse-file selection metadata, more than 200,000
selected entries, streaming round trips, changed-archive detection, and recovery
from a second-pass checksum failure. The sparse test checks the former size boundary
without writing a physical 130 GiB test backup.

Use **Updates** in the app, or download `ubuntu-backup_1.1.1_all.deb` below:

```sh
sudo apt install ./ubuntu-backup_1.1.1_all.deb
```

Close and reopen the app after upgrading, then retry your backup.
