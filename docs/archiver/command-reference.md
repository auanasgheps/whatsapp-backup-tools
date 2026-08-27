# Command Reference

## Config File

The config file is the easiest way to run (and re-run) the archiver: instead of repeating settings on the command line every time, save them in a `config.toml` file.

### Generating the Example File

```bash
python -m wab_archiver --generate-config
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
# mode      = ""          # "adb" or "restore"
# business  = false
# timezone  = ""          # e.g. Europe/Rome
# since     = ""          # e.g. 2024-01-01

# Android — pull media over ADB (slower than Syncthing; see setup-android.md)
# adb_pull_media    = false
# media_staging_dir = ""    # persistent folder for pulled media

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

```
usage: python -m wab_archiver [-h]
                        [--config PATH]
                        [--generate-config]
                        [--msgstore PATH]
                        [--e2e_key KEY]
                        [--contacts PATH]
                        [--wa_root PATH]
                        [--ios_backup PATH]
                        [--ios_password PASSWORD]
                        [--ios_contacts PATH]
                        [--business]
                        --output PATH
                        [--log PATH]
                        [--mode {adb,restore}]
                        [--adb-pull-media]
                        [--media-staging-dir PATH]
                        [--dry-run]
                        [--limit N]
                        [--since DATE]
                        [--timezone TZ]
```

| Argument | Required | Description |
|---|---|---|
| `--config PATH` | No | Path to a TOML config file. If omitted, auto-detects `config.toml` in the package folder or current directory |
| `--generate-config` | No | Write `example-config.toml` to the package folder and exit |
| `--msgstore PATH` | No | Path to `msgstore.db`, `msgstore.db.crypt15`, or `ChatStorage.sqlite`. Defaults to `msgstore.db` in the current folder |
| `--e2e_key KEY` | If encrypted | Your cryptographic key for `.crypt15` decryption |
| `--contacts PATH` | No | Path to the `wa_contacts` file exported via ADB (Android only) |
| `--wa_root PATH` | Android / iOS pre-extracted | Root path of your WhatsApp folder. **Repeat the flag** to specify multiple source folders — the archiver searches all roots and selects the best available copy of each file |
| `--ios_backup PATH` | iOS (recommended) | Path to the iPhone backup directory (the folder containing `Manifest.db`). Mutually exclusive with `--wa_root` |
| `--ios_password PASSWORD` | No | Password for an encrypted iPhone backup |
| `--ios_contacts PATH` | No | Path to `ContactsV2.sqlite` for iOS contacts. Auto-extracted from `--ios_backup` if omitted |
| `--business` | No | Target **WhatsApp Business** instead of the regular WhatsApp app |
| `--output PATH` | **Yes** | Destination folder for the archive |
| `--log PATH` | No | Custom log file path. Defaults to `<output>/wab-archiver.log` |
| `--mode {adb,restore}` | No | `adb` = automatically pull msgstore and contacts from a connected Android device; `restore` = reconstruct original `Media/` tree from the archive (Android only) |
| `--adb-pull-media` | No | Pull WhatsApp media files from the connected device via ADB. Only valid with `--mode adb`. See [Android setup](setup-android.md#pulling-media-via-adb-optional) before using |
| `--media-staging-dir PATH` | If `--adb-pull-media` | Local directory where ADB-pulled media is staged before archiving. Must be persistent across runs — state is tracked in the archive DB, not in this directory |
| `--dry-run` | No | Simulate the run without copying any files |
| `--limit N` | No | Cap rows returned per chat type (N/2 from groups, N/2 from 1-to-1). Total rows ≤ N. Useful for test runs |
| `--since DATE` | No | Only include messages on or after this date (`YYYY-MM-DD`). Combines freely with `--limit` |
| `--timezone TZ` | No | [IANA timezone name](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones) for year folder assignment and report timestamps (e.g. `Europe/Rome`, `America/New_York`, `UTC`). **Windows users: requires `pip install tzdata`** |

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
python -m wab_archiver \
  --mode adb \
  --e2e 1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b \
  --wa_root /path/to/WhatsApp/storage \
  --output /path/to/output \
  --dry-run
```

#### ADB mode — also pull media over USB

