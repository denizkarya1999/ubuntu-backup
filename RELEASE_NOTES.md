Ubuntu Backup 1.1.0 improves the desktop interface:

- Dark theme throughout the app, file pickers, dialogs, and activity report.
- Hidden dot-prefixed entries are no longer shown in file pickers or added to the personal-file list.
- Personal files and app configurations have separate tabs. Configuration choices use readable names instead of hidden paths.
- New **About Us** screen with app name, live version, developer, agent used, Python, GTK 3, CSS, and SVG details.

Backup and restore formats are unchanged; existing `.ubackup` files are compatible.
Hidden files inside a selected folder remain part of its backup. This update changes
what is displayed, not the saved contents of selected folders.

Existing installations can receive this release through **Updates**, or download
`ubuntu-backup_1.1.0_all.deb` below and install it with:

```sh
sudo apt install ./ubuntu-backup_1.1.0_all.deb
```

Close and reopen Ubuntu Backup after upgrading.
