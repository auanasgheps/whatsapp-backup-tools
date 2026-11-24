[← Back to Archiver Overview](index.md) • [Configuration](configuration.md) • [Recipes & Restore](recipes.md) • [Main Readme](../../README.md)

# Command Reference

Comprehensive reference for all `wab-archiver` CLI subcommands, arguments, flags, and output artifacts.

> 💡 **Quick Links:**
> - To set up repeatable runs using a config file, see the **[Configuration Guide](configuration.md)**.
> - For real-world command examples and recipes, see **[CLI Recipes, Workflows & Restore](recipes.md)**.

---

## CLI Synopsis

```bash
wab-archiver [SUBCOMMAND] [OPTIONS]
```

If no subcommand is specified, `archive` is invoked by default.

### Available Subcommands

| Subcommand | Description |
|---|---|
| `archive` *(default)* | Ingest, decrypt, organize, and archive WhatsApp media and databases |
| `restore` | Reconstruct the flat `Media/` tree and `Databases/msgstore.db` from an archive |
| `update-metadata` | Retroactively inject EXIF and container timestamps into existing archived media |
| `config generate` | Write an `example-config.toml` template to the package folder |

---

## Subcommand: `archive` (Default)

The `archive` command processes source databases and media folders, maps contacts and group chats, copies or hardlinks media chronologically, and saves decrypted databases for the viewer.

### Syntax

```bash
wab-archiver archive [--wa-root PATH | --ios-backup PATH]
                     [--from-adb] [--msgstore PATH] [--e2e-key KEY]
                     [-c PATH] [--ios-password PASSWORD] [--ios-contacts PATH]
                     [--business] [--pull-media] [--staging PATH]
                     -o PATH [-l PATH] [--timezone TZ] [--config PATH]
                     [--dry-run] [--limit N] [--since DATE]
                     [--link-duplicates] [-P] [--exif-timestamps] [-v]
```

### Options & Arguments

| Option | Type | Required | Description |
|---|---|---|---|
| `-o`, `--output PATH` | Path | **Yes** | Destination folder for the archive (stores `Contacts/`, `Groups/`, databases, and reports). |
| `--wa-root PATH` | Path | Android / iOS Pre-extracted | Root path of your WhatsApp folder (containing `Media/`). **Repeat the flag** to specify multiple source folders. Mutually exclusive with `--ios-backup` and `--pull-media`. |
| `--ios-backup PATH` | Path | iOS | Path to the iPhone backup directory (containing `Manifest.db`). Mutually exclusive with `--wa-root` and `--from-adb`. |
| `--from-adb` | Flag | ADB Mode | Pull `msgstore.db.crypt15`, daily increments, and contacts automatically from a connected Android device. Mutually exclusive with `--ios-backup`. |
| `--msgstore PATH` | Path | No | Path to `msgstore.db`, `msgstore.db.crypt15`, or `ChatStorage.sqlite`. Defaults to `msgstore.db` in the current folder. |
| `--e2e-key KEY` | String | If Encrypted | 64-character hexadecimal key for `.crypt15` decryption. |
| `-c`, `--contacts PATH` | Path | No | Path to an existing contacts file (CSV formatted as `phone,name` or exported `wa_contacts`). |
| `--ios-password PASSWORD` | String | No | Password for decrypting an encrypted iPhone backup. |
| `--ios-contacts PATH` | Path | No | Path to `ContactsV2.sqlite`. Auto-extracted from `--ios-backup` if omitted. |
| `--business` | Flag | No | Target **WhatsApp Business** (`com.whatsapp.w4b`) instead of regular WhatsApp. |
| `--pull-media` | Flag | No | Pull WhatsApp media files over USB via ADB. Only valid with `--from-adb`. Mutually exclusive with `--wa-root`. |
| `--staging PATH` | Path | If `--pull-media` | Permanent local folder where ADB-pulled media is stored (treated as `--wa-root`). |
| `-l`, `--log PATH` | Path | No | Custom log file path. Defaults to `<output>/wab-archiver.log`. |
| `-v`, `--debug`, `--verbose` | Flag | No | Enable debug-level logging in the log file (defaults to `INFO`). |
| `--timezone TZ` | String | No | [IANA timezone name](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones) (e.g. `Europe/Rome`, `UTC`) for folder timestamps and reports. Windows requires `pip install tzdata`. |
| `--config PATH` | Path | No | Path to a TOML config file. Auto-detects `config.toml` in package or current folder if omitted. |
| `--dry-run` | Flag | No | Simulate the run without copying files, writing reports, or modifying archive state. Decrypts database in memory/temp to allow schema queries. |
| `--limit N` | Integer | No | Cap rows processed per chat type. Total rows ≤ N. Useful for quick smoke tests. |
| `--since DATE` | Date | No | Only archive media from messages sent or received on or after this date (`YYYY-MM-DD`). |
| `--link-duplicates` | Flag | No | Hardlink duplicate media across chats instead of copying bytes to save disk space. Automatically falls back to standard copy on unsupported filesystems (e.g. exFAT, FAT32). |
| `-P`, `--preserve-deleted` | Flag | No | Retain messages deleted on the phone ("Delete for Me"), sender-revoked messages ("Delete for Everyone"), and deleted chats into `preserved_messages.db`. |
| `--exif-timestamps` | Flag | No | Inject WhatsApp message timestamps directly into media headers (EXIF `DateTimeOriginal` for JPEG, QuickTime `creation_time` for MP4/MOV) for chronological cloud gallery indexing. |

