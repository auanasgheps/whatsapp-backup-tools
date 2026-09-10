# Command Reference

## Config File

The config file is the easiest way to run (and re-run) the archiver: instead of repeating settings on the command line every time, save them in a `config.toml` file.

### Generating the Example File

```bash
wab-archiver config generate
```

This writes `example-config.toml` next to the package. Open it, fill in your values, then rename it to `config.toml`.

### Format

```toml
# wab-archiver config
# All paths can be absolute or relative to where you run the script.

output     = "/path/to/archive"

# Optional settings — uncomment to activate:
# msgstore  = "msgstore.db"
# e2e_key   = ""
# wa_root   = ""              # single source folder
# wa_root   = ["/path/to/old-archive", "/path/to/current-phone/WhatsApp"]  # multiple source folders
# contacts  = ""
# log       = ""
# business  = false
# timezone  = ""          # e.g. Europe/Rome
# since     = ""          # e.g. 2024-01-01

# Android — pull via ADB (wab-archiver archive --from-adb; see setup-android.md)
# from_adb  = false
# pull_media = false
# staging    = ""         # persistent folder for pulled media

# iOS
# ios_backup   = ""
# ios_password = ""
# ios_contacts = ""
```

> 💡 **Windows users:** You can paste Windows paths directly (e.g. `C:\Users\...`). The archiver automatically converts backslashes to forward slashes and updates the config file in place.

### How the Config File Is Found

1. **Explicit path** — pass `--config /path/to/myconfig.toml`. No confirmation prompt.
2. **Auto-detection** — the archiver checks for `config.toml` in the package folder and the current directory:
   - Exactly one found → confirmation prompt `Found config.toml at <path> — use it? [Y/n]`
   - Two found → error; use `--config` to specify which one
   - None found → config file ignored, CLI args only

### Precedence

CLI arguments always win. A value set in `config.toml` acts as a default and is overridden by anything explicitly passed on the command line.

`--dry-run` and `--limit` are intentionally excluded from the config file — they are one-off flags and can always be appended to the command line.

---

## Command Reference

The archiver uses subcommands. `archive` is the default and can be omitted.

### `archive` (default)

```
wab-archiver archive [--wa-root PATH | --ios-backup PATH | --from-adb]
                     [--msgstore PATH] [--e2e-key KEY]
                     [-c PATH] [--ios-password PASSWORD] [--ios-contacts PATH]
                     [--business] [--pull-media] [--staging PATH]
                     -o PATH [-l PATH] [--timezone TZ] [--config PATH]
                     [--dry-run] [--limit N] [--since DATE]
```