Use this if you do not use Syncthing and want the archiver to handle everything in one step. See [setup-android.md](setup-android.md#pulling-media-via-adb-optional) for important caveats about speed and reliability.

```bash
python -m wab_archiver \
  --mode adb \
  --e2e 1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b \
  --adb-pull-media \
  --media-staging-dir /path/to/wa-staging \
  --output /path/to/output
```

> ⚠️ On first run, every media file is transferred — this can take hours for large collections. Subsequent runs skip already-archived files automatically.

#### Manual — decrypted database

```bash
python -m wab_archiver \
  --msgstore /path/to/msgstore.db \
  --wa_root /path/to/WhatsApp/storage \
  --output /path/to/output \
  --contacts /path/to/wa_contacts \
  --dry-run
```

#### Manual — multiple source folders

Use this when media is split across several locations. Repeat `--wa_root` for each source.

```bash
python -m wab_archiver \
  --msgstore /path/to/msgstore.db \
  --wa_root /path/to/old-archive/WhatsApp \
  --wa_root /path/to/current-phone/WhatsApp \
  --output /path/to/output \
  --dry-run
```

> 💡 When the same file exists in multiple roots, the largest copy is used (higher quality heuristic). If sizes match but content differs, the first root takes priority and the conflict is written to `source_conflicts_report.csv` for review.

#### Manual — encrypted database (archiver decrypts)

```bash
python -m wab_archiver \
  --msgstore /path/to/msgstore.db.crypt15 \
  --e2e 1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b \
  --wa_root /path/to/WhatsApp/storage \
  --output /path/to/output \
  --dry-run
```

> 💡 Even in dry run mode, decryption still occurs so the DB can be queried. The decrypted `msgstore.db` is written to disk regardless of `--dry-run`.

### iOS

#### Standard flow — unencrypted backup

```bash
python -m wab_archiver \
  --ios_backup ~/Library/Application\ Support/MobileSync/Backup/<UDID> \
  --output /path/to/output \
  --dry-run
```

#### Standard flow — encrypted backup

```bash
python -m wab_archiver \
  --ios_backup ~/Library/Application\ Support/MobileSync/Backup/<UDID> \
  --ios_password your_backup_password \
  --output /path/to/output \
  --dry-run
```

> 💡 `--wa_root` and `--contacts` are not needed. The archiver extracts `ChatStorage.sqlite` and `ContactsV2.sqlite` directly from the backup. `ChatStorage.sqlite` is also saved to the output folder for re-runs.

### Advanced

#### Test run — recent files only

```bash
python -m wab_archiver \
  --msgstore /path/to/msgstore.db \
  --wa_root /path/to/WhatsApp/storage \
  --output /path/to/output \
  --since 2025-01-01 \
  --limit 100 \
  --dry-run
```

#### iOS — pre-extracted mode

Only use this mode if you know what you are doing.

```bash
python -m wab_archiver \
  --msgstore /path/to/ChatStorage.sqlite \
  --wa_root /path/to/AppDomainGroup-group.net.whatsapp.WhatsApp.shared \
  --ios_contacts /path/to/ContactsV2.sqlite \
  --output /path/to/output
```

---

## Output Files

| File | Description |
|---|---|
| `wab-archiver.log` | Full run log including all copied, skipped, and missing files |
| `missing_media_report.csv` | Structured report of all media referenced in the DB but not found on disk. Useful for manual recovery from old backups |
| `duplicate_media_report.csv` | Report of media files with identical content at multiple archive paths. One row per path, sortable by `file_count`. Only written when duplicates exist |
| `source_conflicts_report.csv` | Written when multiple `-wa` roots contain different versions of the same file. Only written when conflicts exist |
| `.wa_media_archiver.db` | SQLite database storing all persistent state: contact folder index, group folder index, and the file archive map. Health checks run automatically on every open. Do not delete unless you want to reset all tracking |
| `restore_report.csv` | Written by restore mode when issues are encountered. Not written if there are no issues |

---

## Restore Mode

Restore mode reconstructs the flat `WhatsApp/Media/` folder structure directly inside the archive folder, without needing the original device or database.

> ⚠️ **Android archives only.** iOS media cannot be restored this way.

```bash
python -m wab_archiver \
  --mode restore \
  --output /path/to/output
```

The reconstructed tree is written to `<output>/Media/`, alongside the existing `Contacts/` and `Groups/` folders. No files are overwritten — identical files already in place are skipped silently.

**How it works:** During every real (non-dry-run) forward run, the archiver records each original `file_path` and the archive path it was copied to in `.wa_media_archiver.db`. Restore mode reads this data and copies each unique original file back to its original relative path. If the same file was archived from multiple chats, it is restored exactly once.

**Limitations:**
- Restored file timestamps reflect the WhatsApp message timestamp (same as in the archive), not the original on-device creation date
- If a forward run was filtered with `--since` or `--limit`, the restore index only covers what was actually archived
