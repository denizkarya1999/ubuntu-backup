Ubuntu Backup 1.2.0 adds automatic Google Drive backup and retention.

- Connect Google Drive through Ubuntu's built-in GNOME Online Accounts.
- Browse My Drive and select the destination folder in the app.
- Schedule backups every day or once a week at a selected local time.
- Run in the background through an Ubuntu user timer while the app is closed.
- Catch up a missed timer after the user next signs in.
- Save the currently checked files, user-installed GNOME extensions, GNOME preferences and extension state, and app-list options as the automatic selection.
- Run the saved selection immediately with **Save and back up now**.
- Keep backups for 1–3,650 days, with 30 days as the default.
- Move only expired app-created automatic backups to Google Drive trash.
- Show the last success, active run, or failure on the Automatic screen.
- Prevent overlapping automatic runs and always remove local staging archives.

Google Drive sign-in stays managed by Ubuntu; the app saves only the selected
Drive folder address. Automatic backups require the desktop user to be signed in,
the computer to be awake, and an internet connection. Large backups need enough
local free space for one staging archive before upload. Existing local backup and
restore behavior remains compatible.

Use **Updates** in the app, or download `ubuntu-backup_1.2.0_all.deb` below:

```sh
sudo apt install ./ubuntu-backup_1.2.0_all.deb
```

Close and reopen Ubuntu Backup after upgrading.