| Argument | Required | Description |
|---|---|---|
| `--wa-root PATH` | Android / iOS pre-extracted | Root path of your WhatsApp folder. **Repeat the flag** to specify multiple source folders |
| `--ios-backup PATH` | iOS (recommended) | Path to the iPhone backup directory (the folder containing `Manifest.db`). Mutually exclusive with `--wa-root` |
| `--from-adb` | ADB mode | Pull msgstore and contacts automatically from a connected Android device via ADB. Mutually exclusive with `--wa-root` and `--ios-backup` |
| `--msgstore PATH` | No | Path to `msgstore.db`, `msgstore.db.crypt15`, or `ChatStorage.sqlite`. Defaults to `msgstore.db` in the current folder |
| `--e2e-key KEY` | If encrypted | Your cryptographic key for `.crypt15` decryption |
| `-c`, `--contacts PATH` | No | Path to the `wa_contacts` file exported via ADB (Android only) |
| `--ios-password PASSWORD` | No | Password for an encrypted iPhone backup |
| `--ios-contacts PATH` | No | Path to `ContactsV2.sqlite` for iOS contacts. Auto-extracted from `--ios-backup` if omitted |
| `--business` | No | Target **WhatsApp Business** instead of the regular WhatsApp app |
| `--pull-media` | No | Pull WhatsApp media files from the device via ADB. Only valid with `--from-adb`. See [Android setup](setup-android.md#adb-pull-media-optional) |
| `--staging PATH` | If `--pull-media` | Local directory where ADB-pulled media is staged. Must be persistent across runs |
| `-o`, `--output PATH` | **Yes** | Destination folder for the archive |
| `-l`, `--log PATH` | No | Custom log file path. Defaults to `<output>/wab-archiver.log` |
| `--timezone TZ` | No | [IANA timezone name](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones) for year folders and report timestamps. **Windows users: requires `pip install tzdata`** |
| `--config PATH` | No | Path to a TOML config file. Auto-detects `config.toml` if omitted |
| `--dry-run` | No | Simulate the run without copying any files |
| `--limit N` | No | Cap rows returned per chat type. Total rows ≤ N. Useful for test runs |
| `--since DATE` | No | Only include messages on or after this date (`YYYY-MM-DD`) |

### `restore`

Reconstruct the original `Media/` tree from an archive (Android only).

```
wab-archiver restore -o PATH [-l PATH] [--dry-run] [--config PATH]
```

### `config`

```
wab-archiver config generate
```

Writes `example-config.toml` to the package folder and exits.

---

## CLI Examples

> 💡 The examples below use `\` to split long commands. Replace it with the correct character for your shell:

| Shell | Character |
|---|---|
| bash / zsh (Linux, macOS) | `\` |
| PowerShell (Windows) | `` ` `` |
| cmd.exe (Windows) | `^` |

### Android

#### ADB mode — automatic pull and decrypt (recommended)

```bash
wab-archiver archive \
  --from-adb \
  --e2e-key 1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b \
  --output /path/to/output \
  --dry-run
```

#### ADB mode — also pull media over USB

Use this if you do not use Syncthing and want the archiver to handle everything in one step. See [setup-android.md](setup-android.md#adb-pull-media-optional) for important caveats about speed and reliability.

```bash
wab-archiver archive \
  --from-adb \
  --e2e-key 1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b \
  --pull-media \
  --staging /path/to/wa-staging \
  --output /path/to/output
```

> ⚠️ On first run, every media file is transferred — this can take hours for large collections. Subsequent runs skip already-archived files automatically.

#### Manual — decrypted database

```bash
wab-archiver archive \
  --msgstore /path/to/msgstore.db \
  --wa-root /path/to/WhatsApp/storage \
  --output /path/to/output \
  --contacts /path/to/wa_contacts \
  --dry-run
```

#### Manual — multiple source folders

Use this when media is split across several locations. Repeat `--wa-root` for each source.

```bash
wab-archiver archive \
  --msgstore /path/to/msgstore.db \
  --wa-root /path/to/old-archive/WhatsApp \
  --wa-root /path/to/current-phone/WhatsApp \
  --output /path/to/output \
  --dry-run
```

> 💡 When the same file exists in multiple roots, the largest copy is used (higher quality heuristic). If sizes match but content differs, the first root takes priority and the conflict is written to `source_conflicts_report.csv` for review.

#### Manual — encrypted database (archiver decrypts)

```bash
wab-archiver archive \
  --msgstore /path/to/msgstore.db.crypt15 \
  --e2e-key 1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b \
  --wa-root /path/to/WhatsApp/storage \
  --output /path/to/output \
  --dry-run
```

> 💡 Even in dry run mode, decryption still occurs so the DB can be queried. The decrypted `msgstore.db` is written to disk regardless of `--dry-run`.

### iOS

#### Standard flow — unencrypted backup

```bash
wab-archiver archive \
  --ios-backup ~/Library/Application\ Support/MobileSync/Backup/<UDID> \
  --output /path/to/output \
  --dry-run
```

#### Standard flow — encrypted backup

```bash
wab-archiver archive \
  --ios-backup ~/Library/Application\ Support/MobileSync/Backup/<UDID> \
  --ios-password your_backup_password \
  --output /path/to/output \
  --dry-run
```

> 💡 `--wa-root` and `--contacts` are not needed. The archiver extracts `ChatStorage.sqlite` and `ContactsV2.sqlite` directly from the backup. `ChatStorage.sqlite` is also saved to the output folder for re-runs.

### Advanced

#### Test run — recent files only

```bash
wab-archiver archive \
  --msgstore /path/to/msgstore.db \
  --wa-root /path/to/WhatsApp/storage \
  --output /path/to/output \
  --since 2025-01-01 \
  --limit 100 \
  --dry-run
```

#### iOS — pre-extracted mode

Only use this mode if you know what you are doing.

```bash
wab-archiver archive \
  --msgstore /path/to/ChatStorage.sqlite \
  --wa-root /path/to/AppDomainGroup-group.net.whatsapp.WhatsApp.shared \
  --ios-contacts /path/to/ContactsV2.sqlite \
  --output /path/to/output
```

---

## Output Files

| File | Description |
|---|---|
| `wab-archiver.log` | Full run log including all copied, skipped, and missing files |
| `missing_media_report.csv` | Structured report of all media referenced in the DB but not found on disk. Useful for manual recovery from old backups |
| `duplicate_media_report.csv` | Report of media files with identical content at multiple archive paths. One row per path, sortable by `file_count`. Only written when duplicates exist |
| `source_conflicts_report.csv` | Written when multiple `--wa-root` roots contain different versions of the same file. Only written when conflicts exist |
| `.wa_media_archiver.db` | SQLite database storing all persistent state: contact folder index, group folder index, and the file archive map. Health checks run automatically on every open. Do not delete unless you want to reset all tracking |
| `adb_conflicts_report.csv` | Written when `--pull-media` finds a file with the same name but different content on the device vs the archive. Only written when conflicts exist; resolve manually |
| `restore_report.csv` | Written by restore mode when issues are encountered. Not written if there are no issues |

---

## Restore Mode

Restore mode reconstructs the flat `WhatsApp/Media/` folder structure directly inside the archive folder, without needing the original device or database.

> ⚠️ **Android archives only.** iOS media cannot be restored this way.

```bash
wab-archiver restore --output /path/to/output
```

The reconstructed tree is written to `<output>/Media/`, alongside the existing `Contacts/` and `Groups/` folders. No files are overwritten — identical files already in place are skipped silently.

**How it works:** During every real (non-dry-run) forward run, the archiver records each original `file_path` and the archive path it was copied to in `.wa_media_archiver.db`. Restore mode reads this data and copies each unique original file back to its original relative path. If the same file was archived from multiple chats, it is restored exactly once.

**Limitations:**
- Restored file timestamps reflect the WhatsApp message timestamp (same as in the archive), not the original on-device creation date
- If a forward run was filtered with `--since` or `--limit`, the restore index only covers what was actually archived
