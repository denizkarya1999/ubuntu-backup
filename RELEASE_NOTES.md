Ubuntu Backup 1.2.2 fixes automatic retention and restore recovery ordering.

- Automatic cleanup now removes only backups belonging to this user’s persistent backup identity. Computers and users can share a destination even if their hostnames match.
- Update every computer using a shared backup folder; older app versions still use the old cleanup behavior.
- Older backups without an owner identity remain available and must be removed manually when no longer needed.
- Undo last restore follows a persistent sequence instead of random directory names, including when restores occur in the same second or the clock changes.
- Existing backups and recovery journals remain supported.

Install `ubuntu-backup_1.2.2_all.deb`, then close and reopen Ubuntu Backup.
