Ubuntu Backup 1.2.1 replaces Google Drive backup with automatic backup to a folder chosen by the user.

- Choose any writable local folder or folder on a connected external drive.
- Schedule the selected content every day or once a week at a chosen time.
- Run in the background through Ubuntu's per-user timer while the app is closed.
- Catch up a missed timer after the user next signs in.
- Save the checked personal files, GNOME changes, user-installed extensions, app configurations, and app list.
- Test the complete selection and destination with **Save and back up now**.
- Delete only app-created automatic backups after 1–3,650 days, with 30 days as the default.
- Show the last success, active run, or failure on the Automatic screen.
- Prevent overlapping automatic runs and reject a destination nested inside a selected source folder.
- Remove the version 1.2.0 Google Drive schedule safely and require a new local destination.

The destination must remain connected, mounted, and writable when the timer runs.
Ubuntu Backup does not upload backup files. Existing local backups remain compatible,
and GNOME preferences plus user-installed extension files remain supported.

Use **Updates** in the app, or download `ubuntu-backup_1.2.1_all.deb` below:

```sh
sudo apt install ./ubuntu-backup_1.2.1_all.deb
```

Close and reopen Ubuntu Backup after upgrading, then configure the Automatic page.