---

## Subcommand: `restore`

Reconstructs the original flat `WhatsApp/Media/` directory tree and restores the latest `Databases/msgstore.db` directly inside the archive folder.

### Syntax

```bash
wab-archiver restore -o PATH [-l PATH] [-v] [--dry-run] [--config PATH]
```

### Options & Arguments

| Option | Type | Required | Description |
|---|---|---|---|
| `-o`, `--output PATH` | Path | **Yes** | Destination folder of the archive (must contain `.wa_media_archiver.db`). |
| `-l`, `--log PATH` | Path | No | Custom log file path (defaults to `<output>/wab-archiver.log`). |
| `-v`, `--debug`, `--verbose` | Flag | No | Enable debug-level logging. |
| `--dry-run` | Flag | No | Preview files that would be restored without copying bytes. |
| `--config PATH` | Path | No | Path to a TOML config file (auto-detected if omitted). |

> 📖 For full details and operational behavior, see **[Restore Mode in Recipes](recipes.md#restore-mode)**.

---

## Subcommand: `update-metadata`

Retroactively writes EXIF tags and container creation timestamps into previously archived media files without re-archiving or connecting devices.

### Syntax

```bash
wab-archiver update-metadata -o PATH [--timezone TZ] [--config PATH] [--dry-run] [--force] [-l PATH] [-v]
```

### Options & Arguments

| Option | Type | Required | Description |
|---|---|---|---|
| `-o`, `--output PATH` | Path | **Yes** | Destination folder of the archive (must contain `.wa_media_archiver.db`). |
| `--timezone TZ` | String | No | Target timezone for local EXIF timestamps and `OffsetTimeOriginal` (falls back to `config.toml` or system time). |
| `--config PATH` | Path | No | Path to a TOML config file (auto-detected if omitted). |
| `--dry-run` | Flag | No | Preview files that would be stamped without writing to disk or database. |
| `--force` | Flag | No | Re-evaluate all media files even if previously marked as stamped. |
| `-l`, `--log PATH` | Path | No | Custom log file path (defaults to `<output>/wab-archiver.log`). |
| `-v`, `--debug`, `--verbose` | Flag | No | Enable debug-level logging. |

> 📖 For engine details and cloud gallery compatibility, see **[Retroactive Metadata Backfill in Recipes](recipes.md#5-retroactively-backfill-metadata-on-existing-archives-update-metadata)**.

---

## Subcommand: `config`

Generates an annotated example configuration file template.

### Syntax

```bash
wab-archiver config generate
```

Writes `example-config.toml` to the package folder and exits. See the **[Configuration Guide](configuration.md)** for file format and discovery rules.

---

## Output Files & Artifacts

After execution, the following files and reports are created or updated inside your archive output folder:

| File / Artifact | Type | Description |
|---|---|---|
| `wab-archiver.log` | Log File | Detailed execution log (`INFO` level by default, or `DEBUG` when `-v` is passed). |
| `missing_media_report.csv` | Audit CSV | Media files referenced in the database but not found on disk. Useful for manual recovery from older backups. |
| `duplicate_media_report.csv` | Audit CSV | Media files with identical MD5 hashes shared across chats, organized into visual sections by media type. Includes columns `media_type`, `group_id`, `file_count`, `archived_path`, `md5_hex`, `link_status`, and `original_timestamp`. Written only when duplicates exist. |
| `source_conflicts_report.csv` | Audit CSV | Discrepancies encountered when multiple `--wa-root` roots contain conflicting versions of the same file. Written only when conflicts exist. |
| `adb_conflicts_report.csv` | Audit CSV | Discrepancies when `--pull-media` finds a file with the same name but different content on device vs archive. Written only when conflicts exist. |
| `restore_report.csv` | Audit CSV | Issues encountered during `restore` mode (e.g. missing files). Written only when issues occur. |
| `.wa_media_archiver.db` | SQLite DB | Persistent archiver state database: contact folder map, group folder map, file archive index, and metadata stamping flags. Self-heals and verifies integrity on every open. |
| `preserved_messages.db` | SQLite DB | Retained messages deleted from phone ("Delete for Me"), sender-revoked messages ("Delete for Everyone"), voice/video calls (`preserved_calls`), interactive polls, shared locations, and deleted chats. Written only when `-P` / `--preserve-deleted` is enabled. |

---

## Next Steps

- ⚙️ **[Configuration Guide](configuration.md)** — Set up a reusable `config.toml` file.
- 💡 **[CLI Recipes, Workflows & Restore](recipes.md)** — View practical recipes for Android, iOS, hardlinks, and data recovery.
- 🌐 **[WhatsApp Backup Viewer Guide](../viewer/index.md)** — Browse and search your archived chats in the web viewer.
